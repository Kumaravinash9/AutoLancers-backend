"""Structured LLM transport for the recommendation engine.

This uses the project's configured Gemini, Anthropic, or OpenAI-compatible provider while keeping
provider details out of the recommendation rules and tests.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class RecommendationProviderError(RuntimeError):
    """A configured provider could not produce a valid structured response."""


async def complete_recommendation(
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    """Use the configured LLM provider and return the parsed JSON object."""
    # Lazy import keeps deterministic recommendation validation usable in tooling that has not
    # installed the application's provider dependencies yet.
    from app.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise RecommendationProviderError("GEMINI_API_KEY is not set")
        return await _complete_with_gemini(system_prompt, user_prompt, schema, max_output_tokens)
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RecommendationProviderError("ANTHROPIC_API_KEY is not set")
        return await _complete_with_anthropic(system_prompt, user_prompt, schema, max_output_tokens)
    if settings.llm_provider == "nvidia":
        if not settings.nvidia_api_key:
            raise RecommendationProviderError("NVIDIA_API_KEY is not set")
        from app.services.llm import LLMError, complete_json

        try:
            result = await complete_json(
                system_prompt,
                user_prompt,
                schema,
                max_output_tokens,
                "profile_skill_recommendation",
            )
        except LLMError as exc:
            raise RecommendationProviderError(str(exc)) from exc
        return _expect_object(result["data"])
    raise RecommendationProviderError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r} — expected "
        "'gemini', 'anthropic' or 'nvidia'"
    )


async def _complete_with_gemini(
    system_prompt: str, user_prompt: str, schema: dict[str, Any], max_output_tokens: int
) -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                GEMINI_ENDPOINT.format(model=settings.gemini_model),
                params={"key": settings.gemini_api_key},
                json=payload,
                headers={"content-type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise RecommendationProviderError(f"Could not reach the Gemini API: {exc}") from exc

    if response.status_code >= 400:
        raise RecommendationProviderError(
            f"Gemini API returned {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        suffix = f" (blocked: {blocked})" if blocked else ""
        raise RecommendationProviderError(f"Gemini returned no candidates{suffix}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return _parse_text("".join(part.get("text", "") for part in parts).strip())


async def _complete_with_anthropic(
    system_prompt: str, user_prompt: str, schema: dict[str, Any], max_output_tokens: int
) -> dict[str, Any]:
    # Imported lazily so the Gemini path does not require the SDK at import time.
    import anthropic

    from app.config import get_settings

    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_output_tokens,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIStatusError as exc:
        raise RecommendationProviderError(
            f"Claude API returned {exc.status_code}: {exc.message}"
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise RecommendationProviderError(f"Could not reach Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise RecommendationProviderError("Claude declined to analyse the profile")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return _parse_text(text)


def _parse_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(_unfence(text))
    except json.JSONDecodeError as exc:
        raise RecommendationProviderError(f"Provider returned unparseable JSON: {exc}") from exc
    return _expect_object(value)


def _unfence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        return text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return text


def _expect_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecommendationProviderError("Provider response must be a JSON object")
    return value


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini rejects ``additionalProperties`` in response schemas."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if isinstance(value, dict):
            out[key] = _gemini_schema(value)
        elif isinstance(value, list):
            out[key] = [_gemini_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value
    return out
