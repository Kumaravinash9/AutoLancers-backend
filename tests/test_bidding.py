"""Bid submission gates.

This is the only code path that acts irreversibly in the outside world, so the tests are about
what must *not* happen. Every case below asserts that no request reaches Freelancer.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.db.models import PlatformConnection, Project, Proposal, Recommendation
from app.services import bidding
from app.services.bidding import (
    BiddingError,
    check_availability,
    submit_bid_for_recommendation,
)


@pytest.fixture
def settings(monkeypatch):
    def apply(enable_bidding=True):
        replacement = Settings(enable_bidding=enable_bidding)
        monkeypatch.setattr(bidding, "get_settings", lambda: replacement)
        return replacement

    return apply


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything tries to reach Freelancer. Tracks whether a client was built."""
    built = {"client": False}

    class _Exploding:
        def __init__(self, *a, **kw):
            built["client"] = True

        async def fetch_self_id(self):
            raise AssertionError("reached Freelancer despite a closed gate")

        async def submit_bid(self, **kw):
            raise AssertionError("submitted a bid despite a closed gate")

    monkeypatch.setattr(bidding, "create_connector", lambda *a, **kw: _Exploding())
    return built


def make_job(**overrides) -> Recommendation:
    """A recommendation with its project and draft attached, as the bidding path expects."""
    text = overrides.pop(
        "proposal_text", "Hi there, I can build the dashboard you described, connected to Postgres."
    )
    rec = Recommendation(
        is_hard_rejected=overrides.pop("rejected", False),
        status=overrides.pop("status", "NEW"),
    )
    rec.project = Project(platform="freelancer", external_id="12345", title="Next.js dashboard")
    rec.proposal = Proposal(
        proposal_text=text, external_bid_id=overrides.pop("external_bid_id", None)
    )
    return rec


def make_token(scope: str | None) -> PlatformConnection:
    return PlatformConnection(platform="freelancer", access_token_encrypted="x", scope=scope)


class TestAvailability:
    def test_disabled_install_explains_itself(self, settings):
        settings(enable_bidding=False)
        result = check_availability(make_token("basic fln:project_manage"))
        assert not result.available
        assert "ENABLE_BIDDING" in result.reason

    def test_no_connection_says_connect(self, settings):
        settings()
        result = check_availability(None)
        assert not result.available
        assert "Connect" in result.reason

    def test_read_only_token_says_reconnect(self, settings):
        settings()
        result = check_availability(make_token("basic"))
        assert not result.available
        assert "read-only" in result.reason

    def test_ready_when_scope_present(self, settings):
        settings()
        assert check_availability(make_token("basic fln:project_manage")).available


class TestGates:
    """Each of these must stop before any network call."""

    async def test_disabled_install_blocks(self, settings, no_network):
        settings(enable_bidding=False)
        with pytest.raises(BiddingError, match="switched off"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(), 500, 7, confirm=True
            )
        assert not no_network["client"]

    async def test_unconfirmed_request_blocks(self, settings, no_network):
        settings()
        with pytest.raises(BiddingError, match="not confirmed"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(), 500, 7, confirm=False
            )
        assert not no_network["client"]

    async def test_double_bid_blocks(self, settings, no_network):
        settings()
        job = make_job(external_bid_id="99887", status="submitted")
        with pytest.raises(BiddingError, match="Already bid"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, job, 500, 7, confirm=True
            )
        assert not no_network["client"]

    async def test_rejected_job_blocks(self, settings, no_network):
        settings()
        with pytest.raises(BiddingError, match="filtered out"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(rejected=True), 500, 7, confirm=True
            )
        assert not no_network["client"]

    async def test_empty_proposal_blocks(self, settings, no_network):
        settings()
        with pytest.raises(BiddingError, match="Write or generate a proposal"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(proposal_text=None), 500, 7, confirm=True
            )
        assert not no_network["client"]

    async def test_stub_proposal_blocks(self, settings, no_network):
        """A near-empty draft would arrive as a truncated proposal — worse than not bidding."""
        settings()
        with pytest.raises(BiddingError, match="Write or generate a proposal"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(proposal_text="ok"), 500, 7, confirm=True
            )
        assert not no_network["client"]

    @pytest.mark.parametrize(
        ("amount", "period", "match"),
        [(0, 7, "greater than zero"), (-5, 7, "greater than zero"), (500, 0, "at least one day")],
    )
    async def test_nonsense_terms_block(self, settings, no_network, amount, period, match):
        settings()
        with pytest.raises(BiddingError, match=match):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, make_job(), amount, period, confirm=True
            )
        assert not no_network["client"]


class TestScopeIsEnforcedOnTheWritePath:
    """Regression: the scope check originally lived only in the availability endpoint.

    A direct POST therefore reached Freelancer holding a read-only token. The remote rejected it
    for an unrelated reason, which is luck, not a guarantee — the check has to be here.
    """

    async def test_read_only_token_never_reaches_freelancer(self, settings, no_network):
        settings()
        session = _SessionWithToken(make_token("basic"))
        with pytest.raises(BiddingError, match="read-only"):
            await submit_bid_for_recommendation(session, 1, make_job(), 500, 7, confirm=True)
        assert not no_network["client"]

    async def test_absent_token_never_reaches_freelancer(self, settings, no_network):
        settings()
        session = _SessionWithToken(None)
        with pytest.raises(BiddingError, match="Connect"):
            await submit_bid_for_recommendation(session, 1, make_job(), 500, 7, confirm=True)
        assert not no_network["client"]


def _bidder_session() -> _SessionWithToken:
    """A session whose stored token carries the bid scope, so gate tests exercise the gate
    they are named for rather than tripping on the scope check first."""
    return _SessionWithToken(make_token("basic fln:project_manage"))


class _SessionWithToken:
    """Minimal stand-in for AsyncSession: returns one token row from `scalar`."""

    def __init__(self, token_row):
        self._token_row = token_row

    async def scalar(self, _query):
        return self._token_row

    async def commit(self):
        pass


class TestRecording:
    async def test_success_records_the_bid(self, settings, monkeypatch):
        settings()

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def fetch_self_id(self):
                return 4242

            async def submit_bid(self, **kw):
                assert kw["bidder_id"] == 4242, "bidder_id must be resolved, never left null"
                assert kw["milestone_percentage"] == 100
                return "55501"

        monkeypatch.setattr(bidding, "create_connector", lambda *a, **kw: _Client())
        monkeypatch.setattr(bidding, "get_valid_access_token", _fake_token)

        job = make_job()
        session = _SessionWithToken(make_token('basic fln:project_manage'))
        bid_id = await submit_bid_for_recommendation(session, 1, job, 750.0, 10, confirm=True)

        assert bid_id == "55501"
        assert job.proposal.external_bid_id == "55501"
        assert job.proposal.bid_amount == 750.0
        assert job.proposal.estimated_days == 10
        assert job.status == "APPLIED"
        assert isinstance(job.proposal.submitted_at, dt.datetime)

    async def test_a_recorded_bid_cannot_be_repeated(self, settings, no_network):
        """The recorded id is what makes the second attempt fail, not luck."""
        settings()
        job = make_job(external_bid_id="55501")
        with pytest.raises(BiddingError, match="Already bid"):
            await submit_bid_for_recommendation(
                _bidder_session(), 1, job, 750.0, 10, confirm=True
            )


async def _fake_token(session, user_id, platform="freelancer"):
    return "token"
