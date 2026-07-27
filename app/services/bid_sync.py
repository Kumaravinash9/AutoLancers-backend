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

import datetime as dt
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
    PlatformConnection,
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

    connections = (
        await session.scalars(
            select(PlatformConnection).where(
                PlatformConnection.user_id == user_id,
                PlatformConnection.platform == "freelancer",
                PlatformConnection.status == "ACTIVE",
            )
        )
    ).all()
    if not connections:
        report.error = "No freelancer account connected."
        return report

    # Every connected account contributes its own bids. Syncing only the first would silently
    # under-report someone operating two.
    for connection in connections:
        await _sync_one(session, user_id, profile, connection, report)

    profile.bids_synced_at = utcnow()
    await session.commit()
    logger.info("Bid sync: %s", report.as_dict())
    return report


async def _sync_one(
    session: AsyncSession,
    user_id: uuid.UUID,
    profile: FreelancerProfile,
    connection: PlatformConnection,
    report: SyncReport,
) -> SyncReport:
    try:
        token = await get_valid_access_token(session, user_id, connection=connection)
    except OAuthError as exc:
        report.error = str(exc)
        return report

    client = FreelancerClient(access_token=token)

    try:
        me = await client.fetch_self()
        bidder_id = int(me.get("id") or 0)
        if not bidder_id:
            raise FreelancerAPIError("Could not read own user id")
        await _store_identity(session, profile, connection, me)
        bids = await client.fetch_my_bids(bidder_id)
    except FreelancerAPIError as exc:
        report.error = str(exc)
        logger.warning("Bid sync failed: %s", exc)
        return report

    report.fetched += len(bids)
    if not bids:
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
            outcome = await _apply(session, profile, bid, known, connection)
        except Exception:
            logger.exception("Could not sync bid %s", bid.get("id"))
            continue
        report.linked += int(outcome.linked)
        report.imported += int(outcome.imported)
        report.outcomes_updated += int(outcome.outcome_changed)

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
    connection: PlatformConnection,
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

    proposal.connection_id = connection.id
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
        result = score_job(to_posting(project), profile)
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


def to_posting(project: Project) -> JobPosting:
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


async def _store_identity(
    session: AsyncSession,
    profile: FreelancerProfile,
    connection: PlatformConnection,
    me: dict[str, Any],
) -> None:
    """Save the marketplace's own view of this account: picture, handle, rating and public profile.

    Freelancer serves several avatar sizes; prefer the large CDN one, since an image scaled down
    looks better than one scaled up.

    Every connection field here is overwritten from the marketplace on each sync, deliberately —
    this is a mirror of what clients see there, not something editable on our side. The freelancer's
    home country and currency are also mirrored onto ``profile`` (see ``_mirror_home_to_profile``),
    which is where the rest of the app reads them.
    """
    avatar = (
        me.get("avatar_large_cdn")
        or me.get("avatar_cdn")
        or me.get("avatar_large")
        or me.get("avatar")
    )
    if avatar:
        # The API returns protocol-relative URLs in places; a bare //host path won't load.
        connection.avatar_url = f"https:{avatar}" if avatar.startswith("//") else avatar

    connection.platform_user_id = str(me.get("id") or "") or connection.platform_user_id
    connection.platform_username = me.get("username") or connection.platform_username

    reputation = (me.get("reputation") or {}).get("entire_history") or {}
    if reputation.get("overall") is not None:
        connection.rating = float(reputation["overall"])
    if reputation.get("reviews") is not None:
        connection.total_reviews = int(reputation["reviews"])

    connection.display_name = me.get("public_name") or me.get("display_name")
    connection.tagline = me.get("tagline")
    connection.summary = me.get("profile_description")
    connection.account_skills = [
        job["name"] for job in (me.get("jobs") or []) if isinstance(job, dict) and job.get("name")
    ]
    connection.hourly_rate = _as_float(me.get("hourly_rate"))
    connection.currency = (me.get("primary_currency") or {}).get("code")
    connection.country = ((me.get("location") or {}).get("country") or {}).get("name")
    connection.portfolio_count = _as_int(me.get("portfolio_count"))

    registered = me.get("registration_date")
    if isinstance(registered, int | float):
        connection.member_since = dt.datetime.fromtimestamp(registered, tz=dt.UTC)

    connection.last_synced_at = utcnow()

    _mirror_home_to_profile(profile, connection)


def _mirror_home_to_profile(profile: FreelancerProfile, connection: PlatformConnection) -> None:
    """Copy the account's home country/currency onto the profile, which is where the rest of the app
    reads them (scoring floors and the local-currency display).

    Only from the account the app is scoped to — or the first one to arrive, so a fresh profile gets
    populated. Currency is adopted only while no budget floor has been set: once the freelancer has
    entered floors, the currency those numbers are stated in is theirs to change, not the sync's to
    silently reinterpret.
    """
    if not connection.is_selected and profile.country:
        return  # a non-selected account doesn't override an already-populated home
    if connection.country:
        profile.country = connection.country
    # Falsy (0 or an unset None) means no floor entered yet — safe to adopt the account currency.
    if connection.currency and not profile.rate_min and not profile.fixed_project_min:
        profile.currency = connection.currency


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None
