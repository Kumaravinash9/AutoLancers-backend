"""Admin portal.

Every route here depends on ``require_admin``. That dependency is the access control — the
frontend hiding the link is presentation, and presentation is not security.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AdminOverview, AdminUserOut, CycleRunOut, RoleUpdate, UserOut
from app.auth.accounts import require_admin
from app.db.models import (
    CycleRun,
    FreelancerProfile,
    PlatformConnection,
    Proposal,
    Recommendation,
    Role,
    User,
    utcnow,
)
from app.db.session import get_session

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview", response_model=AdminOverview)
async def overview(session: AsyncSession = Depends(get_session)) -> AdminOverview:
    """Platform health at a glance: is discovery working, is drafting working, what has it cost."""
    day_ago = utcnow() - dt.timedelta(hours=24)

    total_users = await session.scalar(select(func.count(User.id))) or 0
    active_users = (
        await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    )
    connected = (
        await session.scalar(
            select(func.count(PlatformConnection.id)).where(
                PlatformConnection.disconnected_at.is_(None)
            )
        )
        or 0
    )

    total_jobs = await session.scalar(select(func.count(Recommendation.id))) or 0
    matched = (
        await session.scalar(
            select(func.count(Recommendation.id)).where(
                Recommendation.is_hard_rejected.is_(False)
            )
        )
        or 0
    )
    drafted = (
        await session.scalar(
            select(func.count(Proposal.id)).where(Proposal.proposal_text.isnot(None))
        )
        or 0
    )
    bids = (
        await session.scalar(
            select(func.count(Proposal.id)).where(Proposal.external_bid_id.isnot(None))
        )
        or 0
    )

    tokens_in = await session.scalar(select(func.coalesce(func.sum(Proposal.input_tokens), 0)))
    tokens_out = await session.scalar(
        select(func.coalesce(func.sum(Proposal.output_tokens), 0))
    )

    cycles_24h = (
        await session.scalar(select(func.count(CycleRun.id)).where(CycleRun.started_at >= day_ago))
        or 0
    )
    failed_24h = (
        await session.scalar(
            select(func.count(CycleRun.id)).where(
                CycleRun.started_at >= day_ago, CycleRun.error.isnot(None)
            )
        )
        or 0
    )
    draft_failures_24h = (
        await session.scalar(
            select(func.coalesce(func.sum(CycleRun.draft_failures), 0)).where(
                CycleRun.started_at >= day_ago
            )
        )
        or 0
    )
    last_cycle = await session.scalar(select(func.max(CycleRun.started_at)))

    return AdminOverview(
        total_users=total_users,
        active_users=active_users,
        connected_accounts=connected,
        total_jobs=total_jobs,
        matched_jobs=matched,
        drafted_jobs=drafted,
        bids_placed=bids,
        proposal_input_tokens=int(tokens_in or 0),
        proposal_output_tokens=int(tokens_out or 0),
        cycles_24h=cycles_24h,
        failed_cycles_24h=failed_24h,
        draft_failures_24h=int(draft_failures_24h or 0),
        last_cycle_at=last_cycle,
    )


@router.get("/cycles", response_model=list[CycleRunOut])
async def cycles(
    limit: int = Query(default=25, le=200), session: AsyncSession = Depends(get_session)
) -> list[CycleRun]:
    """Recent poll cycles — the difference between 'a quiet day' and 'discovery is broken'."""
    return list(
        await session.scalars(
            select(CycleRun).order_by(CycleRun.started_at.desc()).limit(limit)
        )
    )


@router.get("/users", response_model=list[AdminUserOut])
async def users(session: AsyncSession = Depends(get_session)) -> list[AdminUserOut]:
    rows = list(await session.scalars(select(User).order_by(User.created_at.desc())))

    out: list[AdminUserOut] = []
    for user in rows:
        # Recommendations belong to a profile, which belongs to a user.
        job_count = (
            await session.scalar(
                select(func.count(Recommendation.id))
                .join(FreelancerProfile, FreelancerProfile.id == Recommendation.freelancer_id)
                .where(FreelancerProfile.user_id == user.id)
            )
            or 0
        )
        token = await session.scalar(
            select(PlatformConnection).where(
                PlatformConnection.user_id == user.id,
                PlatformConnection.disconnected_at.is_(None),
            )
        )
        out.append(
            AdminUserOut(
                id=user.id,
                email=user.email,
                role=user.role,
                is_active=user.is_active,
                created_at=user.created_at,
                last_login_at=user.last_login_at,
                job_count=job_count,
                connected=token is not None,
                connection_scope=token.scope if token else None,
            )
        )
    return out


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def set_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> User:
    """Promote or demote. An admin cannot demote themselves — that's how you lock everyone out."""
    if user_id == admin.id and body.role != Role.ADMIN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You cannot remove your own admin role. Promote someone else first.",
        )

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    user.role = body.role
    await session.commit()
    await session.refresh(user)
    return user


@router.patch("/users/{user_id}/active", response_model=UserOut)
async def set_active(
    user_id: uuid.UUID,
    active: bool,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> User:
    if user_id == admin.id and not active:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot deactivate your own account."
        )

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    user.is_active = active
    await session.commit()
    await session.refresh(user)
    return user
