"""Discovery-skill expansion: widen confirmed skills into the marketplace's tag vocabulary.

Freelancer's project search filters server-side by skill ID (``jobs[]``), and clients tag jobs with
whatever vocabulary they reach for — often broad umbrella categories ("Full Stack Development",
"Web Development", "API Integration") rather than a specialist's exact stack ("Next.js", "FastAPI").
A profile that lists only the specialist tags never even *sees* those jobs: the search doesn't
return them, so semantic matching — which only ranks what's fetched — can't help.

This asks an LLM to broaden the freelancer's confirmed skills into the wider set of marketplace tags
a client might plausibly attach to a job this freelancer could do. The result feeds discovery only:
the broadened tags are resolved to skill IDs and unioned into the search filter (see
``pipeline._resolve_search_skill_ids``). They never touch matching or scoring — widening *what is
fetched* must not change *how good* a job is judged to be, or a broad tag would inflate scores.

This is the opposite precision trade-off from ``matching``: false positives here are cheap (a
loosely-related tag just fetches a few extra jobs, which scoring ranks down), so it errs toward
recall. Same provider switch and models as the other LLM services.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import get_settings
from app.services.llm import LLMError, complete_json

logger = logging.getLogger(__name__)

# A tag list is short; the headroom is for the models' thinking, which draws from the same budget.
MAX_OUTPUT_TOKENS = 2000

# Never widen the search filter past this many extra tags — beyond it, recall has turned into noise.
MAX_EXPANSION_TERMS = 30

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SYSTEM_PROMPT = (
    "# Role\n"
    "You are a discovery assistant for a freelancing marketplace. You widen a freelancer's skills "
    "into the vocabulary clients actually use to tag jobs.\n"
    "\n"
    "# Task\n"
    "Given a freelancer's confirmed skills, list the marketplace skill tags a client might attach "
    "to a job this freelancer could do — including the broader umbrella categories that specific "
    "skills roll up into.\n"
    "\n"
    "# Constraints\n"
    "- Include adjacent and umbrella tags a client would realistically use (e.g. 'Next.js' -> "
    "'React.js', 'JavaScript', 'Web Development', 'Full Stack Development'; 'FastAPI' -> 'Python', "
    "'API Development', 'Backend Development').\n"
    "- Stay within what this freelancer could plausibly deliver — widen the vocabulary, "
    "don't cross into unrelated fields (a web developer is not a 'Video Editing' tag).\n"
    "- Use canonical marketplace tag names, not vague phrases.\n"
    "- It is fine to be generous: this only decides which jobs are fetched, not how they are "
    "scored.\n"
    "\n"
    "# Result\n"
    "A recall-oriented set of marketplace tags that surfaces generically-tagged jobs this "
    "freelancer would want to see, without drifting into work they don't do.\n"
    "\n"
    "# Output\n"
    'Return a JSON object of the form {"tags": ["tag", ...]} — canonical marketplace skill tag '
    "names as strings. Return an empty array if the skills give nothing to widen.\n"
)

_SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["tags"],
    "additionalProperties": False,
}


class SkillExpansionError(RuntimeError):
    pass


async def expand_skill_terms(names: list[str]) -> list[str]:
    """Broaden confirmed skill ``names`` into a wider set of marketplace tags for discovery.

    Returns extra tag names (the originals are unioned in by the caller). Returns ``[]`` when the
    feature is off, no API key is configured, or there are no skills — expansion is optional, so
    those are "no widening", not errors. Raises ``SkillExpansionError`` on a genuine call failure
    so the caller can log and fall back to the listed skills alone.
    """
    settings = get_settings()
    if not settings.skill_expansion_enabled or not names:
        return []

    message = "Confirmed skills:\n" + ", ".join(names)

    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            return []  # no key → no widening, not an error
        raw = await _expand_with_gemini(message)
    elif settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            return []
        raw = await _expand_with_anthropic(message)
    elif settings.llm_provider == "nvidia":
        if not settings.nvidia_api_key:
            return []
        try:
            result = await complete_json(
                _SYSTEM_PROMPT, message, _SCHEMA, MAX_OUTPUT_TOKENS, "skill_expansion"
            )
        except LLMError as exc:
            raise SkillExpansionError(str(exc)) from exc
        raw = _parse(json.dumps(result["data"]))
    else:
        raise SkillExpansionError(
            f"Unknown LLM_PROVIDER {settings.llm_provider!r} — expected "
            "'gemini', 'anthropic' or 'nvidia'"
        )

    return _finalise(raw, listed=names)


async def _expand_with_gemini(message: str) -> list[str]:
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
        raise SkillExpansionError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        # The key rides in the query string, so keep it out of the error text.
        raise SkillExpansionError(
            f"Gemini API returned {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise SkillExpansionError(
            f"Gemini returned no candidates{f' (blocked: {blocked})' if blocked else ''}"
        )

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    return _parse(text)


async def _expand_with_anthropic(message: str) -> list[str]:
    settings = get_settings()

    # Imported lazily so the Gemini path doesn't require the SDK to be installed.
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            output_config={
                "effort": "low",  # vocabulary widening, not deep reasoning
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
        raise SkillExpansionError(f"Claude API returned {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise SkillExpansionError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise SkillExpansionError("Claude declined to expand these skills")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return _parse(text)


def _parse(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SkillExpansionError(f"Expansion response was not valid JSON: {text[:200]!r}") from exc
    tags = data.get("tags")
    if not isinstance(tags, list):
        raise SkillExpansionError("Expansion response had no 'tags' list")
    return tags


def _finalise(raw: list[str], listed: list[str]) -> list[str]:
    """Normalise, drop the originals and duplicates, and cap the count."""
    already = {n.strip().lower() for n in listed}
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw:
        name = str(tag or "").strip()
        key = name.lower()
        if not name or key in already or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_EXPANSION_TERMS:
            break
    return out


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
