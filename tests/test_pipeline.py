"""Discovery paging and semantic-match cost gating — the parts of the cycle that bound spend.

These are unit tests over the pure-ish helpers; the full ``run_cycle`` needs a live API and a
database and is exercised by hand (see the README's verification notes).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.connectors.freelancer import JobPosting
from app.services import pipeline
from app.services.pipeline import (
    WATERMARK_OVERLAP,
    CycleReport,
    _fetch_new_postings,
    _MatchBudget,
    _resolve_search_skill_ids,
    _search_skill_ids_key,
    _worth_matching,
)
from app.services.skill_expansion import SkillExpansionError
from tests.test_scoring import make_job, make_profile


@pytest.fixture
def settings(monkeypatch):
    """Swap in throwaway settings so the cost gate never depends on the developer's .env."""

    def apply(**overrides):
        values = dict(gemini_api_key="", anthropic_api_key="")
        values.update(overrides)
        replacement = Settings(**values)
        monkeypatch.setattr(pipeline, "get_settings", lambda: replacement)
        return replacement

    return apply


class FakeClient:
    """Returns a fixed list of postings, sliced by the offset/limit it's asked for."""

    def __init__(self, postings: list[JobPosting]):
        self.postings = postings
        self.calls: list[dict] = []

    async def search_active_projects(
        self, *, skill_ids=None, from_time=None, offset=0, limit=100, **_
    ) -> list[JobPosting]:
        self.calls.append({"offset": offset, "limit": limit, "from_time": from_time})
        return self.postings[offset : offset + limit]


def _postings(n: int) -> list[JobPosting]:
    return [
        JobPosting(platform="freelancer", external_id=str(i), title="t", description="d", url="")
        for i in range(n)
    ]


class TestMatchBudget:
    def test_positive_budget_decrements_then_denies(self):
        budget = _MatchBudget(remaining=2)
        assert budget.take() is True
        assert budget.take() is True
        assert budget.take() is False  # exhausted — subsequent jobs fall back to substring

    def test_none_is_unlimited(self):
        budget = _MatchBudget(remaining=None)
        assert all(budget.take() for _ in range(100))

    def test_zero_denies_immediately(self):
        assert _MatchBudget(remaining=0).take() is False


class TestWorthMatching:
    def test_normal_job_is_worth_matching(self, settings):
        settings(skill_match_enabled=True)
        assert _worth_matching(make_job(), make_profile()) is True

    def test_feature_off_skips(self, settings):
        settings(skill_match_enabled=False)
        assert _worth_matching(make_job(), make_profile()) is False

    def test_no_skills_skips(self, settings):
        settings(skill_match_enabled=True)
        assert _worth_matching(make_job(), make_profile(skills=[])) is False

    def test_hard_rejected_job_skips(self, settings):
        """A keyword/budget rejection is final regardless of skill fit — don't pay the LLM
        for it."""
        settings(skill_match_enabled=True)
        job = make_job(description="This is an unpaid volunteer role.")
        assert _worth_matching(job, make_profile()) is False

    def test_low_scoring_job_still_qualifies(self, settings):
        """A job that merely scores low (no literal skill overlap) is exactly the rescue case."""
        settings(skill_match_enabled=True)
        job = make_job(
            title="Expo cross-platform app",
            description="Build a cross-platform mobile app with Expo.",
            skills_listed=["Expo"],
        )
        assert _worth_matching(job, make_profile()) is True


class TestFetchNewPostings:
    async def test_stops_on_an_empty_page(self, monkeypatch):
        monkeypatch.setattr(pipeline, "DISCOVERY_PAGE_SIZE", 2)
        client = FakeClient(_postings(3))

        postings, truncated = await _fetch_new_postings(client, [], since=None, max_pages=5)

        assert len(postings) == 3
        assert truncated is False
        # Offset advances by rows actually returned (2, then 1), and only the empty page at 3 stops
        # it — a short-but-nonempty page must not be read as "done" (Freelancer caps a page at ~50).
        assert [c["offset"] for c in client.calls] == [0, 2, 3]

    async def test_page_cap_reports_truncation(self, monkeypatch):
        monkeypatch.setattr(pipeline, "DISCOVERY_PAGE_SIZE", 2)
        client = FakeClient(_postings(4))

        postings, truncated = await _fetch_new_postings(client, [], since=None, max_pages=2)

        assert len(postings) == 4
        assert truncated is True  # every page full at the cap — more likely remain
        assert [c["offset"] for c in client.calls] == [0, 2]

    async def test_watermark_becomes_from_time_with_overlap(self, monkeypatch):
        monkeypatch.setattr(pipeline, "DISCOVERY_PAGE_SIZE", 100)
        client = FakeClient(_postings(1))
        since = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.UTC)

        await _fetch_new_postings(client, [], since=since, max_pages=5)

        assert client.calls[0]["from_time"] == int((since - WATERMARK_OVERLAP).timestamp())

    async def test_first_run_sends_no_from_time(self, monkeypatch):
        monkeypatch.setattr(pipeline, "DISCOVERY_PAGE_SIZE", 100)
        client = FakeClient(_postings(1))

        await _fetch_new_postings(client, [], since=None, max_pages=1)

        assert client.calls[0]["from_time"] is None


class FakeResolveClient:
    """Resolves skill names to ids from a fixed name->id map, recording each call."""

    def __init__(self, mapping: dict[str, int]):
        self.mapping = {k.lower(): v for k, v in mapping.items()}
        self.calls: list[list[str]] = []

    async def resolve_skill_ids(self, names: list[str]) -> tuple[list[int], list[str]]:
        self.calls.append(list(names))
        ids = [self.mapping[n.lower()] for n in names if n.lower() in self.mapping]
        unmatched = [n for n in names if n.lower() not in self.mapping]
        return ids, unmatched


def _expander(monkeypatch, tags, *, calls=None):
    """Patch pipeline.expand_skill_terms with an async stub returning `tags`."""

    async def fake(names):
        if calls is not None:
            calls.append(list(names))
        return list(tags)

    monkeypatch.setattr(pipeline, "expand_skill_terms", fake)


class TestSearchSkillIdsKey:
    def test_same_inputs_same_key(self):
        assert _search_skill_ids_key(["React", "Node"], True) == _search_skill_ids_key(
            ["Node", "React"], True  # order-independent
        )

    def test_flag_changes_the_key(self):
        assert _search_skill_ids_key(["React"], True) != _search_skill_ids_key(["React"], False)


class TestResolveSearchSkillIds:
    def _profile(self, **overrides):
        return make_profile(**overrides)

    async def test_unions_direct_and_expanded_ids_and_pins_the_key(self, settings, monkeypatch):
        settings(skill_expansion_enabled=True)
        _expander(monkeypatch, ["Web Development"])
        client = FakeResolveClient({"next.js": 1, "web development": 2})
        profile = self._profile(skills=[{"name": "next.js", "weight": 5}])
        report = CycleReport()

        ids = await _resolve_search_skill_ids(client, profile, report)

        assert ids == [1, 2]
        assert profile.search_skill_ids == [1, 2]
        assert profile.search_skill_ids_key == _search_skill_ids_key(["next.js"], True)

    async def test_reuses_cache_without_calling_out(self, settings, monkeypatch):
        settings(skill_expansion_enabled=True)
        called: list = []
        _expander(monkeypatch, ["X"], calls=called)
        client = FakeResolveClient({"next.js": 1})
        profile = self._profile(skills=[{"name": "next.js", "weight": 5}])
        profile.search_skill_ids = [9]
        profile.search_skill_ids_key = _search_skill_ids_key(["next.js"], True)

        ids = await _resolve_search_skill_ids(client, profile, CycleReport())

        assert ids == [9]
        assert client.calls == []  # no resolve
        assert called == []  # no expansion

    async def test_expansion_failure_keeps_direct_and_leaves_key_stale(self, settings, monkeypatch):
        settings(skill_expansion_enabled=True)

        async def boom(names):
            raise SkillExpansionError("down")

        monkeypatch.setattr(pipeline, "expand_skill_terms", boom)
        client = FakeResolveClient({"next.js": 1})
        profile = self._profile(skills=[{"name": "next.js", "weight": 5}])

        ids = await _resolve_search_skill_ids(client, profile, CycleReport())

        assert ids == [1]  # still filtered on the listed skill
        assert profile.search_skill_ids == [1]
        assert profile.search_skill_ids_key is None  # not pinned → retried next cycle

    async def test_no_skills_pins_empty(self, settings, monkeypatch):
        settings(skill_expansion_enabled=True)
        _expander(monkeypatch, ["X"])
        client = FakeResolveClient({"x": 1})
        profile = self._profile(skills=[])

        ids = await _resolve_search_skill_ids(client, profile, CycleReport())

        assert ids == []
        assert client.calls == []  # nothing to resolve
        assert profile.search_skill_ids_key == _search_skill_ids_key([], True)
