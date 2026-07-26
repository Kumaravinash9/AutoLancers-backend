"""Proposal drafting.

The system prompt lives in ``app/prompts/proposal_system.md`` and is sourced from the
``freelancer-proposal`` skill — the six-beat flow, the 120-180 word limit, the conversion rules,
and the honesty guardrail. Your identity is injected as a structured data block rather than prose
so the model treats it as facts to use, not text to imitate.

Two providers are supported, selected by ``LLM_PROVIDER``: Gemini (default) and Anthropic. The
prompt is provider-neutral, so switching is a config change rather than a rewrite.

Failures here must never take down the poll loop: a rate limit or a 500 leaves the job undrafted
and the next cycle picks it up again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from app.config import get_settings
from app.connectors.freelancer import JobPosting
from app.db.models import FreelancerProfile

logger = logging.getLogger(__name__)

# Current Gemini and Claude models both think before answering, and that thinking is drawn from
# the same output budget as the reply. Measured against real postings, a ~150-word proposal costs
# ~200 output tokens but over 1,200 thinking tokens — a budget sized for the visible text alone
# silently returns nothing. Only generated tokens are billed, so the headroom is free.
MAX_OUTPUT_TOKENS = 8000

# Long posts add cost without improving the draft; the first ~6k characters carry the brief.
DESCRIPTION_LIMIT = 6000

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "proposal_system.md"


class DraftingError(RuntimeError):
    pass


@dataclass
class Draft:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None


@lru_cache
def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


async def draft_proposal(job: JobPosting, profile: FreelancerProfile) -> Draft:
    """Draft one proposal. Raises ``DraftingError``; callers are expected to catch and continue."""
    settings = get_settings()
    message = _build_user_message(job, profile)

    if settings.llm_provider == "gemini":
        return await _draft_with_gemini(message)
    if settings.llm_provider == "anthropic":
        return await _draft_with_anthropic(message)
    raise DraftingError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r} — expected 'gemini' or 'anthropic'"
    )


async def _draft_with_gemini(message: str) -> Draft:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise DraftingError("GEMINI_API_KEY is not set")

    payload = {
        "systemInstruction": {"parts": [{"text": _system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": message}]}],
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "temperature": 0.7},
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                url,
                params={"key": settings.gemini_api_key},
                json=payload,
                headers={"content-type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise DraftingError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        # The key is in the query string, so keep it out of logs and error text.
        raise DraftingError(f"Gemini API returned {response.status_code}: {response.text[:300]}")

    data = response.json()

    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        suffix = f" (blocked: {blocked})" if blocked else ""
        raise DraftingError(f"Gemini returned no candidates{suffix}")

    candidate = candidates[0]
    finish = candidate.get("finishReason")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()

    if not text:
        # The usual cause is the whole budget going to thinking tokens.
        raise DraftingError(f"Gemini returned an empty draft (finishReason={finish})")
    if finish == "MAX_TOKENS":
        raise DraftingError("Gemini hit the output limit — the draft would be truncated")

    usage = data.get("usageMetadata") or {}
    return Draft(
        text=text,
        model=data.get("modelVersion") or settings.gemini_model,
        input_tokens=usage.get("promptTokenCount"),
        # Thinking is billed but not returned; count it so cost per bid is honest.
        output_tokens=(usage.get("candidatesTokenCount") or 0)
        + (usage.get("thoughtsTokenCount") or 0),
    )


async def _draft_with_anthropic(message: str) -> Draft:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise DraftingError("ANTHROPIC_API_KEY is not set")

    # Imported lazily so the Gemini path doesn't require the SDK to be installed.
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(),
                    # Byte-identical across every job, so it caches.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.APIStatusError as exc:
        raise DraftingError(f"Claude API returned {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise DraftingError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise DraftingError("Claude declined to draft this proposal")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise DraftingError("Claude returned an empty draft")

    return Draft(
        text=text,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _build_user_message(job: JobPosting, profile: FreelancerProfile) -> str:
    description = job.description[:DESCRIPTION_LIMIT]
    if len(job.description) > DESCRIPTION_LIMIT:
        description += "\n[description truncated]"

    return "\n".join(
        [
            "<freelancer_profile>",
            _profile_block(profile),
            "</freelancer_profile>",
            "",
            "<job_post>",
            f"Title: {job.title}",
            f"Budget: {_budget_line(job)}",
            f"Client's listed skills: {', '.join(job.skills_listed) or 'none listed'}",
            "",
            "Description:",
            description,
            "</job_post>",
            "",
            "Write the proposal now.",
        ]
    )


def _profile_block(profile: FreelancerProfile) -> str:
    skills = ", ".join(s["name"] for s in (profile.skills or []) if s.get("name"))
    lines = [
        f"Name / brand: {profile.display_name or 'not set'}",
        f"Headline: {profile.headline or 'not set'}",
        f"Skills: {skills or 'not set'}",
    ]
    if profile.proposal_notes:
        lines += ["", "Proof points, standard offer, and positioning:", profile.proposal_notes]
    return "\n".join(lines)


def _budget_line(job: JobPosting) -> str:
    if job.budget_min is None and job.budget_max is None:
        return "not stated"
    currency = job.currency or ""
    kind = f" ({job.budget_type})" if job.budget_type else ""
    if job.budget_min is not None and job.budget_max is not None:
        return f"{job.budget_min:.0f}-{job.budget_max:.0f} {currency}{kind}".strip()
    stated = job.budget_max if job.budget_max is not None else job.budget_min
    return f"{stated:.0f} {currency}{kind}".strip()
