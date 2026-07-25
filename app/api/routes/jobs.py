from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import BidAvailabilityOut, BidRequest, BidResult, JobOut, JobPatch
from app.db.models import Job, OAuthToken
from app.db.session import get_session
from app.services.bidding import (
    BidAvailability,
    BiddingError,
    check_availability,
    submit_bid_for_job,
)
from app.services.pipeline import rescore_all
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
async def list_jobs(
    status: str | None = Query(default=None, pattern="^(new|drafted|approved|dismissed)$"),
    rejected: bool | None = None,
    min_score: float | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[Job]:
    user = await get_or_create_default_user(session)

    query = select(Job).where(Job.user_id == user.id)
    if status is not None:
        query = query.where(Job.status == status)
    if rejected is not None:
        query = query.where(Job.rejected.is_(rejected))
    if min_score is not None:
        query = query.where(Job.score >= min_score)

    query = query.order_by(Job.score.desc(), Job.first_seen_at.desc()).limit(limit).offset(offset)
    return list(await session.scalars(query))


@router.post("/rescore")
async def rescore(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    """Re-run scoring over every stored job — call this after changing the profile."""
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)
    return {"rescored": await rescore_all(session, user.id, profile)}


@router.get("/bid-availability", response_model=BidAvailabilityOut)
async def bid_availability(session: AsyncSession = Depends(get_session)) -> BidAvailability:
    """Whether bidding is usable, and if not why — so the UI can explain, not just grey out."""
    user = await get_or_create_default_user(session)
    token_row = await session.scalar(
        select(OAuthToken).where(
            OAuthToken.user_id == user.id, OAuthToken.platform == "freelancer"
        )
    )
    return check_availability(token_row)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    return await _owned_job(session, job_id)


@router.post("/{job_id}/bid", response_model=BidResult)
async def place_bid(
    job_id: int, request: BidRequest, session: AsyncSession = Depends(get_session)
) -> BidResult:
    """Place a real bid on Freelancer.com.

    The only path in this codebase that submits anything. Requires `confirm: true` in the body on
    top of the install-level switch and the token's scope.
    """
    user = await get_or_create_default_user(session)
    job = await _owned_job(session, job_id)

    try:
        bid_id = await submit_bid_for_job(
            session,
            user.id,
            job,
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
    job_id: int, patch: JobPatch, session: AsyncSession = Depends(get_session)
) -> Job:
    job = await _owned_job(session, job_id)

    if patch.proposal_text is not None:
        job.proposal_text = patch.proposal_text
        if job.status == "new":
            job.status = "drafted"
    if patch.status is not None:
        job.status = patch.status

    await session.commit()
    await session.refresh(job)
    return job


async def _owned_job(session: AsyncSession, job_id: int) -> Job:
    user = await get_or_create_default_user(session)
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
