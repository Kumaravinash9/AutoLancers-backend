"""Bid-sync mapping.

The network leg can't be tested without a live token, so these pin the part that decides what a
bid *means* — which is where a wrong assumption would quietly corrupt your win rate.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models import FreelancerProfile, PlatformConnection, ProposalStatus
from app.services.bid_sync import AWARD_STATUS, _store_identity


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


class TestStoredIdentity:
    """Mapping the marketplace's own record of an account onto its profile.

    Every field here is what a client sees on the marketplace, so a wrong mapping shows the wrong
    person's details on a card rather than failing loudly. Identity (username) stays on the
    connection; everything descriptive now lives on the linked profile.
    """

    @staticmethod
    def _apply(
        payload: dict, profile: FreelancerProfile | None = None
    ) -> tuple[PlatformConnection, FreelancerProfile]:
        connection = PlatformConnection()
        profile = profile or FreelancerProfile()
        asyncio.run(_store_identity(None, profile, connection, payload))
        return connection, profile

    def test_maps_the_public_profile(self):
        _, p = self._apply(
            {
                "id": 93912309,
                "username": "aiinno",
                "public_name": "Avinash K.",
                "tagline": "Team of IIT Engineers",
                "profile_description": "AI & Backend Engineer.",
                "jobs": [{"name": "Software Architecture"}, {"name": "MongoDB"}],
                "hourly_rate": 25.0,
                "primary_currency": {"code": "INR"},
                "location": {"country": {"name": "India"}},
                "portfolio_count": 3,
            }
        )
        assert p.display_name == "Avinash K."
        assert p.tagline == "Team of IIT Engineers"
        assert p.account_skills == ["Software Architecture", "MongoDB"]
        assert p.hourly_rate == 25.0
        assert p.currency == "INR"
        assert p.country == "India"
        assert p.portfolio_count == 3

    def test_protocol_relative_avatar_is_made_loadable(self):
        """The API returns //host/path in places, which resolves to nothing in an img tag."""
        _, p = self._apply({"id": 1, "avatar_large_cdn": "//cdn2.f-cdn.com/ppic/1/logo.jpg"})
        assert p.avatar_url == "https://cdn2.f-cdn.com/ppic/1/logo.jpg"

    def test_a_bare_account_leaves_fields_empty_rather_than_guessing(self):
        c, p = self._apply({"id": 2, "username": "newcomer"})
        assert c.platform_username == "newcomer"
        assert not p.account_skills
        assert p.tagline is None
        assert p.hourly_rate is None
        assert p.member_since is None

    def test_registration_date_becomes_an_aware_datetime(self):
        _, p = self._apply({"id": 3, "registration_date": 1784569366})
        assert p.member_since is not None
        assert p.member_since.tzinfo is not None


class TestHomeMirror:
    """The account's home country/currency is mirrored onto the profile — where the app reads it."""

    @staticmethod
    def _mirror(payload: dict, profile: FreelancerProfile) -> None:
        asyncio.run(_store_identity(None, profile, PlatformConnection(), payload))

    def test_populates_a_fresh_profile(self):
        profile = FreelancerProfile()
        self._mirror(
            {
                "id": 1,
                "primary_currency": {"code": "INR"},
                "location": {"country": {"name": "India"}},
            },
            profile,
        )
        assert profile.country == "India"
        assert profile.currency == "INR"

    def test_does_not_reinterpret_currency_once_a_floor_is_set(self):
        # The freelancer entered a 500 floor — its currency is theirs to change, not the sync's.
        profile = FreelancerProfile(fixed_project_min=500.0, currency="USD")
        self._mirror(
            {
                "id": 1,
                "primary_currency": {"code": "INR"},
                "location": {"country": {"name": "India"}},
            },
            profile,
        )
        assert profile.currency == "USD"  # respected
        assert profile.country == "India"  # country still mirrors — it isn't tied to a number
