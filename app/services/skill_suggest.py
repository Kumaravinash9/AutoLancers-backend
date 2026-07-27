"""Skill suggestion: complete a freelancer's skill list from what they've already shown.

Freelancers routinely under-list. Someone writes "frontend", lists three skills, and forgets the
five more their portfolio, their marketplace account, and their past proposals plainly demonstrate.
An incomplete list quietly narrows both discovery (the server-side skill filter) and matching, so
the tool under-serves exactly the people who described themselves badly.

This asks an LLM to read the evidence the freelancer has *already* produced — headline, bio,
portfolio, experience, the skills their marketplace account advertises, and proposals they've
written — and propose additional skills they plausibly have. It never invents: every suggestion has
to be grounded in that evidence, and it carries a one-line reason so the accept/reject UI can show
*why*.

Crucially, suggestions are proposals, not facts. They land in ``FreelancerProfile.suggested_skills``
and feed nothing until the freelancer confirms one, at which point the route moves it into
``skills``. That separation is what keeps a model's guess from inflating a match score.

Two providers, selected by ``LLM_PROVIDER`` — same switch and models as ``drafting`` and
``matching``. Both are asked for a strict JSON object.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.db.models import FreelancerProfile, PlatformConnection
from app.services.llm import LLMError, complete_json

logger = logging.getLogger(__name__)

# Suggestions are a short bounded list, but the models think before answering and that draws from
# the same budget, so leave headroom while capping the visible JSON.
MAX_OUTPUT_TOKENS = 4000

# Evidence caps — enough context to judge, not so much that a long portfolio dominates the budget.
MAX_PROPOSAL_SAMPLES = 8
PROPOSAL_SAMPLE_LIMIT = 800
TEXT_FIELD_LIMIT = 2000

# Never propose more than this in one pass; a wall of low-confidence suggestions is noise.
MAX_SUGGESTIONS = 12

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SYSTEM_PROMPT = (
    "# Role\n"
    "You are a skills analyst for a freelancing marketplace. You read the evidence a freelancer "
    "has already produced and identify the marketplace skills they can demonstrably offer.\n"
    "\n"
    "# Task\n"
    "You are given the freelancer's already-listed skills and their evidence: headline, bio, "
    "portfolio, work experience, the skills their marketplace account advertises, and proposals "
    "they have written. Propose additional marketplace skills they plausibly have but have not "
    "listed yet.\n"
    "\n"
    "# Constraints\n"
    "- Only suggest a skill clearly supported by the evidence. Never invent, pad, or guess to fill "
    "a quota.\n"
    "- Do not repeat any skill already listed.\n"
    "- Use canonical marketplace skill names ('React.js', 'Node.js', 'PostgreSQL', 'REST API'), "
    "not vague phrases like 'backend' or 'good communication'.\n"
    "- Ground every suggestion in specific evidence and name where that evidence came from.\n"
    "- Prefer a few well-grounded suggestions over many weak ones.\n"
    "\n"
    "# Result\n"
    "A short, high-precision list the freelancer would recognise as obviously theirs — each skill "
    "traceable to something concrete in the evidence and weighted by how central it is to their "
    "work.\n"
    "\n"
    "# Output\n"
    'Return a JSON object of the form {"suggestions": [{"name", "weight", "reason", "source"}]}, '
    "where each field is:\n"
    "- name: the canonical skill name.\n"
    "- weight: integer 1-5 — 1 is minor/incidental, 5 is a core strength.\n"
    "- reason: one short sentence citing the specific evidence for this skill.\n"
    "- source: where the evidence came from — one of headline, bio, portfolio, experience, "
    "account, proposals.\n"
    "If nothing in the evidence supports a new skill, return an empty suggestions array."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "weight": {"type": "integer"},
                    "reason": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "weight", "reason", "source"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


class SkillSuggestError(RuntimeError):
    pass


@dataclass
class SkillSuggestion:
    """A proposed skill, grounded in evidence, awaiting the freelancer's confirmation."""

    name: str
    weight: int  # 1-5
    reason: str
    source: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "weight": self.weight, "reason": self.reason, "source": self.source}


async def suggest_skills(
    profile: FreelancerProfile,
    connections: list[PlatformConnection],
    proposal_texts: list[str],
) -> list[SkillSuggestion]:
    """Propose skills the evidence supports but ``profile.skills`` omits.

    Raises ``SkillSuggestError`` on misconfiguration (unknown provider, missing key) or a genuine
    call failure — this is a user-initiated action, so the caller surfaces the error rather than
    silently returning nothing.
    """
    settings = get_settings()
    message = _build_user_message(profile, connections, proposal_texts)

    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise SkillSuggestError("GEMINI_API_KEY is not set")
        raw = await _suggest_with_gemini(message)
    elif settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise SkillSuggestError("ANTHROPIC_API_KEY is not set")
        raw = await _suggest_with_anthropic(message)
    elif settings.llm_provider == "nvidia":
        if not settings.nvidia_api_key:
            raise SkillSuggestError("NVIDIA_API_KEY is not set")
        try:
            result = await complete_json(
                _SYSTEM_PROMPT, message, _SCHEMA, MAX_OUTPUT_TOKENS, "skill_suggestions"
            )
        except LLMError as exc:
            raise SkillSuggestError(str(exc)) from exc
        raw = _parse(json.dumps(result["data"]))
    else:
        raise SkillSuggestError(
            f"Unknown LLM_PROVIDER {settings.llm_provider!r} — expected "
            "'gemini', 'anthropic' or 'nvidia'"
        )

    return _finalise(raw, already_listed=profile.skills or [])


async def _suggest_with_gemini(message: str) -> list[dict]:
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
        raise SkillSuggestError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        # The key rides in the query string, so keep it out of the error text.
        raise SkillSuggestError(f"Gemini API returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise SkillSuggestError(
            f"Gemini returned no candidates{f' (blocked: {blocked})' if blocked else ''}"
        )

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    return _parse(text)


async def _suggest_with_anthropic(message: str) -> list[dict]:
    settings = get_settings()

    # Imported lazily so the Gemini path doesn't require the SDK to be installed.
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            output_config={
                "effort": "low",  # grounded extraction, not deep reasoning
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Byte-identical across every freelancer, so it caches.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.APIStatusError as exc:
        raise SkillSuggestError(f"Claude API returned {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise SkillSuggestError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise SkillSuggestError("Claude declined to suggest skills")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return _parse(text)


def _parse(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SkillSuggestError(f"Suggestion response was not valid JSON: {text[:200]!r}") from exc
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        raise SkillSuggestError("Suggestion response had no 'suggestions' list")
    return suggestions


def _finalise(raw: list[dict], already_listed: list[dict]) -> list[SkillSuggestion]:
    """Normalise, clamp weights, and drop anything already listed or duplicated."""
    listed = {s["name"].strip().lower() for s in already_listed if s.get("name")}
    seen: set[str] = set()
    out: list[SkillSuggestion] = []

    for item in raw:
        name = str(item.get("name") or "").strip()
        key = name.lower()
        if not name or key in listed or key in seen:
            continue
        seen.add(key)

        try:
            weight = int(item.get("weight", 1))
        except (TypeError, ValueError):
            weight = 1
        weight = max(1, min(5, weight))

        out.append(
            SkillSuggestion(
                name=name,
                weight=weight,
                reason=str(item.get("reason") or "").strip(),
                source=str(item.get("source") or "").strip().lower(),
            )
        )
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def _build_user_message(
    profile: FreelancerProfile,
    connections: list[PlatformConnection],
    proposal_texts: list[str],
) -> str:
    listed = ", ".join(s["name"] for s in (profile.skills or []) if s.get("name"))

    # Merge the evidence that lives on the marketplace accounts. The freelancer's own profile text
    # and the account's public text often say different, complementary things.
    account_skills = sorted(
        {s for c in connections for s in (c.account_skills or []) if s}
    )
    account_text = _first_nonempty(
        [c.summary for c in connections] + [c.tagline for c in connections]
    )

    lines = [
        "<already_listed_skills>",
        listed or "none",
        "</already_listed_skills>",
        "",
        "<evidence>",
        f"Headline: {profile.headline or 'not set'}",
        f"Bio: {_clip(profile.bio)}",
        f"Skills the marketplace account advertises: {', '.join(account_skills) or 'none'}",
        f"Marketplace account summary: {_clip(account_text)}",
        f"Portfolio: {_clip(_join_entries(profile.portfolio))}",
        f"Experience: {_clip(_join_entries(profile.experience))}",
        "",
        "Recent proposals the freelancer has written:",
        _proposal_block(proposal_texts),
        "</evidence>",
    ]
    return "\n".join(lines)


def _proposal_block(texts: list[str]) -> str:
    samples = [t.strip() for t in texts if t and t.strip()][:MAX_PROPOSAL_SAMPLES]
    if not samples:
        return "none"
    return "\n---\n".join(t[:PROPOSAL_SAMPLE_LIMIT] for t in samples)


def _join_entries(entries: list[dict] | None) -> str:
    """Flatten a portfolio/experience list into a readable string, tolerating unknown shapes."""
    parts: list[str] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            fields = [str(v) for v in entry.values() if isinstance(v, str | int | float) and str(v).strip()]
            if fields:
                parts.append(" — ".join(fields))
        elif isinstance(entry, str) and entry.strip():
            parts.append(entry.strip())
    return "; ".join(parts)


def _first_nonempty(values: list[str | None]) -> str:
    for value in values:
        if value and value.strip():
            return value
    return ""


def _clip(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "not set"
    return text if len(text) <= TEXT_FIELD_LIMIT else text[:TEXT_FIELD_LIMIT] + " […]"


def _gemini_schema(schema: dict) -> dict:
    """Gemini's response schema doesn't accept ``additionalProperties`` — drop it recursively."""
    out: dict = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if isinstance(value, dict):
            out[key] = _gemini_schema(value)
        elif isinstance(value, list):
            out[key] = [_gemini_schema(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out
