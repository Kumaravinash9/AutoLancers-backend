"""Everything you've drafted or bid on, with the score that recommended it.

The pairing is the whole value. A score is a prediction; a submitted bid and its outcome are the
result. Showing them side by side is what turns the scoring weights from a guess into something
you can actually calibrate.
"""

from __future__ import annotations

import uuid

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
from app.services.bid_sync import sync_bids
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
    connection_id: uuid.UUID | None = Query(
        default=None,
        description="Scope to one marketplace account. Omit for all of them.",
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
    if connection_id is not None:
        query = query.where(Proposal.connection_id == connection_id)

    # Newest activity first: a submitted bid is sorted by when it went out, a draft by when it
    # was written.
    query = query.order_by(
        func.coalesce(Proposal.submitted_at, Proposal.created_at).desc()
    ).limit(limit)

    rows = (await session.execute(query)).unique().all()
    return [_out(p, proj, rec, user, profile) for p, proj, rec in rows]


@router.get("/stats", response_model=ProposalStats)
async def stats(
    connection_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ProposalStats:
    """Does a higher score actually convert? This is where you find out."""
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    mine = Proposal.freelancer_id == profile.id
    # Scoping to one account changes what every figure below means, so it is applied to the
    # base predicate rather than to each query in turn.
    scope = (Proposal.connection_id == connection_id,) if connection_id else ()

    async def count(*where) -> int:
        return (
            await session.scalar(
                select(func.count(Proposal.id)).where(mine, *scope, *where)
            )
            or 0
        )

    async def avg_score(*where) -> float | None:
        # Join through the project, not through recommendation_id. An imported bid deliberately
        # has no recommendation_id — we didn't recommend it — but the sync still scores its
        # project, and that score is exactly what calibration needs. Joining on the FK would
        # silently restrict the average to bids we suggested, which is the flattering half.
        value = await session.scalar(
            select(func.avg(Recommendation.score))
            .select_from(Proposal)
            .join(
                Recommendation,
                (Recommendation.project_id == Proposal.project_id)
                & (Recommendation.freelancer_id == Proposal.freelancer_id),
            )
            .where(mine, *scope, *where)
        )
        return round(float(value), 1) if value is not None else None

    tokens = await session.scalar(
        select(func.coalesce(func.sum(Proposal.output_tokens), 0)).where(mine, *scope)
    )

    return ProposalStats(
        # True only once bids have actually been pulled back: before that, zero wins means we
        # never asked, not that you lost.
        outcome_tracking_enabled=profile.bids_synced_at is not None,
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


@router.post("/sync")
async def sync(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """Pull real bids and their award status back from Freelancer.

    This is the only source of truth for whether you were selected — nothing else in the system
    can know that.
    """
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)
    return (await sync_bids(session, user.id, profile)).as_dict()
