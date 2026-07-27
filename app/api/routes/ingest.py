"""Postings and profiles captured by the browser extension.

Upwork has no usable public API for discovery, so nothing here is polled. Every row arrives
because a signed-in person opened a page and clicked — which is why the stored
``discovery_method`` is ``PASTE_IN`` and not ``API_POLL``. Keeping those distinguishable is what
lets anyone later ask "where did this come from?" and get a true answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CapturedPosting,
    CapturedProfile,
    CaptureResult,
    PageParseIn,
    PageParseOut,
)
from app.auth.accounts import current_user
from app.db.models import (
    DiscoveryMethod,
    PlatformConnection,
    Project,
    Recommendation,
    User,
    utcnow,
)
from app.db.session import get_session
from app.services.page_parse import PageParseError, parse_page
from app.services.pipeline import to_posting
from app.services.scoring import score_job
from app.services.users import get_or_create_profile

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/posting", response_model=CaptureResult)
async def capture_posting(
    payload: CapturedPosting,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CaptureResult:
    """Store a posting read off a page, score it, and hand the score straight back.

    Scored inline rather than left for the next cycle because the person is looking at the job
    right now — a verdict that arrives half an hour later is a verdict they will never see.
    """
    profile = await get_or_create_profile(session, user.id)

    project = await session.scalar(
        select(Project).where(
            Project.platform == payload.platform, Project.external_id == payload.external_id
        )
    )
    created = project is None
    if project is None:
        project = Project(
            platform=payload.platform,
            external_id=payload.external_id,
            discovery_method=DiscoveryMethod.PASTE_IN,
        )
        session.add(project)

    project.title = payload.title
    project.description = payload.description
    project.project_url = payload.url
    project.required_skills = payload.skills
    project.work_type = payload.work_type
    project.min_budget = payload.budget_min
    project.max_budget = payload.budget_max
    project.currency = payload.currency
    project.bid_information = {"bid_count": payload.proposal_count}
    project.posted_at = payload.posted_at
    project.updated_at = utcnow()
    await session.flush()

    result = score_job(to_posting(project), profile)

    recommendation = await session.scalar(
        select(Recommendation).where(
            Recommendation.freelancer_id == profile.id, Recommendation.project_id == project.id
        )
    )
    if recommendation is None:
        recommendation = Recommendation(freelancer_id=profile.id, project_id=project.id)
        session.add(recommendation)

    recommendation.score = result.score
    recommendation.reasons = result.reasons
    recommendation.is_hard_rejected = result.rejected
    recommendation.rejection_reason = result.rejection_reason
    recommendation.updated_at = utcnow()
    await session.commit()

    return CaptureResult(
        project_id=project.id,
        recommendation_id=recommendation.id,
        created=created,
        score=result.score,
        rejected=result.rejected,
        rejection_reason=result.rejection_reason,
        reasons=result.reasons,
    )


@router.post("/profile", response_model=dict)
async def capture_profile(
    payload: CapturedProfile,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mirror your own marketplace profile onto a connection row.

    Reuses ``platform_connections`` rather than inventing a parallel table: an Upwork account is
    the same kind of thing as a Freelancer one — a handle, a picture, skills, a rate — and only
    differs in how it was obtained. It carries no tokens, so nothing here can place a bid.
    """
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == payload.platform,
            PlatformConnection.platform_username == payload.username,
        )
    )
    if connection is None:
        connection = PlatformConnection(
            user_id=user.id,
            platform=payload.platform,
            platform_username=payload.username,
            platform_user_id=payload.username,
            status="ACTIVE",
            # No OAuth happened, so there is no scope to record. Saying "read only" here would
            # imply a token exists.
            scope=None,
        )
        session.add(connection)

    connection.display_name = payload.display_name
    connection.tagline = payload.tagline
    connection.summary = payload.summary
    connection.account_skills = payload.skills
    connection.hourly_rate = payload.hourly_rate
    connection.currency = payload.currency
    connection.country = payload.country
    connection.avatar_url = payload.avatar_url
    connection.rating = payload.rating
    connection.total_reviews = payload.total_reviews
    connection.last_synced_at = utcnow()
    await session.commit()

    return {"connection_id": str(connection.id), "skills": len(payload.skills)}


@router.post("/parse", response_model=PageParseOut)
async def parse(
    payload: PageParseIn,
    user: User = Depends(current_user),
) -> PageParseOut:
    """Read a page with an LLM when the selectors come back empty.

    Deliberately a separate endpoint rather than an automatic fallback inside capture: this costs
    money and seconds per call, so it happens because someone chose it, not because a selector
    quietly broke.
    """
    try:
        result = await parse_page(payload.kind, payload.text)
    except PageParseError as exc:
        # 502, not 500: the failure is upstream at the model, and the client can sensibly retry.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PageParseOut(**result)
