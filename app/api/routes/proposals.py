"""Everything you've drafted or bid on, with the score that recommended it.

The pairing is the whole value. A score is a prediction; a submitted bid and its outcome are the
result. Showing them side by side is what turns the scoring weights from a guess into something
you can actually calibrate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.schemas import ProposalOut, ProposalStats
from app.db.models import (
    FreelancerProfile,
    Project,
    Proposal,
    ProposalStatus,
    Recommendation,
    User,
)
from app.db.session import get_session
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(prefix="/proposals", tags=["proposals"])


def _out(
    proposal: Proposal,
    project: Project,
    rec: Recommendation | None,
    user: User,
    profile: FreelancerProfile,
) -> ProposalOut:
    return ProposalOut(
        id=proposal.id,
        recommendation_id=proposal.recommendation_id,
        user_id=user.id,
        user_email=user.email,
        freelancer_name=profile.display_name or user.name or user.email,
        was_recommended=proposal.recommendation_id is not None,
        project_title=project.title,
        project_url=project.project_url,
        platform=project.platform,
        external_id=project.external_id,
        score=rec.score if rec else None,
        reasons=(rec.reasons if rec else []) or [],
        proposal_text=proposal.proposal_text,
        bid_amount=proposal.bid_amount,
        estimated_days=proposal.estimated_days,
        currency=project.currency,
        status=proposal.status,
        submitted_via=proposal.submitted_via,
        external_bid_id=proposal.external_bid_id,
        submitted_at=proposal.submitted_at,
        drafted_at=proposal.drafted_at,
        created_at=proposal.created_at,
        model=proposal.model,
        input_tokens=proposal.input_tokens,
        output_tokens=proposal.output_tokens,
    )


@router.get("", response_model=list[ProposalOut])
async def list_proposals(
    status: str | None = Query(
        default=None, pattern="^(DRAFT|SUBMITTED|ACCEPTED|REJECTED|WITHDRAWN)$"
    ),
    limit: int = Query(default=100, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ProposalOut]:
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    query = (
        select(Proposal, Project, Recommendation)
        .join(Project, Project.id == Proposal.project_id)
        .outerjoin(Recommendation, Recommendation.id == Proposal.recommendation_id)
        .where(Proposal.freelancer_id == profile.id)
        .options(joinedload(Proposal.recommendation))
    )
    if status is not None:
        query = query.where(Proposal.status == status)

    # Newest activity first: a submitted bid is sorted by when it went out, a draft by when it
    # was written.
    query = query.order_by(
        func.coalesce(Proposal.submitted_at, Proposal.created_at).desc()
    ).limit(limit)

    rows = (await session.execute(query)).unique().all()
    return [_out(p, proj, rec, user, profile) for p, proj, rec in rows]


@router.get("/stats", response_model=ProposalStats)
async def stats(session: AsyncSession = Depends(get_session)) -> ProposalStats:
    """Does a higher score actually convert? This is where you find out."""
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    mine = Proposal.freelancer_id == profile.id

    async def count(*where) -> int:
        return await session.scalar(select(func.count(Proposal.id)).where(mine, *where)) or 0

    async def avg_score(*where) -> float | None:
        value = await session.scalar(
            select(func.avg(Recommendation.score))
            .select_from(Proposal)
            .join(Recommendation, Recommendation.id == Proposal.recommendation_id)
            .where(mine, *where)
        )
        return round(float(value), 1) if value is not None else None

    tokens = await session.scalar(
        select(func.coalesce(func.sum(Proposal.output_tokens), 0)).where(mine)
    )

    return ProposalStats(
        # No award-status sync exists yet, so every sent bid is awaiting an outcome we have not
        # asked for. See the outcome-tracking note in the README.
        outcome_tracking_enabled=False,
        awaiting_outcome=await count(Proposal.status == ProposalStatus.SUBMITTED),
        from_recommendation=await count(Proposal.recommendation_id.is_not(None)),
        self_directed=await count(Proposal.recommendation_id.is_(None)),
        total=await count(),
        drafted=await count(Proposal.status == ProposalStatus.DRAFT),
        submitted=await count(Proposal.status == ProposalStatus.SUBMITTED),
        accepted=await count(Proposal.status == ProposalStatus.ACCEPTED),
        rejected=await count(Proposal.status == ProposalStatus.REJECTED),
        avg_score_submitted=await avg_score(Proposal.status != ProposalStatus.DRAFT),
        avg_score_accepted=await avg_score(Proposal.status == ProposalStatus.ACCEPTED),
        total_output_tokens=int(tokens or 0),
    )
