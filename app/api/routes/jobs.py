from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import JobOut, JobPatch
from app.db.models import Job
from app.db.session import get_session
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


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    return await _owned_job(session, job_id)


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
