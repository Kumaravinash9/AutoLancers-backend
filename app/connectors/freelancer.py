"""Freelancer.com API client — discovery only.

There is intentionally no bid-submission method here. v1 drafts proposals for you to copy across
by hand; adding submission is a deliberate future change, not an accident of having the code lying
around.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Module-wide cache for the skill catalogue (see ``fetch_skill_catalogue``). It's the same static,
# account-independent list for everyone, so one copy shared across client instances is correct.
_CATALOGUE_TTL_SECONDS = 24 * 3600
_CATALOGUE_PAGE_SIZE = 1000
_CATALOGUE_MAX_PAGES = 20  # backstop: 20k skills is far above Freelancer's real count
_catalogue_cache: dict[str, int] | None = None
_catalogue_fetched_at: float = 0.0

API_BASE = "https://www.freelancer.com/api"
ACTIVE_PROJECTS_PATH = "/projects/0.1/projects/active/"
JOBS_PATH = "/projects/0.1/jobs/"
BIDS_PATH = "/projects/0.1/bids/"
PROJECTS_BY_ID_PATH = "/projects/0.1/projects/"
SELF_PATH = "/users/0.1/self/"

# Only for vocabulary Freelancer names differently enough that no amount of string matching gets
# there — "llm" will never resolve to "Artificial Intelligence" on its own. Everything else
# (react -> React.js, chatbot -> AI Chatbot) is handled by the word-boundary fallback below.
SKILL_ALIASES = {
    "ai": "artificial intelligence",
    "llm": "artificial intelligence",
    "genai": "artificial intelligence",
    "rag": "artificial intelligence",
    "ml": "machine learning (ml)",
    "js": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "automation": "api development",
}

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

    async def fetch_self(self) -> dict[str, Any]:
        """The authenticated user's own record: identity, reputation and public profile.

        Every one of these is an opt-in flag; without them the response omits the block entirely
        rather than returning nulls. ``jobs`` carries the skills the account advertises and
        ``profile_description`` the summary — the two things a client actually reads.
        """
        payload = await self._get(
            f"{API_BASE}{SELF_PATH}",
            {
                "avatar": "true",
                "reputation": "true",
                "display_info": "true",
                "jobs": "true",
                "profile_description": "true",
                "country_details": "true",
                "portfolio_details": "true",
            },
        )
        return (payload.get("result") or {}) or {}

    async def fetch_self_id(self) -> int:
        """The authenticated user's own id, required as ``bidder_id`` when placing a bid.

        The API will not infer it from the token — omitting it (or sending null, as the earlier
        prototype did) is rejected.
        """
        payload = await self._get(f"{API_BASE}{SELF_PATH}", {})
        user_id = (payload.get("result") or {}).get("id")
        if not user_id:
            raise FreelancerAPIError(f"Could not read own user id from {payload}")
        return int(user_id)

    async def submit_bid(
        self,
        project_id: int,
        bidder_id: int,
        amount: float,
        period_days: int,
        description: str,
        milestone_percentage: int = 100,
    ) -> str:
        """Place a real bid. Returns Freelancer's bid id.

        Nothing in the polling path calls this. It is reachable only from the explicit per-job
        endpoint, which itself requires a confirmation flag — placing a bid should take a
        deliberate act, not a default.
        """
        if not self.access_token:
            raise FreelancerAPIError("Cannot bid without an access token")

        payload = {
            "project_id": project_id,
            "bidder_id": bidder_id,
            "amount": amount,
            "period": period_days,
            "description": description,
            # Required for fixed-price projects; harmless on hourly.
            "milestone_percentage": milestone_percentage,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{API_BASE}{BIDS_PATH}", json=payload, headers=self._headers()
            )

        if response.status_code >= 400:
            raise FreelancerAPIError(
                f"Bid rejected ({response.status_code}): {response.text[:400]}"
            )

        result = (response.json() or {}).get("result") or {}
        bid_id = result.get("id")
        if not bid_id:
            raise FreelancerAPIError(f"Bid response had no id: {response.text[:300]}")
        return str(bid_id)

    async def fetch_my_bids(self, bidder_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Every bid this account has placed, including ones made outside this tool.

        Returned raw: the mapping onto our own rows is a decision for the sync service, not the
        transport.
        """
        payload = await self._get(
            f"{API_BASE}{BIDS_PATH}",
            {"bidders[]": [bidder_id], "limit": limit, "compact": "false"},
        )
        result = payload.get("result") or {}
        return list(result.get("bids") or [])

    async def fetch_projects_by_id(self, project_ids: list[int]) -> dict[str, JobPosting]:
        """Fetch specific projects by id, keyed by external id.

        Needed because a bid can point at a project we never saw — placed before this tool
        existed, or on work outside the profile's skill filter.
        """
        if not project_ids:
            return {}
        payload = await self._get(
            f"{API_BASE}{PROJECTS_BY_ID_PATH}",
            {
                "projects[]": project_ids,
                "full_description": "true",
                "job_details": "true",
            },
        )
        projects = (payload.get("result") or {}).get("projects") or []
        return {str(p.get("id")): normalize_project(p) for p in projects if p.get("id")}

    async def fetch_skill_catalogue(self) -> dict[str, int]:
        """Return Freelancer's whole skill list as ``{lowercased name: id}``.

        Paged to completion rather than capped at a single request: Freelancer limits a page to its
        own maximum, so one call would silently truncate the dictionary and lose skills we could
        match on. The catalogue is effectively static and account-independent, so the full result is
        cached module-wide for ``_CATALOGUE_TTL_SECONDS`` — paid once a day at most.
        """
        global _catalogue_cache, _catalogue_fetched_at
        now = time.monotonic()
        if _catalogue_cache is not None and (now - _catalogue_fetched_at) < _CATALOGUE_TTL_SECONDS:
            return _catalogue_cache

        catalogue: dict[str, int] = {}
        offset = 0
        for _ in range(_CATALOGUE_MAX_PAGES):
            response = await self._get(
                f"{API_BASE}{JOBS_PATH}", {"limit": _CATALOGUE_PAGE_SIZE, "offset": offset}
            )
            entries = response.get("result") or []
            if not entries:
                break
            before = len(catalogue)
            for e in entries:
                if e.get("name") and e.get("id"):
                    catalogue[e["name"].lower()] = e["id"]
            # Stop on an empty page, or if a full page added nothing new — the latter guards against
            # an endpoint that ignores ``offset`` and would otherwise loop on the same first page.
            if len(catalogue) == before:
                break
            offset += len(entries)

        logger.info("Loaded %d Freelancer skills into the catalogue", len(catalogue))
        if catalogue:  # never cache an empty catalogue — that's a bad response, not a real answer
            _catalogue_cache = catalogue
            _catalogue_fetched_at = now
        return catalogue

    async def resolve_skill_ids(self, names: list[str]) -> tuple[list[int], list[str]]:
        """Map profile skill names onto Freelancer skill IDs.

        Returns ``(ids, unmatched_names)``. Unmatched names are handed back rather than silently
        dropped — a typo'd or non-existent skill quietly narrowing your search is exactly the kind
        of failure that looks like "the tool found nothing good today".
        """
        catalogue = await self.fetch_skill_catalogue()
        ids: list[int] = []
        unmatched: list[str] = []

        for raw in names:
            name = raw.strip().lower()
            if not name:
                continue

            candidate = catalogue.get(name)
            if candidate is None:
                candidate = catalogue.get(SKILL_ALIASES.get(name, ""))
            if candidate is None:
                candidate = _closest_skill(catalogue, name)

            if candidate is None:
                unmatched.append(raw)
            elif candidate not in ids:
                ids.append(candidate)

        return ids, unmatched

    async def search_active_projects(
        self,
        query: str | None = None,
        skill_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
        from_time: int | None = None,
        project_types: tuple[str, ...] = ("fixed", "hourly"),
        max_retries: int = 3,
    ) -> list[JobPosting]:
        """Fetch one page of active projects, retrying transient failures with exponential backoff.

        Pass ``skill_ids`` to filter server-side by skill. Without it this returns whatever was
        posted most recently across the whole marketplace, which for a specialist profile is
        almost entirely noise.

        ``from_time`` (a Unix timestamp) and ``offset`` drive incremental paging: fetch only
        postings newer than the last watermark, one ``limit``-sized page at a time. Results are
        newest-first, so walking ``offset`` forward walks backward in time toward that watermark.

        Raises on permanent failures (4xx other than 429); the caller decides whether one bad
        cycle should be logged and skipped.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "full_description": "true",
            "job_details": "true",
            "project_types[]": list(project_types),
            # Newest first. Bid counts on Freelancer climb fast — postings routinely pass 40 bids
            # within a couple of hours — so getting there early is most of the advantage.
            "sort_field": "time_submitted",
        }
        if query:
            params["query"] = query
        if skill_ids:
            params["jobs[]"] = skill_ids
        if from_time is not None:
            params["from_time"] = from_time

        payload = await self._get(
            f"{API_BASE}{ACTIVE_PROJECTS_PATH}", params, max_retries=max_retries
        )
        projects = (payload.get("result") or {}).get("projects") or []
        return [normalize_project(p) for p in projects]

    async def _get(
        self, url: str, params: dict[str, Any], max_retries: int = 3
    ) -> dict[str, Any]:
        """GET with exponential backoff on 429 and 5xx. Raises on permanent failures."""
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

            return response.json()

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


def _closest_skill(catalogue: dict[str, int], term: str) -> int | None:
    """Best catalogue entry containing ``term`` as a whole word, or None.

    Freelancer qualifies most skill names ("React.js", "AI Chatbot"), so an exact lookup on what
    someone types into a profile usually misses. Requiring a word boundary keeps "api" from
    matching "Rapid Prototyping", and preferring the shortest match picks the canonical entry —
    "React.js" over "React Native", "AI Chatbot" over "AI Chatbot Development".
    """
    pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
    matches = [name for name in catalogue if pattern.search(name)]
    if not matches:
        return None
    return catalogue[min(matches, key=len)]


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(value)
    return None
