"""Freelancer.com API client — discovery only.

There is intentionally no bid-submission method here. v1 drafts proposals for you to copy across
by hand; adding submission is a deliberate future change, not an accident of having the code lying
around.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://www.freelancer.com/api"
ACTIVE_PROJECTS_PATH = "/projects/0.1/projects/active/"

# Freelancer uses its own auth header, not `Authorization: Bearer`.
AUTH_HEADER = "freelancer-oauth-v1"


@dataclass
class JobPosting:
    """Platform-neutral normalised posting."""

    platform: str
    external_id: str
    title: str
    description: str
    url: str
    skills_listed: list[str] = field(default_factory=list)
    budget_type: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str | None = None
    bid_count: int | None = None
    posted_at: dt.datetime | None = None


class FreelancerAPIError(RuntimeError):
    pass


class FreelancerClient:
    def __init__(self, access_token: str | None = None, timeout: float = 30.0) -> None:
        self.access_token = access_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {AUTH_HEADER: self.access_token} if self.access_token else {}

    async def search_active_projects(
        self,
        query: str | None = None,
        limit: int = 50,
        project_types: tuple[str, ...] = ("fixed", "hourly"),
        max_retries: int = 3,
    ) -> list[JobPosting]:
        """Fetch active projects, retrying transient failures with exponential backoff.

        Raises on permanent failures (4xx other than 429); the caller decides whether one bad
        cycle should be logged and skipped.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "full_description": "true",
            "job_details": "true",
            "project_types[]": list(project_types),
        }
        if query:
            params["query"] = query

        url = f"{API_BASE}{ACTIVE_PROJECTS_PATH}"
        delay = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt == max_retries:
                    raise FreelancerAPIError(
                        f"Network error after {attempt} attempts: {exc}"
                    ) from exc
                logger.warning("Freelancer request failed (%s), retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_retries:
                    raise FreelancerAPIError(
                        f"Freelancer API returned {response.status_code} after {attempt} attempts"
                    )
                retry_after = float(response.headers.get("retry-after", delay))
                logger.warning(
                    "Freelancer API %s, retrying in %.0fs", response.status_code, retry_after
                )
                await asyncio.sleep(retry_after)
                delay *= 2
                continue

            if response.status_code >= 400:
                raise FreelancerAPIError(
                    f"Freelancer API returned {response.status_code}: {response.text[:400]}"
                )

            payload = response.json()
            projects = (payload.get("result") or {}).get("projects") or []
            return [normalize_project(p) for p in projects]

        raise FreelancerAPIError("Exhausted retries without a response")


def normalize_project(raw: dict[str, Any]) -> JobPosting:
    """Map one raw API project onto ``JobPosting``.

    Defensive throughout: fields are absent often enough on real responses that assuming any of
    them is a runtime error waiting to happen. Anything missing stays ``None`` so scoring can tell
    "absent" apart from "zero" and skip the filter rather than silently rejecting.
    """
    budget = raw.get("budget") or {}
    currency = (raw.get("currency") or {}).get("code")
    bid_stats = raw.get("bid_stats") or {}

    seo_url = raw.get("seo_url")
    url = f"https://www.freelancer.com/projects/{seo_url}" if seo_url else ""

    posted_at = None
    submitdate = raw.get("submitdate")
    if isinstance(submitdate, int | float):
        posted_at = dt.datetime.fromtimestamp(submitdate, tz=dt.UTC)

    jobs = raw.get("jobs") or []
    skills = [j.get("name", "") for j in jobs if isinstance(j, dict) and j.get("name")]

    project_type = raw.get("type")
    budget_type = project_type if project_type in ("fixed", "hourly") else None

    return JobPosting(
        platform="freelancer",
        external_id=str(raw.get("id", "")),
        title=raw.get("title") or "",
        description=raw.get("description") or raw.get("preview_description") or "",
        url=url,
        skills_listed=skills,
        budget_type=budget_type,
        budget_min=_as_float(budget.get("minimum")),
        budget_max=_as_float(budget.get("maximum")),
        currency=currency,
        bid_count=_as_int(bid_stats.get("bid_count")),
        posted_at=posted_at,
    )


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(value)
    return None
