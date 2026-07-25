"""Drafting failure handling.

These cover the paths that must not take the poller down. Generation quality itself needs a live
API key and a real posting — see the drafting check in the README's verification notes.
"""

from __future__ import annotations

import pytest

from app.services import drafting
from app.services.drafting import DraftingError, _budget_line, _build_user_message
from tests.test_scoring import make_job, make_profile


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
        line = _budget_line(make_job(budget_min=None, budget_max=900.0))
        assert "900" in line


class TestFailureHandling:
    async def test_missing_api_key_raises_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(drafting, "_client", _raise_missing_key)
        with pytest.raises(DraftingError, match="ANTHROPIC_API_KEY"):
            await drafting.draft_proposal(make_job(), make_profile())

    async def test_max_tokens_leaves_room_for_thinking(self):
        """Thinking is on by default on this model and shares the max_tokens budget with the
        response, so a limit sized only for a 180-word proposal would truncate drafts."""
        assert drafting.MAX_TOKENS >= 4000


def _raise_missing_key():
    raise DraftingError("ANTHROPIC_API_KEY is not set")
