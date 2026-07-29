"""User and profile helpers.

A user has one :class:`FreelancerProfile` per connected marketplace account (plus, before any
account is connected, one connection-less default). Recommendations and proposals hang off the
profile, not the user, so resolving the right profile from whatever a caller holds — a user, or a
specific connection — happens here so no caller has to know the rule.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FreelancerProfile,
    PlatformConnection,
    ProfileConfig,
    Role,
    User,
)

# Used by the CLI scripts and the poller, which run without a signed-in request.
DEFAULT_USER_EMAIL = "owner@localhost"


async def get_or_create_default_user(session: AsyncSession) -> User:
    """The account background jobs act as when no one is signed in."""
    user = await session.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    if user is None:
        user = User(email=DEFAULT_USER_EMAIL, role=Role.ADMIN, name="Owner")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


def _ensure_config(session: AsyncSession, profile: FreelancerProfile) -> FreelancerProfile:
    """Guarantee a profile carries its scoring config. A profile without one can't be scored."""
    if profile.config is None:
        profile.config = ProfileConfig()
        session.add(profile.config)
    return profile


async def get_or_create_profile(
    session: AsyncSession, user_id: uuid.UUID
) -> FreelancerProfile:
    """The profile the app is scoped to for this user.

    Resolves the user's ``is_selected`` profile, falling back to their oldest, and creating a
    connection-less default if they have none yet. This is what callers that hold only a user reach
    for — the poller, the CLI, and every read that isn't already about one specific account.
    """
    profile = await session.scalar(
        select(FreelancerProfile)
        .where(FreelancerProfile.user_id == user_id)
        # Selected first, then oldest — a stable, single answer when several exist.
        .order_by(FreelancerProfile.is_selected.desc(), FreelancerProfile.created_at.asc())
    )
    if profile is None:
        profile = FreelancerProfile(user_id=user_id, is_selected=True, config=ProfileConfig())
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        return profile

    if profile.config is None:
        _ensure_config(session, profile)
        await session.commit()
        await session.refresh(profile)
    return profile


async def get_or_create_profile_for_connection(
    session: AsyncSession, connection: PlatformConnection
) -> FreelancerProfile:
    """The profile that mirrors one specific marketplace account (1:1 with the connection).

    This is what the enrichment and bid-sync paths use, since they act on one account at a time. A
    user's first connection adopts their connection-less default profile rather than spawning a
    second; later connections get their own. The very first profile a user gets is the selected one.
    """
    profile = await session.scalar(
        select(FreelancerProfile).where(FreelancerProfile.connection_id == connection.id)
    )
    if profile is not None:
        if profile.config is None:
            _ensure_config(session, profile)
            await session.commit()
            await session.refresh(profile)
        return profile

    # Adopt an existing connection-less default (the fresh-account placeholder) if any, so a first
    # connection enriches the profile the user already has rather than duplicating it.
    profile = await session.scalar(
        select(FreelancerProfile).where(
            FreelancerProfile.user_id == connection.user_id,
            FreelancerProfile.connection_id.is_(None),
        )
    )
    has_any = await session.scalar(
        select(FreelancerProfile.id).where(FreelancerProfile.user_id == connection.user_id)
    )

    if profile is None:
        profile = FreelancerProfile(
            user_id=connection.user_id,
            config=ProfileConfig(),
            # First profile for this user becomes the selected one.
            is_selected=has_any is None,
        )
        session.add(profile)

    profile.connection_id = connection.id
    _ensure_config(session, profile)
    await session.commit()
    await session.refresh(profile)
    return profile
