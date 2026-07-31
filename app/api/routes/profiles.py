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
import random
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ConnectionOut,
    FullSyncOut,
    ProfileCard,
    ProfileDetail,
    ProfileIn,
    ProfileOut,
    SelectionIn,
    SkillNamesIn,
    SkillsAcceptIn,
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
from app.services.connections import disconnect_connection
from app.services.pipeline import run_cycle
from app.services.skill_suggest import (
    MAX_PROPOSAL_SAMPLES,
    SkillSuggestError,
    suggest_skills,
)
from app.services.users import (
    get_or_create_default_user,
    get_or_create_profile,
    get_or_create_profile_for_connection,
)

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


async def _connection_counts(
    session: AsyncSession, connection: PlatformConnection
) -> tuple[int, int]:
    """Proposals placed and won through one connection. Per account, never a shared total."""
    placed = (
        await session.scalar(
            select(func.count(Proposal.id)).where(Proposal.connection_id == connection.id)
        )
        or 0
    )
    won = (
        await session.scalar(
            select(func.count(Proposal.id)).where(
                Proposal.connection_id == connection.id,
                Proposal.status == ProposalStatus.ACCEPTED,
            )
        )
        or 0
    )
    return placed, won


def _avatar(profile: FreelancerProfile) -> str | None:
    """The profile's marketplace avatar, or None.

    Freelancer serves a grey placeholder for accounts with no picture; treat it as no picture so the
    UI can fall back to initials instead.
    """
    url = profile.avatar_url or ""
    return None if url.endswith("unknown.png") else (url or None)


async def _card(
    session: AsyncSession, profile: FreelancerProfile, user: User
) -> dict:
    connection = profile.connection
    recs, props, wins = await _counts(session, profile)
    skills = [s["name"] for s in (profile.skills or []) if s.get("name")]

    return {
        "id": profile.id,
        "display_name": profile.display_name or user.name or user.email,
        "headline": profile.headline,
        # The marketplace avatar mirrored onto the profile; fall back to the account's own image.
        "profile_image": _avatar(profile) or user.profile_image,
        "initials": _initials(profile.display_name or user.name or "", user.email),
        # Cards show a handful; the rest are counted rather than listed.
        "skills": skills[:8],
        "skill_count": len(skills),
        "rate_min": profile.rate_min,
        "rate_max": profile.rate_max,
        "currency": profile.currency,
        "availability": profile.availability,
        "status": profile.status,
        # One profile mirrors at most one account, so at most one platform.
        "platforms": [connection.platform] if connection else [],
        "is_selected": profile.is_selected,
        "last_synced_at": profile.last_synced_at,
        "bids_synced_at": profile.bids_synced_at,
        "recommendations": recs,
        "proposals": props,
        "wins": wins,
        "_connection": connection,
    }


def _connection_out(
    c: PlatformConnection,
    profile: FreelancerProfile | None,
    proposals: int = 0,
    wins: int = 0,
) -> ConnectionOut:
    """One connection as the API returns it, with the public profile it links to.

    The wire shape is deliberately flat and unchanged: the marketplace-mirror fields now live on the
    linked :class:`FreelancerProfile`, but the client still reads them here. Built in one place
    because both the profile detail and the connections list serve the same shape.
    """
    return ConnectionOut(
        id=c.id,
        proposals=proposals,
        wins=wins,
        platform=c.platform,
        platform_username=c.platform_username,
        scope=c.scope,
        status=c.status,
        connected_at=c.connected_at,
        is_selected=profile.is_selected if profile else False,
        rating=profile.rating if profile else None,
        total_reviews=profile.total_reviews if profile else None,
        avatar_url=_avatar(profile) if profile else None,
        display_name=profile.display_name if profile else None,
        tagline=profile.tagline if profile else None,
        summary=profile.summary if profile else None,
        account_skills=(profile.account_skills or []) if profile else [],
        hourly_rate=profile.hourly_rate if profile else None,
        currency=profile.currency if profile else None,
        country=profile.country if profile else None,
        portfolio_count=profile.portfolio_count if profile else None,
        member_since=profile.member_since if profile else None,
        last_synced_at=profile.last_synced_at if profile else None,
    )


@router.get("/profiles", response_model=list[ProfileCard])
async def list_profiles(session: AsyncSession = Depends(get_session)) -> list[ProfileCard]:
    """Every profile on this account — one per connected marketplace account.

    Selected first, then oldest, so the account switcher has a stable order and the scoped account
    leads.
    """
    user = await get_or_create_default_user(session)
    profiles = (
        await session.scalars(
            select(FreelancerProfile)
            .where(FreelancerProfile.user_id == user.id)
            .order_by(
                FreelancerProfile.is_selected.desc(), FreelancerProfile.created_at.asc()
            )
        )
    ).all()
    if not profiles:
        profiles = [await get_or_create_profile(session, user.id)]

    cards: list[ProfileCard] = []
    for profile in profiles:
        card = await _card(session, profile, user)
        card.pop("_connection")
        cards.append(ProfileCard(**card))
    return cards


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
    connection = card.pop("_connection")
    config = profile.config

    avg = await session.scalar(
        select(func.avg(Recommendation.score)).where(
            Recommendation.freelancer_id == profile.id,
            Recommendation.is_hard_rejected.is_(False),
        )
    )

    # The one account this profile mirrors, as a list for a stable response shape.
    connections = []
    if connection is not None and connection.disconnected_at is None:
        props, wins = await _connection_counts(session, connection)
        connections = [_connection_out(connection, profile, proposals=props, wins=wins)]

    return ProfileDetail(
        **card,
        bio=profile.bio,
        weighted_skills=profile.skills or [],
        portfolio=profile.portfolio or [],
        experience=profile.experience or [],
        education=profile.education or [],
        keywords_include=config.keywords_include or [],
        keywords_exclude=config.keywords_exclude or [],
        fixed_project_min=profile.fixed_project_min,
        crowded_at_bids=config.crowded_at_bids,
        min_match_score=config.min_match_score,
        weight_skills=config.weight_skills,
        weight_budget=config.weight_budget,
        weight_competition=config.weight_competition,
        weight_recency=config.weight_recency,
        proposal_notes=profile.proposal_notes,
        connections=connections,
        avg_score=round(float(avg), 1) if avg is not None else None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# --- the signed-in user's own profile (what the edit form reads and writes) ---



# ProfileIn fields that live on the config table rather than the profile itself.
_CONFIG_FIELDS = frozenset(
    {
        "keywords_include",
        "keywords_exclude",
        "crowded_at_bids",
        "min_match_score",
        "weight_skills",
        "weight_budget",
        "weight_competition",
        "weight_recency",
    }
)


def _out(profile: FreelancerProfile) -> ProfileOut:
    synced = profile.last_synced_at
    if synced is not None and synced.tzinfo is None:
        synced = synced.replace(tzinfo=dt.UTC)
    stale = synced is None or (utcnow() - synced) > STALE_AFTER
    config = profile.config
    # Built flat from two tables: the wire shape the edit form reads stays as it was, even though
    # the scoring knobs now live on ``profile.config``.
    return ProfileOut(
        display_name=profile.display_name,
        headline=profile.headline,
        skills=profile.skills or [],
        keywords_include=config.keywords_include or [],
        keywords_exclude=config.keywords_exclude or [],
        fixed_project_min=profile.fixed_project_min,
        rate_min=profile.rate_min,
        currency=profile.currency,
        country=profile.country,
        crowded_at_bids=config.crowded_at_bids,
        min_match_score=config.min_match_score,
        weight_skills=config.weight_skills,
        weight_budget=config.weight_budget,
        weight_competition=config.weight_competition,
        weight_recency=config.weight_recency,
        proposal_notes=profile.proposal_notes,
        suggested_skills=profile.suggested_skills or [],
        last_synced_at=profile.last_synced_at,
        updated_at=profile.updated_at,
        sync_is_stale=stale,
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
    config = profile.config

    data = payload.model_dump()
    data["skills"] = [s for s in data["skills"] if s.get("name")]
    for key, value in data.items():
        # Scoring knobs live on the config table; everything else on the profile itself.
        setattr(config if key in _CONFIG_FIELDS else profile, key, value)

    await session.commit()
    await session.refresh(profile)
    return _out(profile)


# --- skill suggestions (LLM proposes, the freelancer confirms) ---


@router.post("/profile/skills/suggest", response_model=ProfileOut)
async def suggest_profile_skills(session: AsyncSession = Depends(get_session)) -> ProfileOut:
    """Propose skills the freelancer's own evidence supports but their list omits.

    On-demand, not automatic: this spends an LLM call, so it runs when the freelancer asks for it
    rather than silently on every account refresh. Results land in ``suggested_skills`` and feed
    nothing until accepted — see ``services.skill_suggest``.
    """
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    proposal_texts = (
        await session.scalars(
            select(Proposal.proposal_text)
            .where(Proposal.freelancer_id == profile.id)
            .order_by(Proposal.drafted_at.desc())
            .limit(MAX_PROPOSAL_SAMPLES)
        )
    ).all()

    try:
        suggestions = await suggest_skills(profile, list(proposal_texts))
    except SkillSuggestError as exc:
        # A user pressed a button; a clear failure beats a silent empty list.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    profile.suggested_skills = [s.as_dict() for s in suggestions]
    await session.commit()
    await session.refresh(profile)
    return _out(profile)


@router.post("/profile/skills/accept", response_model=ProfileOut)
async def accept_profile_skills(
    payload: SkillsAcceptIn, session: AsyncSession = Depends(get_session)
) -> ProfileOut:
    """Confirm suggested skills into the real list. The freelancer may have edited a name or weight
    first, so the confirmed values — not the proposed ones — are what's stored. An accepted name is
    also cleared from the pending suggestions."""
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    accepted = [
        {"name": s.name.strip(), "weight": s.weight} for s in payload.skills if s.name.strip()
    ]

    # Merge into the existing skills, keeping order and letting an accepted weight update a skill
    # already listed under the same name. Rebuilt as a fresh list so SQLAlchemy sees the change.
    by_name: dict[str, dict] = {}
    order: list[str] = []
    for existing in profile.skills or []:
        name = str(existing.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in by_name:
            order.append(key)
        by_name[key] = {"name": name, "weight": existing.get("weight", 1)}
    for skill in accepted:
        key = skill["name"].lower()
        if key not in by_name:
            order.append(key)
        by_name[key] = skill
    profile.skills = [by_name[key] for key in order]

    accepted_keys = {s["name"].lower() for s in accepted}
    profile.suggested_skills = [
        p
        for p in (profile.suggested_skills or [])
        if str(p.get("name") or "").strip().lower() not in accepted_keys
    ]

    await session.commit()
    await session.refresh(profile)
    return _out(profile)


@router.post("/profile/skills/reject", response_model=ProfileOut)
async def reject_profile_skills(
    payload: SkillNamesIn, session: AsyncSession = Depends(get_session)
) -> ProfileOut:
    """Dismiss suggestions without accepting them."""
    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    drop = {n.strip().lower() for n in payload.names if n.strip()}
    profile.suggested_skills = [
        p
        for p in (profile.suggested_skills or [])
        if str(p.get("name") or "").strip().lower() not in drop
    ]

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
    bids = await sync_bids(session, user.id)

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


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(session: AsyncSession = Depends(get_session)) -> list[ConnectionOut]:
    """Every marketplace account linked to this user."""
    user = await get_or_create_default_user(session)
    rows = (
        await session.scalars(
            select(PlatformConnection)
            .where(
                PlatformConnection.user_id == user.id,
                PlatformConnection.disconnected_at.is_(None),
            )
            .order_by(PlatformConnection.connected_at)
        )
    ).all()
    out: list[ConnectionOut] = []
    for c in rows:
        placed, won = await _connection_counts(session, c)
        out.append(_connection_out(c, c.profile, proposals=placed, wins=won))
    return out


@router.put("/connections/selected", response_model=list[ConnectionOut])
async def select_connection(
    payload: SelectionIn, session: AsyncSession = Depends(get_session)
) -> list[ConnectionOut]:
    """Scope the app to one connected account, or to all of them with a null id.

    Stored on the user rather than in the browser so the choice survives a new device and so the
    API can honour it later without the client having to restate it on every request.
    """
    user = await get_or_create_default_user(session)

    owned: PlatformConnection | None = None
    if payload.connection_id is not None:
        owned = await session.scalar(
            select(PlatformConnection).where(
                PlatformConnection.id == payload.connection_id,
                PlatformConnection.user_id == user.id,
                PlatformConnection.disconnected_at.is_(None),
            )
        )
        # 404 rather than 403: whether someone else's connection exists is not ours to confirm.
        if owned is None:
            raise HTTPException(status_code=404, detail="No such connection.")

    # Selection lives on the profile now. Clear first, then set: the partial unique index rejects a
    # second selected row, so the two statements have to be in this order within the transaction.
    await session.execute(
        update(FreelancerProfile)
        .where(FreelancerProfile.user_id == user.id, FreelancerProfile.is_selected.is_(True))
        .values(is_selected=False)
    )
    await session.flush()
    if owned is not None:
        profile = owned.profile or await get_or_create_profile_for_connection(session, owned)
        profile.is_selected = True
    await session.commit()
    return await list_connections(session)


@router.delete("/connections/{connection_id}", status_code=204)
async def remove_connection(
    connection_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    """Disconnect a marketplace account.

    A soft delete: the credential is scrubbed immediately, but the row is kept and only hard-deleted
    later by the background purge — a destructive delete never runs inline against the live database
    (see ``services.connections``). Projects, recommendations and proposals already gathered through
    it stay regardless — they are your record of work, not the platform's, and losing your bid
    history because you rotated an account would be its own bug.
    """
    user = await get_or_create_default_user(session)
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.id == connection_id,
            PlatformConnection.user_id == user.id,
            PlatformConnection.disconnected_at.is_(None),
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Selection lives on the profile now. A disconnected account's profile can't be the scoped one.
    profile = connection.profile
    was_selected = bool(profile and profile.is_selected)
    if profile is not None:
        profile.is_selected = False
    disconnect_connection(connection)

    # If the account the app was scoped to is the one being removed, move the scope to another
    # still-connected account's profile rather than silently dropping to "all accounts". Flush first
    # so the removed connection's disconnected_at lands before we pick a replacement — the partial
    # unique index allows only one selected profile at a time.
    if was_selected:
        await session.flush()
        remaining = (
            await session.scalars(
                select(FreelancerProfile)
                .join(
                    PlatformConnection,
                    PlatformConnection.id == FreelancerProfile.connection_id,
                )
                .where(
                    FreelancerProfile.user_id == user.id,
                    PlatformConnection.disconnected_at.is_(None),
                )
            )
        ).all()
        if remaining:
            random.choice(remaining).is_selected = True

    await session.commit()
