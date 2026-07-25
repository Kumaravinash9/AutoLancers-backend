"""Drafting: prompt assembly, provider selection, and failure handling.

These cover the paths that must not take the poller down. Generation quality itself needs a live
API key and a real posting — see the drafting check in the README's verification notes.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services import drafting
from app.services.drafting import DraftingError, _budget_line, _build_user_message
from tests.test_scoring import make_job, make_profile


@pytest.fixture
def settings(monkeypatch):
    """Swap in throwaway settings so tests never depend on the developer's .env."""

    def apply(**overrides):
        values = dict(llm_provider="gemini", gemini_api_key="", anthropic_api_key="")
        values.update(overrides)
        replacement = Settings(**values)
        monkeypatch.setattr(drafting, "get_settings", lambda: replacement)
        return replacement

    return apply


class TestPromptAssembly:
    def test_includes_profile_and_post(self):
        message = _build_user_message(make_job(), make_profile())
        assert "<freelancer_profile>" in message
        assert "<job_post>" in message
        assert "InnoAI Labs" in message
        assert "Next.js dashboard" in message

    def test_truncates_a_very_long_description(self):
        job = make_job(description="x" * 20_000)
        message = _build_user_message(job, make_profile())
        assert "[description truncated]" in message
        assert len(message) < 20_000

    def test_proposal_notes_are_passed_through(self):
        profile = make_profile(proposal_notes="15 days free post-delivery bug support.")
        assert "15 days free post-delivery" in _build_user_message(make_job(), profile)

    def test_budget_line_handles_missing_budget(self):
        assert _budget_line(make_job(budget_min=None, budget_max=None)) == "not stated"

    def test_budget_line_handles_one_sided_budget(self):
        assert "900" in _budget_line(make_job(budget_min=None, budget_max=900.0))


class TestProviderSelection:
    async def test_unknown_provider_is_a_clear_error(self, settings):
        settings(llm_provider="llama")
        with pytest.raises(DraftingError, match="Unknown LLM_PROVIDER"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_missing_gemini_key_is_a_clear_error(self, settings):
        settings(llm_provider="gemini", gemini_api_key="")
        with pytest.raises(DraftingError, match="GEMINI_API_KEY"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_missing_anthropic_key_is_a_clear_error(self, settings):
        settings(llm_provider="anthropic", anthropic_api_key="")
        with pytest.raises(DraftingError, match="ANTHROPIC_API_KEY"):
            await drafting.draft_proposal(make_job(), make_profile())


class TestGeminiResponseHandling:
    """Gemini reports failures as a 200 with an unusable body, so these are not hypothetical."""

    async def test_empty_text_raises_rather_than_saving_a_blank_draft(self, settings, monkeypatch):
        settings(gemini_api_key="test-key")
        _stub_gemini(
            monkeypatch,
            {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]},
        )
        with pytest.raises(DraftingError, match="empty draft"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_blocked_prompt_reports_the_reason(self, settings, monkeypatch):
        settings(gemini_api_key="test-key")
        _stub_gemini(monkeypatch, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
        with pytest.raises(DraftingError, match="SAFETY"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_truncated_draft_is_rejected(self, settings, monkeypatch):
        settings(gemini_api_key="test-key")
        _stub_gemini(
            monkeypatch,
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Hi there, I can hel"}]},
                        "finishReason": "MAX_TOKENS",
                    }
                ]
            },
        )
        with pytest.raises(DraftingError, match="output limit"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_thinking_tokens_are_counted_in_cost(self, settings, monkeypatch):
        """Thinking is billed but never returned — omitting it would understate cost per bid."""
        settings(gemini_api_key="test-key")
        _stub_gemini(
            monkeypatch,
            {
                "candidates": [
                    {"content": {"parts": [{"text": "A proposal."}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 200,
                    "thoughtsTokenCount": 1200,
                },
                "modelVersion": "gemini-3.6-flash",
            },
        )
        draft = await drafting.draft_proposal(make_job(), make_profile())
        assert draft.output_tokens == 1400
        assert draft.input_tokens == 900

    async def test_http_error_is_wrapped(self, settings, monkeypatch):
        settings(gemini_api_key="test-key")
        _stub_gemini(monkeypatch, {"error": "quota"}, status_code=429)
        with pytest.raises(DraftingError, match="429"):
            await drafting.draft_proposal(make_job(), make_profile())


class TestTokenBudget:
    def test_budget_leaves_room_for_thinking(self):
        """Measured on real postings: ~200 visible tokens but >1,200 thinking tokens. A budget
        sized for the reply alone comes back empty."""
        assert drafting.MAX_OUTPUT_TOKENS >= 4000


def _stub_gemini(monkeypatch, payload: dict, status_code: int = 200) -> None:
    """Replace httpx.AsyncClient with one that returns a canned Gemini response."""

    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(drafting.httpx, "AsyncClient", lambda **kwargs: _Client())
