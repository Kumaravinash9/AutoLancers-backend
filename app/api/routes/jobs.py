"""The board: recommendations, flattened with their project and proposal.

The route is still ``/jobs`` and still returns the flat shape clients already consume. Splitting
projects from recommendations was a storage decision — it shouldn't ripple out into every caller.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import BidAvailabilityOut, BidRequest, BidResult, JobOut, JobPatch
from app.db.models import (
    PlatformConnection,
    Project,
    Proposal,
    ProposalAIVersion,
    ProposalStatus,
    Recommendation,
    RecommendationStatus,
    SubmittedVia,
    User,
    utcnow,
)
from app.db.session import get_session
from app.services.bidding import (
    BidAvailability,
    BiddingError,
    check_availability,
    submit_bid_for_recommendation,
)
from app.services.currency import convert
from app.services.drafting import DraftingError, draft_proposal
from app.services.pipeline import rescore_all, to_posting
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(prefix="/jobs", tags=["jobs"])


async def _local_currency(session: AsyncSession, user: User | None = None) -> str | None:
    """The freelancer's home currency for display, read from the profile — where sync mirrors it
    from the connected account's country. It's the same currency scoring floors use, so the
    converted figure and the floor reasoning always agree."""
    profile = await _profile_for(session, user)
    return profile.currency


def _flatten(rec: Recommendation, local_currency: str | None = None) -> JobOut:
    project = rec.project
    proposal = rec.proposal
    listed = project.currency
    # Only fill the local figures when the job is in a different currency and we can convert it;
    # otherwise leave them null so the UI falls back to the listed budget alone.
    local_min = local_max = out_local = None
    if local_currency and listed and local_currency.upper() != listed.upper():
        converted_min = convert(project.min_budget, listed, local_currency)
        converted_max = convert(project.max_budget, listed, local_currency)
        if converted_min is not None or converted_max is not None:
            out_local = local_currency
            local_min = round(converted_min) if converted_min is not None else None
            local_max = round(converted_max) if converted_max is not None else None
    return JobOut(
        id=rec.id,
        platform=project.platform,
        external_id=project.external_id,
        title=project.title,
        description=project.description,
        url=project.project_url,
        skills_listed=project.required_skills or [],
        budget_type=project.work_type,
        budget_min=project.min_budget,
        budget_max=project.max_budget,
        currency=project.currency,
        local_currency=out_local,
        budget_min_local=local_min,
        budget_max_local=local_max,
        bid_count=project.bid_count,
        posted_at=project.posted_at,
        score=rec.score,
        reasons=rec.reasons or [],
        rejected=rec.is_hard_rejected,
        rejection_reason=rec.rejection_reason,
        proposal_text=proposal.proposal_text if proposal else None,
        status=rec.status,
        first_seen_at=rec.recommended_at,
        bid_amount=proposal.bid_amount if proposal else None,
        bid_period_days=proposal.estimated_days if proposal else None,
        bid_submitted_at=proposal.submitted_at if proposal else None,
        external_bid_id=proposal.external_bid_id if proposal else None,
        has_changes=rec.has_changes,
        changed_at=rec.changed_at,
    )


async def _profile_for(session: AsyncSession, user: User | None):
    """The profile whose board we're reading.

    Falls back to the default account so the poller and the CLI scripts keep working without a
    signed-in request.
    """
    owner = user or await get_or_create_default_user(session)
    return await get_or_create_profile(session, owner.id)


async def _maybe_user(session: AsyncSession) -> User | None:
    return None


@router.get("", response_model=list[JobOut])
async def list_jobs(
    status: str | None = Query(default=None, pattern="^(NEW|VIEWED|APPLIED|DISMISSED)$"),
    rejected: bool | None = None,
    min_score: float | None = None,
    sort: str = Query(default="recent", pattern="^(recent|score)$"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[JobOut]:
    profile = await _profile_for(session, None)

    query = select(Recommendation).where(Recommendation.freelancer_id == profile.id)
    if status is not None:
        query = query.where(Recommendation.status == status)
    if rejected is not None:
        query = query.where(Recommendation.is_hard_rejected.is_(rejected))
    if min_score is not None:
        query = query.where(Recommendation.score >= min_score)

    # Newest first by default: on a board refreshed every half hour, what changed since you last
    # looked matters more than a score you have already seen. Postings without a stated date sort
    # last rather than to the top.
    if sort == "recent":
        query = query.join(Recommendation.project).order_by(
            Project.posted_at.desc().nullslast(), Recommendation.score.desc()
        )
    else:
        query = query.order_by(
            Recommendation.score.desc(), Recommendation.recommended_at.desc()
        )

    query = query.limit(limit).offset(offset)
    rows = (await session.scalars(query)).unique().all()
    local = await _local_currency(session)
    return [_flatten(rec, local) for rec in rows]


@router.post("/rescore")
async def rescore(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    """Re-run scoring over every stored recommendation — call after changing the profile."""
    profile = await _profile_for(session, None)
    return {"rescored": await rescore_all(session, profile)}


@router.get("/bid-availability", response_model=BidAvailabilityOut)
async def bid_availability(session: AsyncSession = Depends(get_session)) -> BidAvailability:
    """Whether bidding is usable, and if not why — so the UI can explain, not just grey out."""
    user = await get_or_create_default_user(session)
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == "freelancer",
            PlatformConnection.disconnected_at.is_(None),
        )
    )
    return check_availability(connection)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> JobOut:
    rec = await _owned(session, job_id)
    # Opening it is reading it: clear the change flag and stop calling it NEW.
    if rec.status == RecommendationStatus.NEW or rec.has_changes:
        rec.status = (
            RecommendationStatus.VIEWED if rec.status == RecommendationStatus.NEW else rec.status
        )
        rec.has_changes = False
        await session.commit()
    return _flatten(rec)


@router.post("/{job_id}/bid", response_model=BidResult)
async def place_bid(
    job_id: uuid.UUID, request: BidRequest, session: AsyncSession = Depends(get_session)
) -> BidResult:
    """Place a real bid. The only path in this codebase that submits anything."""
    user = await get_or_create_default_user(session)
    rec = await _owned(session, job_id)

    try:
        bid_id = await submit_bid_for_recommendation(
            session,
            user.id,
            rec,
            amount=request.amount,
            period_days=request.period_days,
            confirm=request.confirm,
            milestone_percentage=request.milestone_percentage,
        )
    except BiddingError as exc:
        # 409: the request was well-formed, the current state just doesn't permit it.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return BidResult(bid_id=bid_id, amount=request.amount, period_days=request.period_days)


@router.patch("/{job_id}", response_model=JobOut)
async def patch_job(
    job_id: uuid.UUID, patch: JobPatch, session: AsyncSession = Depends(get_session)
) -> JobOut:
    rec = await _owned(session, job_id)

    if patch.proposal_text is not None:
        proposal = rec.proposal
        if proposal is None:
            # Hand-written before the drafter got to it — still a proposal.
            proposal = Proposal(
                project_id=rec.project_id,
                freelancer_id=rec.freelancer_id,
                recommendation_id=rec.id,
                status=ProposalStatus.DRAFT,
                submitted_via=SubmittedVia.MANUAL_COPY,
            )
            session.add(proposal)
        proposal.proposal_text = patch.proposal_text
        proposal.updated_at = utcnow()

    if patch.status is not None:
        rec.status = patch.status

    await session.commit()
    await session.refresh(rec)
    return _flatten(rec)


async def _owned(session: AsyncSession, job_id: uuid.UUID) -> Recommendation:
    profile = await _profile_for(session, None)
    rec = await session.scalar(
        select(Recommendation).where(
            Recommendation.id == job_id, Recommendation.freelancer_id == profile.id
        )
    )
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return rec


# Kept so the enum values used by the frontend stay discoverable from one place.
STATUSES = [s.value for s in RecommendationStatus]


@router.post("/{job_id}/draft", response_model=JobOut)
async def draft(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> JobOut:
    """Write a proposal for this job now, rather than waiting for the poller to reach it.

    The cycle only drafts the top few per run, so a job further down the board would otherwise
    never get one. Re-running replaces the current text and keeps the previous generation as a
    version, so regenerating is never destructive.
    """
    rec = await _owned(session, job_id)
    profile = await _profile_for(session, None)

    try:
        generated = await draft_proposal(to_posting(rec.project), profile)
    except DraftingError as exc:
        # 502: the failure is upstream at the model provider, not in the request.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    proposal = rec.proposal
    if proposal is None:
        proposal = Proposal(
            project_id=rec.project_id,
            freelancer_id=profile.id,
            recommendation_id=rec.id,
            status=ProposalStatus.DRAFT,
        )
        session.add(proposal)
        await session.flush()

    version = (
        await session.scalar(
            select(func.count(ProposalAIVersion.id)).where(
                ProposalAIVersion.proposal_id == proposal.id
            )
        )
        or 0
    ) + 1
    session.add(
        ProposalAIVersion(
            proposal_id=proposal.id,
            version=version,
            generated_text=generated.text,
            model=generated.model,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
        )
    )

    proposal.proposal_text = generated.text
    proposal.model = generated.model
    proposal.input_tokens = generated.input_tokens
    proposal.output_tokens = generated.output_tokens
    proposal.drafted_at = utcnow()

    await session.commit()
    await session.refresh(rec)
    return _flatten(rec)
