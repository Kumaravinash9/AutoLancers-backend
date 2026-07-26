"""Job scoring: hard filters, then a weighted 0-100 score.

Two properties this module has to keep:

1. **Explainable.** Every job carries the reasons it scored what it did. You will be tuning weights
   constantly in the first weeks, and a bare number tells you nothing about what to change.
2. **Honest about missing data.** The Freelancer search response does not reliably populate every
   field. A filter that silently passes because its input was ``None`` looks like it is working
   when it is not, so each filter states explicitly that it skipped for lack of data.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from app.connectors.freelancer import JobPosting
from app.db.models import FreelancerProfile, utcnow
from app.services.currency import convert
from app.services.matching import SkillMatch

# Anything older than this scores zero on recency.
RECENCY_HORIZON_HOURS = 48.0

# How many of your strongest skills a job has to hit to earn full marks on the skills component.
# No single posting asks for everything you do, so full credit has to be reachable.
SKILL_SATURATION_COUNT = 3


@dataclass
class ScoreResult:
    score: float
    reasons: list[dict[str, Any]] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str | None = None


def score_job(
    job: JobPosting,
    profile: FreelancerProfile,
    now: dt.datetime | None = None,
    skill_match: SkillMatch | None = None,
) -> ScoreResult:
    """Score one job. ``skill_match`` is an optional precomputed semantic fit (see
    ``services.matching``); when given, it replaces the substring skill match. It's computed in the
    async pipeline — only for new/changed postings — and threaded in here so scoring stays sync."""
    now = now or utcnow()
    haystack = _haystack(job)

    rejection = _hard_filter(job, profile, haystack)
    if rejection:
        return ScoreResult(score=0.0, rejected=True, rejection_reason=rejection)

    reasons: list[dict[str, Any]] = []
    weights = {
        "skills": max(profile.weight_skills, 0.0),
        "budget": max(profile.weight_budget, 0.0),
        "competition": max(profile.weight_competition, 0.0),
        "recency": max(profile.weight_recency, 0.0),
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return ScoreResult(
            score=0.0,
            rejected=True,
            rejection_reason="All scoring weights are zero — nothing can be scored.",
        )

    earned = 0.0
    earned += _score_skills(job, profile, haystack, weights["skills"], reasons, skill_match)
    earned += _score_budget(job, profile, weights["budget"], reasons)
    earned += _score_competition(job, profile, weights["competition"], reasons)
    earned += _score_recency(job, now, weights["recency"], reasons)

    score = round(earned / total_weight * 100, 1)

    if score < profile.min_match_score:
        return ScoreResult(
            score=score,
            reasons=reasons,
            rejected=True,
            rejection_reason=f"Scored {score}, below your minimum of {profile.min_match_score}.",
        )

    return ScoreResult(score=score, reasons=reasons)


# --- hard filters -----------------------------------------------------------------


def hard_reject_reason(job: JobPosting, profile: FreelancerProfile) -> str | None:
    """The reason a hard filter rejects ``job`` outright, or ``None`` if it clears them.

    Exposed so callers can distinguish a hard rejection (excluded/required keyword or budget floor
    — final regardless of skill fit, since these run before skills scoring) from a merely low
    score. Used to decide whether an expensive semantic skill match is worth running at all.
    """
    return _hard_filter(job, profile, _haystack(job))


def _hard_filter(job: JobPosting, profile: FreelancerProfile, haystack: str) -> str | None:
    for term in profile.keywords_exclude or []:
        if term and _contains_term(haystack, term):
            return f"Excluded keyword: {term!r}"

    includes = [t for t in (profile.keywords_include or []) if t]
    if includes and not any(_contains_term(haystack, t) for t in includes):
        return f"No required keyword present (need one of: {', '.join(includes)})"

    floor = _budget_floor(job, profile)
    if floor is not None and job.budget_max is not None:
        budget = _budget_in_floor_currency(job, profile)
        # If the currencies differ and there's no rate, ``budget`` is None: skip the floor rather
        # than compare across currencies — a garbage comparison is how a ₹12,500 job used to read
        # as clearing a $500 floor.
        if budget is not None and budget < floor:
            return (
                f"Budget tops out at {job.budget_max:.0f} {job.currency or profile.currency}, "
                f"below your floor of {floor:.0f} {profile.currency}"
            )

    return None


def _budget_floor(job: JobPosting, profile: FreelancerProfile) -> float | None:
    if job.budget_type == "hourly":
        return profile.rate_min or None
    if job.budget_type == "fixed":
        return profile.fixed_project_min or None
    return None  # unknown type — don't guess which floor applies


def _budget_in_floor_currency(job: JobPosting, profile: FreelancerProfile) -> float | None:
    """``job.budget_max`` expressed in the profile's currency, which floors are stated in.

    ``None`` when the currencies differ and no conversion rate is available — the caller treats
    that as "can't tell", not as zero. A job with no stated currency is assumed to already be in
    the profile's currency (the same assumption the rest of scoring makes)."""
    if job.budget_max is None:
        return None
    job_currency = job.currency or profile.currency
    if job_currency == profile.currency:
        return job.budget_max
    return convert(job.budget_max, job_currency, profile.currency)


# --- components -------------------------------------------------------------------


def _score_skills(
    job: JobPosting,
    profile: FreelancerProfile,
    haystack: str,
    weight: float,
    reasons: list[dict[str, Any]],
    skill_match: SkillMatch | None = None,
) -> float:
    skills = [s for s in (profile.skills or []) if s.get("name")]
    if not skills or weight <= 0:
        return 0.0

    # A precomputed semantic fit (see ``services.matching``) supersedes substring matching: it
    # already reasoned about equivalent and adjacent skills the literal match can't see. Its 0-1
    # score is the fraction of the skills weight earned.
    if skill_match is not None:
        earned = skill_match.score * weight
        detail = skill_match.reason or (
            ", ".join(skill_match.matched) if skill_match.matched else "Semantic fit"
        )
        reasons.append(
            {
                "label": "Skills matched (semantic)",
                "detail": detail,
                "points": round(earned, 1),
            }
        )
        return earned

    # Score against what a single job could plausibly ask for, not against your whole skill list.
    # Normalising by the total would punish having broad skills: a Python/FastAPI/AI job would
    # score 33% simply because you also happen to list Next.js and React. The target is the sum
    # of your top few weights, so matching a few strong skills earns full marks.
    weights = sorted((float(s.get("weight", 1)) for s in skills), reverse=True)
    target = sum(weights[:SKILL_SATURATION_COUNT])
    if target <= 0:
        return 0.0

    matched = [s for s in skills if _contains_term(haystack, s["name"])]
    matched_weight = sum(float(s.get("weight", 1)) for s in matched)
    earned = min(1.0, matched_weight / target) * weight

    if matched:
        names = ", ".join(s["name"] for s in matched)
        reasons.append(
            {
                "label": "Skills matched",
                "detail": f"{len(matched)}/{len(skills)} — {names}",
                "points": round(earned, 1),
            }
        )
    else:
        reasons.append(
            {"label": "No skill match", "detail": "None of your skills appear", "points": 0.0}
        )
    return earned


def _score_budget(
    job: JobPosting, profile: FreelancerProfile, weight: float, reasons: list[dict[str, Any]]
) -> float:
    if weight <= 0:
        return 0.0

    if job.budget_max is None:
        # Neutral, not zero — an unstated budget is not evidence of a bad budget.
        earned = weight * 0.5
        reasons.append(
            {"label": "Budget not stated", "detail": "Scored neutral", "points": round(earned, 1)}
        )
        return earned

    floor = _budget_floor(job, profile)
    currency = job.currency or profile.currency

    if not floor:
        earned = weight * 0.5
        reasons.append(
            {
                "label": "No budget floor set",
                "detail": f"Listed up to {job.budget_max:.0f} {currency}",
                "points": round(earned, 1),
            }
        )
        return earned

    budget = _budget_in_floor_currency(job, profile)
    if budget is None:
        # Currencies differ and there's no rate — score neutral rather than divide across them.
        earned = weight * 0.5
        reasons.append(
            {
                "label": "Budget currency unknown",
                "detail": f"Listed up to {job.budget_max:.0f} {currency}; can't compare to your floor",
                "points": round(earned, 1),
            }
        )
        return earned

    ratio = budget / floor
    fraction = 1.0 if ratio >= 2 else 0.7 if ratio >= 1 else 0.0
    earned = weight * fraction
    reasons.append(
        {
            "label": "Budget fit",
            "detail": f"Up to {job.budget_max:.0f} {currency} against your floor of {floor:.0f} {profile.currency}",
            "points": round(earned, 1),
        }
    )
    return earned


def _score_competition(
    job: JobPosting, profile: FreelancerProfile, weight: float, reasons: list[dict[str, Any]]
) -> float:
    if weight <= 0:
        return 0.0

    if job.bid_count is None:
        earned = weight * 0.5
        reasons.append(
            {"label": "Bid count unknown", "detail": "Scored neutral", "points": round(earned, 1)}
        )
        return earned

    # Competition costs points; it does not disqualify. A hard cap threw away 59 of 75 postings
    # on a normal day — including the profile's only won job, at 50 bids — because postings on
    # this board routinely pass 70 bids within the hour. Being crowded is a reason to rank a job
    # lower, not a reason to hide it.
    #
    # Decay is hyperbolic rather than linear so it never bottoms out: a 30-bid job and a 300-bid
    # job have to be distinguishable, which subtraction against a cap cannot do once both are
    # past it. ``crowded_at_bids`` is the count that scores half marks.
    reference = profile.crowded_at_bids or 25
    fraction = reference / (reference + job.bid_count)
    earned = weight * fraction
    reasons.append(
        {
            "label": "Competition",
            "detail": f"{job.bid_count} bids so far",
            "points": round(earned, 1),
        }
    )
    return earned


def _score_recency(
    job: JobPosting, now: dt.datetime, weight: float, reasons: list[dict[str, Any]]
) -> float:
    if weight <= 0:
        return 0.0

    if job.posted_at is None:
        earned = weight * 0.5
        reasons.append(
            {"label": "Post age unknown", "detail": "Scored neutral", "points": round(earned, 1)}
        )
        return earned

    posted_at = job.posted_at
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=dt.UTC)

    age_hours = max(0.0, (now - posted_at).total_seconds() / 3600)
    fraction = max(0.0, 1.0 - age_hours / RECENCY_HORIZON_HOURS)
    earned = weight * fraction
    reasons.append(
        {
            "label": "Freshness",
            "detail": f"Posted {_humanise_age(age_hours)} ago",
            "points": round(earned, 1),
        }
    )
    return earned


# --- helpers ----------------------------------------------------------------------


def _haystack(job: JobPosting) -> str:
    return " ".join([job.title, job.description, " ".join(job.skills_listed)]).lower()


def _contains_term(haystack: str, term: str) -> bool:
    """Word-boundary match.

    Plain substring matching makes short skills unusable — "R" hits every word containing an r,
    and "Go" matches "Google", "going", "Django". Boundaries keep one-or-two-character skills
    meaningful. Multi-word terms are matched with flexible internal whitespace so "next js"
    matches "next  js" across a line break.
    """
    term = term.strip().lower()
    if not term:
        return False
    pattern = r"\s+".join(re.escape(part) for part in term.split())
    return re.search(rf"(?<!\w){pattern}(?!\w)", haystack) is not None


def _humanise_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"
