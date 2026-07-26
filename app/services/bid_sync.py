"""Pulling your real bids back from Freelancer.

Without this the proposals view only knows about bids this tool placed, and every outcome is
unknown. Syncing does two things worth having:

* **Completeness.** Bids you placed directly on Freelancer appear alongside the assisted ones, so
  the calibration figures cover all your bidding rather than a self-selected slice.
* **Outcomes.** Freelancer reports an award status per bid, which is the only source of truth for
  whether you were selected. Nothing else in this system can know that.

A bid whose project we have never seen is fetched and stored, so it can be scored like any other.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.freelancer_oauth import OAuthError, get_valid_access_token
from app.connectors.freelancer import FreelancerAPIError, FreelancerClient, JobPosting
from app.db.models import (
    DiscoveryMethod,
    FreelancerProfile,
    Project,
    Proposal,
    ProposalStatus,
    Recommendation,
    RecommendationStatus,
    SubmittedVia,
    utcnow,
)
from app.services.scoring import score_job

logger = logging.getLogger(__name__)

# Freelancer's award_status -> our proposal status. Anything unrecognised stays SUBMITTED rather
# than being guessed at: an unknown state is not evidence of a loss.
AWARD_STATUS = {
    "awarded": ProposalStatus.ACCEPTED,
    "rejected": ProposalStatus.REJECTED,
    "revoked": ProposalStatus.WITHDRAWN,
    "pending": ProposalStatus.SUBMITTED,
}


class BidSyncError(RuntimeError):
    pass


@dataclass
class SyncReport:
    fetched: int = 0
    linked: int = 0          # matched a proposal we already had
    imported: int = 0        # bid placed outside this tool
    projects_added: int = 0  # bid pointed at a project we had never seen
    outcomes_updated: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


async def sync_bids(
    session: AsyncSession, user_id: uuid.UUID, profile: FreelancerProfile
) -> SyncReport:
    report = SyncReport()

    try:
        token = await get_valid_access_token(session, user_id)
    except OAuthError as exc:
        report.error = str(exc)
        return report

    client = FreelancerClient(access_token=token)

    try:
        bidder_id = await client.fetch_self_id()
        bids = await client.fetch_my_bids(bidder_id)
    except FreelancerAPIError as exc:
        report.error = str(exc)
        logger.warning("Bid sync failed: %s", exc)
        return report

    report.fetched = len(bids)
    if not bids:
        profile.bids_synced_at = utcnow()
        await session.commit()
        return report

    # Resolve every project referenced by a bid in one pass, then fetch only the unknown ones.
    external_ids = {str(b.get("project_id")) for b in bids if b.get("project_id")}
    known = {
        p.external_id: p
        for p in (
            await session.scalars(
                select(Project).where(
                    Project.platform == "freelancer", Project.external_id.in_(external_ids)
                )
            )
        ).all()
    }

    missing = [int(i) for i in external_ids - known.keys() if i.isdigit()]
    if missing:
        try:
            fetched = await client.fetch_projects_by_id(missing)
        except FreelancerAPIError as exc:
            # Partial sync beats none: link what we can and report the rest next time.
            logger.warning("Could not fetch %d unseen projects: %s", len(missing), exc)
            fetched = {}
        for external_id, posting in fetched.items():
            project = _project_from(posting)
            session.add(project)
            known[external_id] = project
            report.projects_added += 1
        if fetched:
            await session.flush()

    for bid in bids:
        try:
            outcome = await _apply(session, profile, bid, known)
        except Exception:
            logger.exception("Could not sync bid %s", bid.get("id"))
            continue
        report.linked += int(outcome.linked)
        report.imported += int(outcome.imported)
        report.outcomes_updated += int(outcome.outcome_changed)

    profile.bids_synced_at = utcnow()
    await session.commit()
    logger.info("Bid sync: %s", report.as_dict())
    return report


@dataclass
class _Applied:
    linked: bool = False
    imported: bool = False
    outcome_changed: bool = False


async def _apply(
    session: AsyncSession,
    profile: FreelancerProfile,
    bid: dict[str, Any],
    known: dict[str, Project],
) -> _Applied:
    external_bid_id = str(bid.get("id") or "")
    project = known.get(str(bid.get("project_id")))
    if not external_bid_id or project is None:
        return _Applied()

    status = AWARD_STATUS.get(str(bid.get("award_status") or "").lower(), ProposalStatus.SUBMITTED)

    # Match on the marketplace's own bid id first — it's the only identifier both sides agree on.
    proposal = await session.scalar(
        select(Proposal).where(
            Proposal.freelancer_id == profile.id, Proposal.external_bid_id == external_bid_id
        )
    )
    result = _Applied()

    if proposal is None:
        # A bid we submitted through this tool won't have the id yet; match it by project instead
        # so syncing adopts it rather than creating a duplicate.
        proposal = await session.scalar(
            select(Proposal).where(
                Proposal.freelancer_id == profile.id,
                Proposal.project_id == project.id,
                Proposal.external_bid_id.is_(None),
            )
        )
        if proposal is not None:
            result.linked = True

    if proposal is None:
        proposal = Proposal(
            project_id=project.id,
            freelancer_id=profile.id,
            submitted_via=SubmittedVia.MANUAL_COPY,
        )
        session.add(proposal)
        result.imported = True

    if proposal.status != status:
        result.outcome_changed = True
    proposal.status = status

    proposal.external_bid_id = external_bid_id
    proposal.bid_amount = _as_float(bid.get("amount")) or proposal.bid_amount
    proposal.estimated_days = _as_int(bid.get("period")) or proposal.estimated_days
    # Freelancer's description is what the client actually received, so it wins over our draft.
    if bid.get("description"):
        proposal.proposal_text = bid["description"]
    if proposal.submitted_at is None:
        proposal.submitted_at = _as_datetime(bid.get("submitdate"))
    proposal.updated_at = utcnow()

    await _attach_recommendation(session, profile, project, proposal, status)
    return result


async def _attach_recommendation(
    session: AsyncSession,
    profile: FreelancerProfile,
    project: Project,
    proposal: Proposal,
    status: str,
) -> None:
    """Link the proposal to this profile's recommendation, scoring the project if it has none.

    An imported bid is worth scoring even after the fact: it's how a bid you found yourself gets
    compared against the ones we suggested.
    """
    rec = await session.scalar(
        select(Recommendation).where(
            Recommendation.freelancer_id == profile.id, Recommendation.project_id == project.id
        )
    )
    if rec is None:
        result = score_job(_to_posting(project), profile)
        rec = Recommendation(
            freelancer_id=profile.id,
            project_id=project.id,
            score=result.score,
            reasons=result.reasons,
            is_hard_rejected=result.rejected,
            rejection_reason=result.rejection_reason,
        )
        session.add(rec)
        await session.flush()

    # The link records that a bid exists for this recommendation. It does NOT claim we
    # recommended it — `was_recommended` is decided by whether the proposal came from our
    # drafting path, which imported bids never did.
    if proposal.recommendation_id is None and proposal.submitted_via == SubmittedVia.API:
        proposal.recommendation_id = rec.id

    if status != ProposalStatus.SUBMITTED:
        rec.status = RecommendationStatus.APPLIED


def _project_from(posting: JobPosting) -> Project:
    return Project(
        platform=posting.platform,
        external_id=posting.external_id,
        discovery_method=DiscoveryMethod.API_POLL,
        title=posting.title,
        description=posting.description,
        project_url=posting.url,
        required_skills=posting.skills_listed,
        work_type=posting.budget_type,
        min_budget=posting.budget_min,
        max_budget=posting.budget_max,
        currency=posting.currency,
        bid_information={"bid_count": posting.bid_count},
        posted_at=posting.posted_at,
    )


def _to_posting(project: Project) -> JobPosting:
    return JobPosting(
        platform=project.platform,
        external_id=project.external_id or "",
        title=project.title,
        description=project.description,
        url=project.project_url,
        skills_listed=project.required_skills or [],
        budget_type=project.work_type,
        budget_min=project.min_budget,
        budget_max=project.max_budget,
        currency=project.currency,
        bid_count=project.bid_count,
        posted_at=project.posted_at,
    )


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _as_datetime(value: Any):
    import datetime as dt

    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    return None
