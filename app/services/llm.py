"""One OpenAI-compatible client, shared by every service that needs structured output.

Each service already owns its prompt and its schema; what they were duplicating was the transport —
build a request, post it, check the status, pull the text out, parse it. That duplication is why
adding a third provider meant editing four files. This is the third provider, written once.

Deliberately raw ``httpx`` rather than the ``openai`` SDK: the Gemini path is already raw httpx, and
one HTTP call does not justify a dependency whose only job here would be to build the same JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def loads(raw: str) -> Any:
    """Parse JSON that may arrive wrapped in a markdown fence."""
    text = (raw or "").strip()
    if not text:
        raise LLMError("The model returned an empty result.")
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"The model returned unparseable JSON: {exc}") from exc


async def complete_json(
    system: str,
    message: str,
    schema: dict[str, Any],
    max_tokens: int,
    schema_name: str = "result",
) -> dict[str, Any]:
    """Ask an OpenAI-compatible endpoint for JSON matching ``schema``.

    Returns ``{"data", "model", "input_tokens", "output_tokens"}``. Raises ``LLMError`` on anything
    that would otherwise hand the caller a partial or invented answer.
    """
    settings = get_settings()
    if not settings.nvidia_api_key:
        raise LLMError("NVIDIA_API_KEY is not set.")

    payload = {
        "model": settings.nvidia_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
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
        raise LLMError(f"Could not reach {settings.nvidia_base_url}: {exc}") from exc

    if response.status_code >= 400:
        # The key rides in a header, but the body can still echo request detail — keep it short.
        raise LLMError(f"Provider returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("Provider returned no choices.")

    if choices[0].get("finish_reason") == "length":
        # Truncated JSON either fails to parse or, worse, parses as a valid partial object.
        raise LLMError("Provider hit the output limit before finishing.")

    usage = data.get("usage") or {}
    return {
        "data": loads((choices[0].get("message") or {}).get("content") or ""),
        "model": data.get("model") or settings.nvidia_model,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
