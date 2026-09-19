"""Semantic skill matching.

Freelancer projects list a handful of required skills, but a client rarely tags them the way a
freelancer names their own. A React Native developer is a perfect fit for an "Expo cross-platform
app" that never says "React Native", and a substring match (:func:`scoring._contains_term`) scores
that zero. This module asks an LLM instead: given the freelancer's skills and the job, how well do
they line up, 0-100, with a one-line reason.

Two providers, selected by ``LLM_PROVIDER`` — the same switch and models as ``drafting`` — so
switching is a config change, not a rewrite. Both are asked for a strict JSON object.

The call is not cheap, so callers must gate it: only run it on postings that are new or whose
content changed (the pipeline already tracks both), cache the result on the recommendation, and
fall back to substring matching if this raises. Nothing here may take down the poll loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.connectors.freelancer import JobPosting
from app.db.models import FreelancerProfile
from app.services.llm import LLMError, complete_json

logger = logging.getLogger(__name__)

# A match is a bounded classification, not prose — it needs almost no output budget. The Gemini and
# Claude models still think before answering, and that thinking draws from the same budget, so this
# leaves headroom for it while capping the visible JSON.
MAX_OUTPUT_TOKENS = 2000

# The brief carries the signal; the boilerplate that follows a long post rarely changes the match.
DESCRIPTION_LIMIT = 4000

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Shared by both providers so the two paths judge on the same instructions.
_SYSTEM_PROMPT = (
    "# Role\n"
    "You are a matching engine for a freelancing marketplace. You judge how well a freelancer's "
    "skills fit what a job actually needs.\n"
    "\n"
    "# Task\n"
    "Given the freelancer's skills and a job posting, decide how well their abilities fit the "
    "job's real requirements and score the fit from 0 to 100.\n"
    "\n"
    "# Constraints\n"
    "- Reason semantically about equivalent and adjacent skills, not just literal word overlap "
    "(e.g. 'React Native' fits an Expo cross-platform app; 'PostgreSQL' fits a job that only says "
    "'relational database').\n"
    "- Only credit skills the freelancer actually lists — never reward a skill they lack just "
    "because the job wants it.\n"
    "- Score 0-100: 100 means the freelancer clearly has what the job needs; 0 means no relevant "
    "overlap.\n"
    "\n"
    "# Result\n"
    "An honest fit score a freelancer could trust to rank one job above another, backed by the "
    "specific skills that earned it.\n"
    "\n"
    "# Output\n"
    'Return a JSON object of the form {"match_score", "matched_skills", "reason"}, where each '
    "field is:\n"
    "- match_score: integer 0-100 — how well the freelancer fits the job.\n"
    "- matched_skills: the freelancer's listed skills that drove the match.\n"
    "- reason: one short sentence explaining the score.\n"
)

# Numeric bounds aren't expressible in structured-output schemas, so the score is clamped in code.
_SCHEMA = {
    "type": "object",
    "properties": {
        "match_score": {"type": "integer"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["match_score", "matched_skills", "reason"],
    "additionalProperties": False,
}


class MatchingError(RuntimeError):
    pass


@dataclass
class SkillMatch:
    """A semantic fit, normalised so scoring can treat it as a 0-1 fraction of the skills weight."""

    score: float  # 0.0-1.0
    matched: list[str] = field(default_factory=list)
    reason: str = ""


async def match_skills(job: JobPosting, profile: FreelancerProfile) -> SkillMatch | None:
    """Return the semantic fit of ``profile``'s skills to ``job``, or ``None`` to fall back.

    ``None`` (rather than an exception) means "no opinion, use the substring match instead": the
    feature is disabled, no key is configured, or the profile lists no skills. Genuine call
    failures raise ``MatchingError`` so the caller can log and still fall back.
    """
    settings = get_settings()
    if not settings.skill_match_enabled:
        return None

    skills = [s["name"] for s in (profile.skills or []) if s.get("name")]
    if not skills:
        return None

    message = _build_user_message(job, skills)

    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            return None
        return await _match_with_gemini(message)
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            return None
        return await _match_with_anthropic(message)
    if settings.llm_provider == "nvidia":
        if not settings.nvidia_api_key:
            return None
        try:
            result = await complete_json(
                _SYSTEM_PROMPT, message, _SCHEMA, MAX_OUTPUT_TOKENS, "skill_match"
            )
        except LLMError as exc:
            raise MatchingError(str(exc)) from exc
        return _parse(json.dumps(result["data"]))
    raise MatchingError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r} — expected "
        "'gemini', 'anthropic' or 'nvidia'"
    )


async def _match_with_gemini(message: str) -> SkillMatch:
    settings = get_settings()
    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": message}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(_SCHEMA),
        },
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                url,
                params={"key": settings.gemini_api_key},
                json=payload,
                headers={"content-type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise MatchingError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        # The key rides in the query string, so keep it out of the error text.
        raise MatchingError(f"Gemini API returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise MatchingError(
            f"Gemini returned no candidates{f' (blocked: {blocked})' if blocked else ''}"
        )

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    return _parse(text)


async def _match_with_anthropic(message: str) -> SkillMatch:
    settings = get_settings()

    # Imported lazily so the Gemini path doesn't require the SDK to be installed.
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            output_config={
                "effort": "low",  # a bounded classification, not deep reasoning
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Byte-identical across every job, so it caches.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.APIStatusError as exc:
        raise MatchingError(f"Claude API returned {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise MatchingError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise MatchingError("Claude declined to score this match")

    # output_config.format guarantees a single text block of schema-valid JSON.
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return _parse(text)


def _parse(text: str) -> SkillMatch:
    """Turn the model's JSON into a normalised ``SkillMatch``, clamping the score to 0-1."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MatchingError(f"Match response was not valid JSON: {text[:200]!r}") from exc

    raw = data.get("match_score")
    try:
        score = max(0.0, min(100.0, float(raw))) / 100.0
    except (TypeError, ValueError) as exc:
        raise MatchingError(f"Match score was not a number: {raw!r}") from exc

    matched = [str(s) for s in (data.get("matched_skills") or []) if s]
    reason = str(data.get("reason") or "").strip()
    return SkillMatch(score=score, matched=matched, reason=reason)


def _build_user_message(job: JobPosting, skills: list[str]) -> str:
    description = job.description[:DESCRIPTION_LIMIT]
    if len(job.description) > DESCRIPTION_LIMIT:
        description += "\n[description truncated]"

    return "\n".join(
        [
            "<freelancer_skills>",
            ", ".join(skills),
            "</freelancer_skills>",
            "",
            "<job_post>",
            f"Title: {job.title}",
            f"Skills the client tagged: {', '.join(job.skills_listed) or 'none listed'}",
            "",
            "Description:",
            description,
            "</job_post>",
        ]
    )


def _gemini_schema(schema: dict) -> dict:
    """Gemini's response schema doesn't accept ``additionalProperties`` — drop it recursively."""
    out: dict = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if isinstance(value, dict):
            out[key] = _gemini_schema(value)
        else:
            out[key] = value
    return out
