"""Profiles — browsing, opening, and editing.

One module because it is one resource. Three shapes serve three jobs and that is deliberate:
``ProfileCard`` carries enough to recognise a profile in a grid, ``ProfileDetail`` everything
behind a click, and ``ProfileIn``/``ProfileOut`` the editable subset. Sending the full record to
render a name and an avatar would ship every portfolio entry and scoring weight with it.

``/profile`` (singular) is the signed-in user's own profile — the shape the edit form posts back.
``/profiles`` is the collection.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ConnectionOut,
    FullSyncOut,
    ProfileCard,
    ProfileDetail,
    ProfileIn,
    ProfileOut,
)
from app.db.models import (
    FreelancerProfile,
    PlatformConnection,
    Proposal,
    ProposalStatus,
    Recommendation,
    User,
    utcnow,
)
from app.db.session import get_session
from app.services.bid_sync import sync_bids
from app.services.pipeline import run_cycle
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(tags=["profiles"])

# Freelancer expects cached marketplace data refreshed at least daily.
STALE_AFTER = dt.timedelta(hours=24)


def _initials(name: str, email: str) -> str:
    """Two letters for the avatar when there's no image. Falls back to the email."""
    source = (name or email or "?").strip()
    parts = [p for p in source.replace("@", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return source[:2].upper()


async def _counts(session: AsyncSession, profile: FreelancerProfile) -> tuple[int, int, int]:
    recs = (
        await session.scalar(
            select(func.count(Recommendation.id)).where(
                Recommendation.freelancer_id == profile.id
            )
        )
        or 0
    )
    props = (
        await session.scalar(
            select(func.count(Proposal.id)).where(Proposal.freelancer_id == profile.id)
        )
        or 0
    )
    wins = (
        await session.scalar(
            select(func.count(Proposal.id)).where(
                Proposal.freelancer_id == profile.id,
                Proposal.status == ProposalStatus.ACCEPTED,
            )
        )
        or 0
    )
    return recs, props, wins


async def _card(
    session: AsyncSession, profile: FreelancerProfile, user: User
) -> dict:
    connections = (
        await session.scalars(
            select(PlatformConnection).where(PlatformConnection.user_id == user.id)
        )
    ).all()
    recs, props, wins = await _counts(session, profile)
    skills = [s["name"] for s in (profile.skills or []) if s.get("name")]

    return {
        "id": profile.id,
        "display_name": profile.display_name or user.name or user.email,
        "headline": profile.headline,
        "profile_image": user.profile_image,
        "initials": _initials(profile.display_name or user.name or "", user.email),
        # Cards show a handful; the rest are counted rather than listed.
        "skills": skills[:8],
        "skill_count": len(skills),
        "rate_min": profile.rate_min,
        "rate_max": profile.rate_max,
        "currency": profile.currency,
        "availability": profile.availability,
        "status": profile.status,
        "platforms": [c.platform for c in connections],
        "last_synced_at": profile.last_synced_at,
        "bids_synced_at": profile.bids_synced_at,
        "recommendations": recs,
        "proposals": props,
        "wins": wins,
        "_connections": connections,
    }


@router.get("/profiles", response_model=list[ProfileCard])
async def list_profiles(session: AsyncSession = Depends(get_session)) -> list[ProfileCard]:
    """Every profile on this account.

    One today — the model is 1:1 with a user — but returning a list means an agency with several
    operating profiles doesn't need a different endpoint later.
    """
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)
    card = await _card(session, profile, user)
    card.pop("_connections")
    return [ProfileCard(**card)]


@router.get("/profiles/{profile_id}", response_model=ProfileDetail)
async def get_profile(
    profile_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> ProfileDetail:
    user = await get_or_create_default_user(session)
    profile = await session.scalar(
        select(FreelancerProfile).where(
            FreelancerProfile.id == profile_id, FreelancerProfile.user_id == user.id
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    card = await _card(session, profile, user)
    connections = card.pop("_connections")

    avg = await session.scalar(
        select(func.avg(Recommendation.score)).where(
            Recommendation.freelancer_id == profile.id,
            Recommendation.is_hard_rejected.is_(False),
        )
    )

    return ProfileDetail(
        **card,
        bio=profile.bio,
        weighted_skills=profile.skills or [],
        portfolio=profile.portfolio or [],
        experience=profile.experience or [],
        education=profile.education or [],
        keywords_include=profile.keywords_include or [],
        keywords_exclude=profile.keywords_exclude or [],
        fixed_project_min=profile.fixed_project_min,
        max_existing_bids=profile.max_existing_bids,
        min_match_score=profile.min_match_score,
        weight_skills=profile.weight_skills,
        weight_budget=profile.weight_budget,
        weight_competition=profile.weight_competition,
        weight_recency=profile.weight_recency,
        proposal_notes=profile.proposal_notes,
        connections=[
            ConnectionOut(
                platform=c.platform,
                platform_username=c.platform_username,
                scope=c.scope,
                rating=c.rating,
                total_reviews=c.total_reviews,
                status=c.status,
                connected_at=c.connected_at,
                last_synced_at=c.last_synced_at,
            )
            for c in connections
        ],
        avg_score=round(float(avg), 1) if avg is not None else None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# --- the signed-in user's own profile (what the edit form reads and writes) ---



def _out(profile: FreelancerProfile) -> ProfileOut:
    synced = profile.last_synced_at
    if synced is not None and synced.tzinfo is None:
        synced = synced.replace(tzinfo=dt.UTC)
    stale = synced is None or (utcnow() - synced) > STALE_AFTER
    return ProfileOut.model_validate(profile, from_attributes=True).model_copy(
        update={"sync_is_stale": stale}
    )


@router.get("/profile", response_model=ProfileOut)
async def read_profile(session: AsyncSession = Depends(get_session)) -> ProfileOut:
    user = await get_or_create_default_user(session)
    return _out(await get_or_create_profile(session, user.id))


@router.put("/profile", response_model=ProfileOut)
async def update_profile(
    payload: ProfileIn, session: AsyncSession = Depends(get_session)
) -> ProfileOut:
    """Replace the profile.

    Scores already stored are left alone — call ``POST /jobs/rescore`` to apply the new weights.
    Keeping them separate means you can make several edits before paying for one re-score.
    """
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    data = payload.model_dump()
    data["skills"] = [s for s in data["skills"] if s.get("name")]
    for key, value in data.items():
        setattr(profile, key, value)

    await session.commit()
    await session.refresh(profile)
    return _out(profile)


@router.post("/profiles/{profile_id}/sync", response_model=FullSyncOut)
async def sync_everything(
    profile_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> FullSyncOut:
    """Refresh everything for this profile: new work from the marketplace, and your own results.

    Run in that order deliberately. Discovery can create the project a bid points at, so doing it
    first means fewer bids arrive referencing something we have to fetch separately.

    Each half is reported on its own. They fail for different reasons — discovery can be rate
    limited while the bid pull is fine, or the token can lack scope for one and not the other —
    and a single "sync failed" would hide which.
    """
    user = await get_or_create_default_user(session)
    profile = await session.scalar(
        select(FreelancerProfile).where(
            FreelancerProfile.id == profile_id, FreelancerProfile.user_id == user.id
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    board = await run_cycle(session, trigger="manual")
    # A failed board fetch must not stop the bid pull: they are independent.
    bids = await sync_bids(session, user.id, profile)

    await session.refresh(profile)
    return FullSyncOut(
        board_fetched=board.fetched,
        board_new=board.new,
        board_changed=board.updated,
        board_drafted=board.drafted,
        board_error=board.error,
        bids_fetched=bids.fetched,
        bids_imported=bids.imported,
        outcomes_updated=bids.outcomes_updated,
        bids_error=bids.error,
        last_synced_at=profile.last_synced_at,
        bids_synced_at=profile.bids_synced_at,
    )
