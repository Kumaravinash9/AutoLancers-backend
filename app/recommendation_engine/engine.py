"""Evidence-grounded profile-skill recommendation orchestration."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from app.recommendation_engine.models import (
    ProfileEvidence,
    ProfileSkillRecommendation,
    WeightedSkill,
)
from app.recommendation_engine.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from app.recommendation_engine.providers import RecommendationProviderError, complete_recommendation


MAX_OUTPUT_TOKENS = 4_000
MAX_RECOMMENDED_SKILLS = 12
_SOURCES = frozenset({"summary", "account_skills", "portfolio", "reviews", "experience"})
_RECOMMENDATION_SOURCES = _SOURCES - {"account_skills"}

StructuredCompletion = Callable[
    [str, str, dict[str, Any], int], Awaitable[Mapping[str, Any]]
]


class RecommendationEngineError(RuntimeError):
    """The engine could not obtain or safely use a recommendation result."""


async def recommend_profile_skills(
    evidence: ProfileEvidence,
    *,
    completion: StructuredCompletion | None = None,
) -> ProfileSkillRecommendation:
    """Weight listed skills and identify evidence-backed additions.

    The optional ``completion`` seam is primarily for tests and alternative enterprise LLM clients.
    It receives ``(system_prompt, user_prompt, json_schema, max_output_tokens)`` and must return a
    decoded JSON object matching the prompt schema.
    """
    user_prompt = build_user_prompt(evidence)
    try:
        raw = dict(
            await (completion or _configured_completion)(
                SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA, MAX_OUTPUT_TOKENS
            )
        )
    except RecommendationProviderError as exc:
        raise RecommendationEngineError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise RecommendationEngineError("LLM completion returned an invalid result") from exc

    return finalise_recommendation(raw, evidence.account_skills)


async def _configured_completion(
    system_prompt: str, user_prompt: str, schema: dict[str, Any], max_tokens: int
) -> Mapping[str, Any]:
    return await complete_recommendation(system_prompt, user_prompt, schema, max_tokens)


def finalise_recommendation(
    raw: Mapping[str, Any], account_skills: Sequence[str]
) -> ProfileSkillRecommendation:
    """Enforce list membership, de-duplication, evidence requirements, and weight bounds.

    An LLM cannot be allowed to silently drop one of the user's existing skills.  A missing item is
    retained with the lowest weight. Suggestions without a source beyond the account's own skill
    list are discarded.
    """
    listed = _unique_names(account_skills)
    raw_existing = _index_by_skill(raw.get("existing_skills"))
    existing: list[WeightedSkill] = []
    for name in listed:
        item = raw_existing.get(_skill_key(name))
        existing.append(
            _to_weighted_skill(
                item,
                fallback_name=name,
                fallback_sources=("account_skills",),
            )
        )

    listed_keys = {_skill_key(name) for name in listed}
    recommended: list[WeightedSkill] = []
    seen = set(listed_keys)
    for item in _as_item_list(raw.get("recommended_skills")):
        name = _item_name(item)
        key = _skill_key(name)
        if not name or not key or key in seen:
            continue
        skill = _to_weighted_skill(
            item,
            fallback_name=name,
            fallback_sources=(),
        )
        has_external_evidence = set(skill.evidence_sources).intersection(_RECOMMENDATION_SOURCES)
        if not has_external_evidence:
            continue
        seen.add(key)
        recommended.append(skill)
        if len(recommended) == MAX_RECOMMENDED_SKILLS:
            break

    return ProfileSkillRecommendation(tuple(existing), tuple(recommended))


def _index_by_skill(value: object) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in _as_item_list(value):
        name = _item_name(item)
        key = _skill_key(name)
        if key and key not in indexed:
            indexed[key] = item
    return indexed


def _as_item_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _item_name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or "").strip()


def _to_weighted_skill(
    item: Mapping[str, Any] | None,
    *,
    fallback_name: str,
    fallback_sources: tuple[str, ...],
) -> WeightedSkill:
    if item is None:
        return WeightedSkill(fallback_name, 1, fallback_sources)
    sources = _sources(item.get("evidence_sources")) or fallback_sources
    return WeightedSkill(
        name=fallback_name,
        weight=_weight(item.get("weight")),
        evidence_sources=sources,
    )


def _weight(value: object) -> int:
    """Clamp the LLM's weight to the supported 1–5 range."""
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 1


def _sources(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for source in value:
        name = str(source or "").strip().lower()
        if name in _SOURCES and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _unique_names(skills: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in skills:
        name = str(value or "").strip()
        key = _skill_key(name)
        if name and key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _skill_key(name: str) -> str:
    """Match ordinary spelling variants without collapsing meaningful symbols such as C++/C#."""
    return re.sub(r"[\s._-]+", "", name).casefold()
