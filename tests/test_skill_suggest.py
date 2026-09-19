"""Skill suggestion: evidence assembly, provider selection, and result normalisation.

Generation quality needs a live key and real evidence; these cover the deterministic parts — that
a bad list can't crash the endpoint and that a model's guess is cleaned before it's stored.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.db.models import FreelancerProfile
from app.services import skill_suggest
from app.services.skill_suggest import (
    MAX_SUGGESTIONS,
    SkillSuggestError,
    _build_user_message,
    _finalise,
    _gemini_schema,
    _parse,
    suggest_skills,
)


@pytest.fixture
def settings(monkeypatch):
    def apply(**overrides):
        values = dict(gemini_api_key="", anthropic_api_key="")
        values.update(overrides)
        replacement = Settings(**values)
        monkeypatch.setattr(skill_suggest, "get_settings", lambda: replacement)
        return replacement

    return apply


class TestFinalise:
    def test_drops_already_listed_case_insensitively(self):
        raw = [{"name": "React.js", "weight": 4, "reason": "r", "source": "portfolio"}]
        out = _finalise(raw, already_listed=[{"name": "react.js", "weight": 5}])
        assert out == []

    def test_dedupes_within_a_batch(self):
        raw = [
            {"name": "Node.js", "weight": 4, "reason": "a", "source": "experience"},
            {"name": "node.js", "weight": 2, "reason": "b", "source": "proposals"},
        ]
        out = _finalise(raw, already_listed=[])
        assert [s.name for s in out] == ["Node.js"]

    def test_clamps_and_coerces_weight(self):
        raw = [
            {"name": "A", "weight": 99, "reason": "", "source": ""},
            {"name": "B", "weight": "not a number", "reason": "", "source": ""},
            {"name": "C", "weight": 0, "reason": "", "source": ""},
        ]
        out = _finalise(raw, already_listed=[])
        assert [s.weight for s in out] == [5, 1, 1]

    def test_skips_blank_names(self):
        raw = [{"name": "   ", "weight": 3, "reason": "", "source": ""}]
        assert _finalise(raw, already_listed=[]) == []

    def test_caps_the_number_of_suggestions(self):
        raw = [
            {"name": f"skill-{i}", "weight": 3, "reason": "", "source": ""}
            for i in range(MAX_SUGGESTIONS + 5)
        ]
        assert len(_finalise(raw, already_listed=[])) == MAX_SUGGESTIONS


class TestParse:
    def test_rejects_non_json(self):
        with pytest.raises(SkillSuggestError):
            _parse("not json")

    def test_rejects_missing_list(self):
        with pytest.raises(SkillSuggestError):
            _parse('{"nope": 1}')

    def test_reads_the_suggestions_list(self):
        assert _parse('{"suggestions": [{"name": "Go"}]}') == [{"name": "Go"}]


class TestUserMessage:
    def _profile(self, **overrides):
        defaults = dict(
            headline="Full-stack dev",
            bio="I build React and Node apps.",
            skills=[{"name": "React", "weight": 5}],
            portfolio=[{"title": "Shop", "detail": "Next.js storefront"}],
            experience=[],
        )
        defaults.update(overrides)
        return FreelancerProfile(**defaults)

    def test_includes_listed_skills_and_evidence(self):
        profile = self._profile(account_skills=["PHP", "WordPress"], summary="Freelance builder")
        msg = _build_user_message(profile, ["Hi, I can build your store in Next.js."])
        assert "React" in msg  # already-listed
        assert "PHP" in msg and "WordPress" in msg  # account skills (now on the profile)
        assert "Next.js storefront" in msg  # portfolio
        assert "build your store" in msg  # proposal sample

    def test_tolerates_empty_evidence(self):
        msg = _build_user_message(self._profile(skills=[], portfolio=[], bio=""), [])
        assert "none" in msg  # no skills listed, no account skills, no proposals


class TestProviderSelection:
    async def test_unknown_provider_is_a_clear_error(self, settings):
        settings(llm_provider="mystery")
        with pytest.raises(SkillSuggestError, match="Unknown LLM_PROVIDER"):
            await suggest_skills(FreelancerProfile(skills=[]), [])

    async def test_missing_gemini_key_is_a_clear_error(self, settings):
        settings(llm_provider="gemini")
        with pytest.raises(SkillSuggestError, match="GEMINI_API_KEY"):
            await suggest_skills(FreelancerProfile(skills=[]), [])

    async def test_missing_anthropic_key_is_a_clear_error(self, settings):
        settings(llm_provider="anthropic")
        with pytest.raises(SkillSuggestError, match="ANTHROPIC_API_KEY"):
            await suggest_skills(FreelancerProfile(skills=[]), [])


class TestGeminiSchema:
    def test_strips_additional_properties_recursively(self):
        cleaned = _gemini_schema(skill_suggest._SCHEMA)
        assert "additionalProperties" not in cleaned
        # ...including inside the array items, which is where a stray one would slip through.
        item = cleaned["properties"]["suggestions"]["items"]
        assert "additionalProperties" not in item
        assert set(item["properties"]) == {"name", "weight", "reason", "source"}
