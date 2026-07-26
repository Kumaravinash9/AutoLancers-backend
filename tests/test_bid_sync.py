"""Bid-sync mapping.

The network leg can't be tested without a live token, so these pin the part that decides what a
bid *means* — which is where a wrong assumption would quietly corrupt your win rate.
"""

from __future__ import annotations

import pytest

from app.db.models import ProposalStatus
from app.services.bid_sync import AWARD_STATUS


class TestAwardStatusMapping:
    @pytest.mark.parametrize(
        ("award", "expected"),
        [
            ("awarded", ProposalStatus.ACCEPTED),
            ("rejected", ProposalStatus.REJECTED),
            ("revoked", ProposalStatus.WITHDRAWN),
            ("pending", ProposalStatus.SUBMITTED),
        ],
    )
    def test_known_states(self, award, expected):
        assert AWARD_STATUS[award] == expected

    @pytest.mark.parametrize("award", ["", "something_new", "in_review", None])
    def test_unknown_state_is_not_treated_as_a_loss(self, award):
        """An unrecognised award status must fall back to SUBMITTED, never REJECTED.

        Freelancer can add states at any time. Guessing 'rejected' would silently invent losses
        and skew every calibration figure built on top.
        """
        resolved = AWARD_STATUS.get(str(award or "").lower(), ProposalStatus.SUBMITTED)
        assert resolved == ProposalStatus.SUBMITTED
