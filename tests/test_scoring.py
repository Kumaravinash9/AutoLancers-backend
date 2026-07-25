"""Scoring is pure logic over fixtures — no database, no network."""

from __future__ import annotations

import datetime as dt

import pytest

from app.connectors.freelancer import JobPosting, normalize_project
from app.db.models import Profile
from app.services.scoring import score_job

NOW = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.UTC)


def make_profile(**overrides) -> Profile:
    defaults = dict(
        display_name="InnoAI Labs",
        headline="AI systems, backends and client-owned websites",
        skills=[
            {"name": "next.js", "weight": 5},
            {"name": "react", "weight": 4},
            {"name": "python", "weight": 4},
            {"name": "go", "weight": 1},
        ],
        keywords_include=[],
        keywords_exclude=["equity only", "unpaid"],
        fixed_project_min=500.0,
        hourly_min=25.0,
        currency="USD",
        max_existing_bids=25,
        min_match_score=55.0,
        weight_skills=60.0,
        weight_budget=20.0,
        weight_competition=10.0,
        weight_recency=10.0,
        proposal_notes="",
    )
    defaults.update(overrides)
    return Profile(**defaults)


def make_job(**overrides) -> JobPosting:
    defaults = dict(
        platform="freelancer",
        external_id="1",
        title="Build a Next.js dashboard",
        description="We need a React and Next.js dashboard with a Python backend.",
        url="https://www.freelancer.com/projects/example",
        skills_listed=["Next.js", "React"],
        budget_type="fixed",
        budget_min=800.0,
        budget_max=1500.0,
        currency="USD",
        bid_count=4,
        posted_at=NOW - dt.timedelta(hours=1),
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


class TestHardFilters:
    def test_excluded_keyword_rejects(self):
        job = make_job(description="Equity only, no cash upfront.")
        result = score_job(job, make_profile(), now=NOW)
        assert result.rejected
        assert "equity only" in result.rejection_reason.lower()

    def test_missing_required_keyword_rejects(self):
        profile = make_profile(keywords_include=["shopify"])
        result = score_job(make_job(), profile, now=NOW)
        assert result.rejected
        assert "shopify" in result.rejection_reason.lower()

    def test_present_required_keyword_passes(self):
        profile = make_profile(keywords_include=["next.js"])
        assert not score_job(make_job(), profile, now=NOW).rejected

    def test_too_many_bids_rejects(self):
        result = score_job(make_job(bid_count=99), make_profile(), now=NOW)
        assert result.rejected
        assert "99 bids" in result.rejection_reason

    def test_budget_below_floor_rejects(self):
        result = score_job(make_job(budget_max=100.0), make_profile(), now=NOW)
        assert result.rejected
        assert "below your floor" in result.rejection_reason

    def test_below_min_score_rejects_but_keeps_reasons(self):
        profile = make_profile(min_match_score=99.0)
        result = score_job(make_job(), profile, now=NOW)
        assert result.rejected
        assert "below your minimum" in result.rejection_reason
        assert result.reasons, "a score-based rejection must still explain how it scored"

    @pytest.mark.parametrize("field", ["bid_count", "budget_max", "posted_at"])
    def test_missing_data_does_not_reject(self, field):
        """A filter with no input must skip, not silently reject."""
        job = make_job(**{field: None})
        assert not score_job(job, make_profile(), now=NOW).rejected


class TestScoring:
    def test_strong_match_scores_high(self):
        result = score_job(make_job(), make_profile(), now=NOW)
        assert not result.rejected
        assert result.score >= 80

    def test_no_skill_overlap_scores_low(self):
        job = make_job(
            title="Logo design for a bakery",
            description="Need a logo and brand palette in Illustrator.",
            skills_listed=["Graphic Design"],
        )
        result = score_job(job, make_profile(min_match_score=0), now=NOW)
        assert result.score < 45

    def test_more_competition_scores_lower(self):
        few = score_job(make_job(bid_count=2), make_profile(), now=NOW)
        many = score_job(make_job(bid_count=20), make_profile(), now=NOW)
        assert few.score > many.score

    def test_older_post_scores_lower(self):
        fresh = score_job(make_job(posted_at=NOW - dt.timedelta(hours=1)), make_profile(), now=NOW)
        stale = score_job(make_job(posted_at=NOW - dt.timedelta(hours=40)), make_profile(), now=NOW)
        assert fresh.score > stale.score

    def test_reasons_are_populated(self):
        result = score_job(make_job(), make_profile(), now=NOW)
        labels = {r["label"] for r in result.reasons}
        assert "Skills matched" in labels
        assert all("points" in r for r in result.reasons)

    def test_weight_change_changes_score(self):
        """Tuning has to actually move the number, or the profile page is decorative."""
        job = make_job(bid_count=20)
        skills_heavy = score_job(job, make_profile(weight_skills=90, weight_competition=1), now=NOW)
        comp_heavy = score_job(job, make_profile(weight_skills=10, weight_competition=80), now=NOW)
        assert skills_heavy.score != comp_heavy.score

    def test_secondary_skills_do_not_drag_down_a_strong_match(self):
        """A long tail of secondary skills must not dilute a job that hits your core ones.

        Normalising by the whole profile would mean a Next.js job scores lower purely because you
        also happen to list six other things — under that model, being honest about your range
        makes every job look worse, which is backwards.
        """
        core = [{"name": "next.js", "weight": 5}, {"name": "react", "weight": 5}]
        tail = [
            {"name": name, "weight": 1}
            for name in ("wordpress", "flutter", "rust", "solidity", "figma", "excel")
        ]
        job = make_job(
            title="Next.js and React dashboard",
            description="Build a dashboard in next.js and react.",
            skills_listed=["Next.js", "React"],
        )

        narrow = score_job(job, make_profile(skills=core), now=NOW).score
        broad = score_job(job, make_profile(skills=core + tail), now=NOW).score

        # Some dilution is legitimate — with more skills listed, a three-skill job could have
        # matched more of them. What matters is that it stays mild. The old whole-profile
        # normalisation put this at roughly a third of the narrow score.
        assert broad >= narrow * 0.9, f"broad profile scored {broad} vs {narrow} for the same job"

    def test_matching_a_few_of_many_skills_still_scores_well(self):
        """Matching 3 of 9 skills is a strong fit, not a 33% one — no job asks for everything."""
        profile = make_profile(
            skills=[
                {"name": "next.js", "weight": 5},
                {"name": "react", "weight": 5},
                {"name": "node", "weight": 4},
                *[
                    {"name": n, "weight": 4}
                    for n in ("python", "fastapi", "ai", "llm", "chatbot", "automation")
                ],
            ]
        )
        job = make_job(
            title="Next.js dashboard",
            description="Next.js and react front end with a node backend.",
            skills_listed=["Next.js", "React", "Node.js"],
        )
        result = score_job(job, profile, now=NOW)
        skills_points = next(r["points"] for r in result.reasons if r["label"] == "Skills matched")
        assert skills_points >= profile.weight_skills * 0.9

    def test_all_zero_weights_is_rejected_not_a_crash(self):
        profile = make_profile(
            weight_skills=0, weight_budget=0, weight_competition=0, weight_recency=0
        )
        result = score_job(make_job(), profile, now=NOW)
        assert result.rejected
        assert "weights are zero" in result.rejection_reason


class TestTermMatching:
    def test_short_skill_does_not_false_positive(self):
        """Substring matching makes "go" match "Google" and "going" — boundaries must hold."""
        job = make_job(
            title="Google Ads campaign",
            description="Going live next week. Google Analytics required.",
            skills_listed=["Google Ads"],
        )
        result = score_job(job, make_profile(min_match_score=0), now=NOW)
        matched = [r for r in result.reasons if r["label"] == "Skills matched"]
        assert not matched, "'go' should not match 'Google' or 'going'"

    def test_dotted_skill_matches(self):
        job = make_job(title="Next.js work", description="next.js app", skills_listed=[])
        result = score_job(job, make_profile(min_match_score=0), now=NOW)
        matched = [r for r in result.reasons if r["label"] == "Skills matched"]
        assert matched and "next.js" in matched[0]["detail"]


class TestNormalization:
    def test_normalizes_a_full_project(self):
        posting = normalize_project(
            {
                "id": 12345,
                "title": "Build an API",
                "description": "Full description here",
                "seo_url": "python/build-an-api",
                "type": "fixed",
                "budget": {"minimum": 250, "maximum": 750},
                "currency": {"code": "USD"},
                "bid_stats": {"bid_count": 7},
                "jobs": [{"name": "Python"}, {"name": "FastAPI"}],
                "submitdate": 1753444800,
            }
        )
        assert posting.external_id == "12345"
        assert posting.budget_max == 750.0
        assert posting.currency == "USD"
        assert posting.bid_count == 7
        assert posting.skills_listed == ["Python", "FastAPI"]
        assert posting.url.endswith("python/build-an-api")
        assert posting.posted_at is not None

    def test_missing_fields_become_none_not_zero(self):
        """None and 0 differ to the filters — an absent budget is not a zero budget."""
        posting = normalize_project({"id": 1, "title": "Bare"})
        assert posting.budget_max is None
        assert posting.bid_count is None
        assert posting.posted_at is None
        assert posting.currency is None
        assert posting.skills_listed == []

    def test_survives_junk_field_types(self):
        posting = normalize_project(
            {"id": 2, "budget": None, "currency": None, "bid_stats": None, "jobs": None}
        )
        assert posting.external_id == "2"
        assert posting.budget_min is None
