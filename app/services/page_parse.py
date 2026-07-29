"""Turn a scraped page into structured fields with an LLM.

Selectors are the cheap path and stay the default: free, instant, and deterministic. But a
marketplace can move its markup overnight, and the profile page in particular carries its structure
in headings rather than in anything machine-readable — one redesign and half the fields come back
empty. This is the fallback for that: hand the visible text to a model with a strict schema and let
it do the reading.

The trade is real and worth stating. It costs tokens per page, takes seconds rather than
milliseconds, and a model can fabricate a plausible value where a selector would simply return
nothing. So the schema forbids invention, every field is nullable, and the caller is told which
path produced the answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Profiles run long. The models think before answering and that draws from the same budget, so
# sizing this for the JSON alone returns an empty response.
MAX_OUTPUT_TOKENS = 8000

# A listing page is up to sixty jobs in one answer, and the run is aborted rather than truncated
# when the budget is hit — so the list kinds get their own ceiling instead of failing on page two.
MAX_OUTPUT_TOKENS_LIST = 32_000

# Enough of the page to cover a full profile; beyond this is footer boilerplate.
MAX_INPUT_CHARS = 30_000

# A listing page of sixty jobs is several times a profile's length, and truncating the input drops
# the last jobs silently — they simply never appear in the answer.
MAX_INPUT_CHARS_LIST = 120_000

_SYSTEM_PROMPT = (
    "# Role\n"
    "You extract structured data from the visible text of a freelancing marketplace page.\n"
    "\n"
    "# Constraints\n"
    "- Report only what the text states. Never infer, complete, or guess a value.\n"
    "- If a field is not present, return null for it (or an empty list). An absent field is a "
    "correct answer; a plausible invention is not.\n"
    "- Ignore navigation, cookie banners, upsells, tooltips and footer links. They are page "
    "furniture, not content about this person or job.\n"
    "- Numbers must be plain: '$40K' becomes 40000, '1,240' becomes 1240, '98%' becomes 98.\n"
    "- Copy names, titles and skills exactly as written. Do not translate or tidy them.\n"
)

_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "display_name": {"type": "string", "nullable": True},
        "tagline": {"type": "string", "nullable": True},
        "summary": {"type": "string", "nullable": True},
        "country": {"type": "string", "nullable": True},
        "city": {"type": "string", "nullable": True},
        "availability": {"type": "string", "nullable": True},
        "hourly_rate": {"type": "number", "nullable": True},
        "currency": {"type": "string", "nullable": True},
        "total_earnings": {"type": "number", "nullable": True},
        "job_success": {"type": "number", "nullable": True},
        "rating": {"type": "number", "nullable": True},
        "total_reviews": {"type": "number", "nullable": True},
        "total_jobs": {"type": "number", "nullable": True},
        "total_hours": {"type": "number", "nullable": True},
        "languages": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "portfolio": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
        "work_history": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "feedback": {"type": "string", "nullable": True},
                    "rating": {"type": "number", "nullable": True},
                },
                "required": ["title"],
            },
        },
        "employment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string", "nullable": True},
                    "period": {"type": "string", "nullable": True},
                },
                "required": ["title"],
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "school": {"type": "string"},
                    "degree": {"type": "string", "nullable": True},
                    "period": {"type": "string", "nullable": True},
                },
                "required": ["school"],
            },
        },
        "certifications": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["display_name", "skills"],
}

_JOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "nullable": True},
        "description": {"type": "string", "nullable": True},
        "work_type": {"type": "string", "nullable": True},
        "budget_min": {"type": "number", "nullable": True},
        "budget_max": {"type": "number", "nullable": True},
        "currency": {"type": "string", "nullable": True},
        "experience_level": {"type": "string", "nullable": True},
        "project_length": {"type": "string", "nullable": True},
        "proposal_count": {"type": "number", "nullable": True},
        "interviewing": {"type": "number", "nullable": True},
        "category": {"type": "string", "nullable": True},
        "skills": {"type": "array", "items": {"type": "string"}},
        "client": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "nullable": True},
                "rating": {"type": "number", "nullable": True},
                "reviews": {"type": "number", "nullable": True},
                "total_spent": {"type": "number", "nullable": True},
                "total_hires": {"type": "number", "nullable": True},
                "member_since": {"type": "string", "nullable": True},
                "payment_verified": {"type": "boolean", "nullable": True},
            },
        },
    },
    "required": ["title", "skills"],
}


# The four ``projects`` columns the job schema above has no field for, added rather than duplicated
# so the list schema below and the single-job schema cannot drift apart.
_JOB_SCHEMA["properties"].update(
    {
        "required_skills": {"type": "array", "items": {"type": "string"}},
        "min_budget": {"type": "number", "nullable": True},
        "max_budget": {"type": "number", "nullable": True},
        "posted_at": {"type": "string", "nullable": True},
        "bid_count": {"type": "number", "nullable": True},
        "client_name": {"type": "string", "nullable": True},
        "client_rating": {"type": "number", "nullable": True},
        "client_country": {"type": "string", "nullable": True},
        "client_reviews_count": {"type": "number", "nullable": True},
        "client_total_spent": {"type": "number", "nullable": True},
        "client_payment_verified": {"type": "boolean", "nullable": True},
    }
)

# A listing page holds many jobs, so the list kinds return an array under ``items``. ``title`` stays
# required on every item because it is the only key available for matching a reading back to the
# scraped row it describes — ids live in hrefs, and the model only ever sees the visible text.
_JOBS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": _JOB_SCHEMA}},
    "required": ["items"],
}

_PROPOSALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_title": {"type": "string"},
                    "external_bid_id": {"type": "string", "nullable": True},
                    # Mapped onto this app's vocabulary, because every marketplace words it
                    # differently: PeoplePerHour's "Awaiting response" is SUBMITTED, its
                    # "Not selected" is REJECTED.
                    "status": {
                        "type": "string",
                        "nullable": True,
                        "enum": ["DRAFT", "SUBMITTED", "ACCEPTED", "REJECTED", "WITHDRAWN"],
                    },
                    "bid_amount": {"type": "number", "nullable": True},
                    "currency": {"type": "string", "nullable": True},
                    "estimated_days": {"type": "number", "nullable": True},
                    "submitted_at": {"type": "string", "nullable": True},
                    "client_name": {"type": "string", "nullable": True},
                },
                "required": ["project_title"],
            },
        }
    },
    "required": ["items"],
}

_CONTRACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "client_name": {"type": "string", "nullable": True},
                    "work_type": {"type": "string", "nullable": True},
                    "status": {
                        "type": "string",
                        "nullable": True,
                        "enum": ["ACTIVE", "PAUSED", "ENDED"],
                    },
                    "rate": {"type": "number", "nullable": True},
                    "currency": {"type": "string", "nullable": True},
                    "earned_to_date": {"type": "number", "nullable": True},
                    "hours_logged": {"type": "number", "nullable": True},
                    "started_at": {"type": "string", "nullable": True},
                    "ended_at": {"type": "string", "nullable": True},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["items"],
}

SCHEMAS = {
    "profile": _PROFILE_SCHEMA,
    "job": _JOB_SCHEMA,
    "jobs": _JOBS_SCHEMA,
    "proposals": _PROPOSALS_SCHEMA,
    "contracts": _CONTRACTS_SCHEMA,
}

# Every kind whose result is a list of items rather than one object.
LIST_KINDS = frozenset({"jobs", "proposals", "contracts"})

# What to ask for, per kind. The list kinds need "every one you can see, and no others" said
# explicitly: a model handed an array to fill will pad it if the instruction only implies a count.
_INSTRUCTIONS = {
    "profile": "Extract the freelancer profile described by this marketplace page.",
    "job": "Extract the job described by this marketplace page.",
    "jobs": (
        "Extract every job listed on this page, one item per job, in the order they appear. "
        "Copy each title exactly as written — the title is how each item is matched back to the "
        "listing it came from. Do not add a job that is not on the page, and do not merge two."
    ),
    "proposals": (
        "Extract every proposal or bid listed on this page, one item per row. `project_title` is "
        "the job the proposal was for. Map the marketplace's own wording onto the status values: "
        "awaiting a reply is SUBMITTED, hired or awarded is ACCEPTED, not selected or declined is "
        "REJECTED, withdrawn by the freelancer is WITHDRAWN, unsent is DRAFT. Never return the "
        "proposal's own text — a listing shows only a truncated preview of it."
    ),
    "contracts": (
        "Extract every contract, order or ongoing project listed on this page, one item per row. "
        "A contract still being worked is ACTIVE, one on hold is PAUSED, a finished or closed one "
        "is ENDED. Money already paid goes in `earned_to_date`, the agreed rate or price in `rate`."
    ),
}


class PageParseError(RuntimeError):
    pass


# The model returns whatever the page showed — "$" for a rate in dollars. Downstream comparisons
# are against an ISO code, and "$" would silently fail to match USD.
_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY"}


def _normalise(fields: dict[str, Any]) -> None:
    """In-place tidy-ups the schema can't express.

    Walks into ``items`` for the list kinds: a per-row currency symbol needs the same treatment as a
    top-level one, and skipping it would leave "$" sitting in a column compared against "USD".
    """
    currency = fields.get("currency")
    if isinstance(currency, str):
        fields["currency"] = _SYMBOLS.get(currency.strip(), currency.strip().upper() or None)

    for item in fields.get("items") or []:
        if isinstance(item, dict):
            _normalise(item)


async def parse_page(kind: str, text: str) -> dict[str, Any]:
    """Extract fields of ``kind`` from page text.

    ``kind`` is one of :data:`SCHEMAS` — a single ``profile`` or ``job``, or one of the
    :data:`LIST_KINDS` (``jobs``, ``proposals``, ``contracts``) whose ``fields`` carry an ``items``
    array instead of one object.
    """
    schema = SCHEMAS.get(kind)
    if schema is None:
        raise PageParseError(f"Unknown page kind {kind!r}")

    body = (text or "").strip()
    if len(body) < 40:
        raise PageParseError("The page text was too short to read anything from.")

    settings = get_settings()
    listing = kind in LIST_KINDS
    input_cap = MAX_INPUT_CHARS_LIST if listing else MAX_INPUT_CHARS
    output_cap = MAX_OUTPUT_TOKENS_LIST if listing else MAX_OUTPUT_TOKENS
    truncated = body[:input_cap]
    message = (
        f"{_INSTRUCTIONS.get(kind, f'Extract the {kind} described by this marketplace page.')}\n\n"
        f"<page_text>\n{truncated}\n</page_text>"
    )

    provider = (settings.llm_provider or "gemini").lower()
    if provider == "nvidia":
        result = await _parse_openai_compatible(message, schema, output_cap)
    elif provider in ("gemini", "anthropic"):
        # Anthropic has no structured-output mode here yet, and Gemini's is the one this was built
        # against — so it handles both rather than silently doing something different.
        result = await _parse_gemini(message, schema, output_cap)
    else:
        raise PageParseError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")

    _normalise(result["fields"])
    result["truncated_input"] = len(body) > input_cap
    return result


async def _parse_openai_compatible(
    message: str, schema: dict[str, Any], max_output_tokens: int = MAX_OUTPUT_TOKENS
) -> dict[str, Any]:
    """Any OpenAI-compatible chat endpoint that honours ``response_format: json_schema``."""
    settings = get_settings()
    if not settings.nvidia_api_key:
        raise PageParseError("NVIDIA_API_KEY is not set.")

    payload = {
        "model": settings.nvidia_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": schema},
        },
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{settings.nvidia_base_url.rstrip('/')}/chat/completions",
                headers={
                    "authorization": f"Bearer {settings.nvidia_api_key}",
                    "content-type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise PageParseError(f"Could not reach {settings.nvidia_base_url}: {exc}") from exc

    if response.status_code >= 400:
        raise PageParseError(f"Provider returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise PageParseError("Provider returned no choices.")

    finish = choices[0].get("finish_reason")
    raw = (choices[0].get("message") or {}).get("content") or ""
    if finish == "length":
        # Truncated JSON either fails to parse or, worse, parses as a valid partial object.
        raise PageParseError("Provider hit the output limit before finishing.")

    usage = data.get("usage") or {}
    return {
        "fields": _loads(raw),
        "model": data.get("model") or settings.nvidia_model,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


def _loads(raw: str) -> dict[str, Any]:
    """Parse JSON that may arrive wrapped in a markdown fence."""
    text = (raw or "").strip()
    if not text:
        raise PageParseError("The model returned an empty result.")
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PageParseError(f"The model returned unparseable JSON: {exc}") from exc


async def _parse_gemini(
    message: str, schema: dict[str, Any], max_output_tokens: int = MAX_OUTPUT_TOKENS
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise PageParseError("GEMINI_API_KEY is not set.")

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": message}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                url,
                params={"key": settings.gemini_api_key},
                json=payload,
                headers={"content-type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise PageParseError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        # The key rides in the query string, so keep it out of the error text.
        raise PageParseError(f"Gemini API returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise PageParseError(
            f"Gemini returned nothing{f' (blocked: {blocked})' if blocked else ''}"
        )

    finish = candidates[0].get("finishReason")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(part.get("text", "") for part in parts).strip()

    if not raw:
        raise PageParseError(f"Gemini returned an empty result (finishReason={finish}).")
    if finish == "MAX_TOKENS":
        # Truncated JSON parses as garbage or, worse, as a valid but partial object.
        raise PageParseError("Gemini hit the output limit before finishing.")

    usage = data.get("usageMetadata") or {}
    return {
        "fields": _loads(raw),
        "model": data.get("modelVersion") or settings.gemini_model,
        "input_tokens": usage.get("promptTokenCount"),
        # Thinking is billed but never returned; omitting it would understate cost per page.
        "output_tokens": (usage.get("candidatesTokenCount") or 0)
        + (usage.get("thoughtsTokenCount") or 0),
    }
