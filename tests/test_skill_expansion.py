"""Discovery-skill expansion: normalisation, optionality, and provider selection.

Expansion is recall-first and optional — off, unconfigured, or empty must all mean "no widening",
not an error — and whatever the model returns is cleaned before it widens the search.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services import skill_expansion
from app.services.skill_expansion import (
    MAX_EXPANSION_TERMS,
    SkillExpansionError,
    _finalise,
    _gemini_schema,
    _parse,
    expand_skill_terms,
)


@pytest.fixture
def settings(monkeypatch):
    def apply(**overrides):
        values = dict(gemini_api_key="", anthropic_api_key="")
        values.update(overrides)
        replacement = Settings(**values)
        monkeypatch.setattr(skill_expansion, "get_settings", lambda: replacement)
        return replacement

    return apply


class TestFinalise:
    def test_drops_the_originals_case_insensitively(self):
        out = _finalise(["react.js", "Web Development"], listed=["React.js"])
        assert out == ["Web Development"]

    def test_dedupes(self):
        assert _finalise(["Node.js", "node.js", "NODE.JS"], listed=[]) == ["Node.js"]

    def test_skips_blanks(self):
        assert _finalise(["  ", "Go"], listed=[]) == ["Go"]

    def test_caps_the_count(self):
        out = _finalise([f"tag-{i}" for i in range(MAX_EXPANSION_TERMS + 10)], listed=[])
        assert len(out) == MAX_EXPANSION_TERMS


class TestParse:
    def test_rejects_non_json(self):
        with pytest.raises(SkillExpansionError):
            _parse("not json")

    def test_rejects_missing_list(self):
        with pytest.raises(SkillExpansionError):
            _parse('{"nope": 1}')

    def test_reads_the_tags_list(self):
        assert _parse('{"tags": ["Web Development"]}') == ["Web Development"]


class TestOptionality:
    async def test_disabled_returns_empty(self, settings):
        settings(skill_expansion_enabled=False, llm_provider="gemini", gemini_api_key="k")
        assert await expand_skill_terms(["React"]) == []

    async def test_no_names_returns_empty(self, settings):
        settings(skill_expansion_enabled=True)
        assert await expand_skill_terms([]) == []

    async def test_missing_key_returns_empty_not_error(self, settings):
        # Unlike the user-initiated suggest flow, expansion runs in the poller — a missing key is
        # "no widening", never a raised error that would disrupt a cycle.
        settings(skill_expansion_enabled=True, llm_provider="gemini")
        assert await expand_skill_terms(["React"]) == []


class TestProviderSelection:
    async def test_unknown_provider_is_a_clear_error(self, settings):
        settings(skill_expansion_enabled=True, llm_provider="mystery")
        with pytest.raises(SkillExpansionError, match="Unknown LLM_PROVIDER"):
            await expand_skill_terms(["React"])


class TestGeminiSchema:
    def test_strips_additional_properties(self):
        cleaned = _gemini_schema(skill_expansion._SCHEMA)
        assert "additionalProperties" not in cleaned
        assert cleaned["properties"]["tags"]["items"] == {"type": "string"}
