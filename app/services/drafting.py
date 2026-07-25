"""Proposal drafting via the Claude API.

The system prompt lives in ``app/prompts/proposal_system.md`` and is sourced from the
``freelancer-proposal`` skill — the six-beat flow, the 120-180 word limit, the conversion rules,
and the honesty guardrail. Your identity is injected as a structured data block rather than prose
so the model treats it as facts to use, not text to imitate.

Failures here must never take down the poll loop: a rate limit or a 500 leaves the job undrafted
and the next cycle picks it up again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import anthropic

from app.config import get_settings
from app.connectors.freelancer import JobPosting
from app.db.models import Profile

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Thinking is on by default on this model and max_tokens caps thinking *plus* response text, so a
# budget sized for a 180-word proposal would truncate mid-sentence. Only generated tokens are
# billed, so the headroom is free.
MAX_TOKENS = 8000

# A proposal is a short, well-specified generation. Low effort is strong here and keeps both
# latency and cost per bid down; raise it if drafts start feeling generic.
EFFORT = "low"

# Long posts add cost without improving the draft; the first ~6k characters carry the brief.
DESCRIPTION_LIMIT = 6000

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


@lru_cache
def _client() -> anthropic.AsyncAnthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise DraftingError("ANTHROPIC_API_KEY is not set")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def draft_proposal(job: JobPosting, profile: Profile) -> Draft:
    """Draft one proposal. Raises ``DraftingError``; callers are expected to catch and continue."""
    message = _build_user_message(job, profile)

    try:
        response = await _client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(),
                    # The system prompt is byte-identical across every job, so it caches.
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


def _build_user_message(job: JobPosting, profile: Profile) -> str:
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


def _profile_block(profile: Profile) -> str:
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
