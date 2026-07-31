"""Deterministic guardrails around profile-skill recommendations."""

from __future__ import annotations

import asyncio

import pytest

from app.recommendation_engine.engine import (
    RecommendationEngineError,
    finalise_recommendation,
    recommend_profile_skills,
)
from app.recommendation_engine.models import ProfileEvidence
from app.recommendation_engine.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from app.recommendation_engine.providers import RecommendationProviderError


class TestPrompt:
    def test_includes_every_evidence_field_as_json(self):
        prompt = build_user_prompt(
            ProfileEvidence(
                summary="React specialist",
                account_skills=["React.js"],
                portfolio=[{"title": "Storefront"}],
                reviews=[{"feedback": "Excellent API work"}],
                experience=[{"title": "Software Engineer"}],
            )
        )
        for value in (
            "React specialist",
            "React.js",
            "Storefront",
            "Excellent API work",
            "Software Engineer",
        ):
            assert value in prompt
        assert "untrusted data" in SYSTEM_PROMPT
        assert set(RESPONSE_SCHEMA["properties"]) == {"existing_skills", "recommended_skills"}


class TestFinalise:
    def test_keeps_every_existing_skill_in_original_spelling(self):
        result = finalise_recommendation(
            {
                "existing_skills": [
                    {
                        "name": "react js",
                        "weight": 7,
                        "reason": "Several React portfolio projects.",
                        "evidence_sources": ["portfolio"],
                    }
                ],
                "recommended_skills": [],
            },
            ["React.js", "Python"],
        )
        assert [skill.name for skill in result.existing_skills] == ["React.js", "Python"]
        assert [skill.weight for skill in result.existing_skills] == [5, 1]
        assert result.existing_skills[1].evidence_sources == ("account_skills",)

    def test_filters_duplicates_and_recommendations_without_real_evidence(self):
        result = finalise_recommendation(
            {
                "existing_skills": [],
                "recommended_skills": [
                    {
                        "name": "node js",
                        "weight": 5,
                        "reason": "It appears in the listed skills.",
                        "evidence_sources": ["account_skills"],
                    },
                    {
                        "name": "PostgreSQL",
                        "weight": 4,
                        "reason": "Database design is described in a portfolio project.",
                        "evidence_sources": ["portfolio"],
                    },
                    {
                        "name": "postgresql",
                        "weight": 2,
                        "reason": "Repeated.",
                        "evidence_sources": ["experience"],
                    },
                ],
            },
            ["Node.js"],
        )
        assert [skill.name for skill in result.recommended_skills] == ["PostgreSQL"]
        assert result.recommended_skills[0].weight == 4

    def test_does_not_collapse_cplusplus_and_csharp(self):
        result = finalise_recommendation(
            {
                "existing_skills": [],
                "recommended_skills": [
                    {
                        "name": "C#",
                        "weight": 3,
                        "reason": "C# application is in the portfolio.",
                        "evidence_sources": ["portfolio"],
                    }
                ],
            },
            ["C++"],
        )
        assert [skill.name for skill in result.recommended_skills] == ["C#"]


class TestEngine:
    def test_sends_prompt_and_finalises_completion(self):
        received: dict[str, object] = {}

        async def fake_completion(system, prompt, schema, max_tokens):
            received.update(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens)
            return {
                "existing_skills": [
                    {
                        "name": "Python",
                        "weight": 5,
                        "reason": "Core language in experience.",
                        "evidence_sources": ["experience"],
                    }
                ],
                "recommended_skills": [
                    {
                        "name": "FastAPI",
                        "weight": 4,
                        "reason": "Portfolio describes FastAPI services.",
                        "evidence_sources": ["portfolio"],
                    }
                ],
            }

        result = asyncio.run(
            recommend_profile_skills(
                ProfileEvidence(
                    summary="Backend developer",
                    account_skills=["Python"],
                    portfolio=[{"description": "FastAPI service"}],
                ),
                completion=fake_completion,
            )
        )
        assert received["system"] == SYSTEM_PROMPT
        assert received["schema"] == RESPONSE_SCHEMA
        assert "FastAPI service" in received["prompt"]
        assert result.existing_skills[0].weight == 5
        assert [skill.name for skill in result.recommended_skills] == ["FastAPI"]

    def test_converts_provider_failure_to_engine_failure(self):
        async def failing_completion(*_args):
            raise RecommendationProviderError("provider unavailable")

        with pytest.raises(RecommendationEngineError, match="provider unavailable"):
            asyncio.run(recommend_profile_skills(ProfileEvidence(), completion=failing_completion))
