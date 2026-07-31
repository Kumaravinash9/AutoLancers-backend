"""User and profile helpers.

A user has one :class:`FreelancerProfile` per connected marketplace account (plus, before any
account is connected, one connection-less default). Recommendations and proposals hang off the
profile, not the user, so resolving the right profile from whatever a caller holds — a user, or a
specific connection — happens here so no caller has to know the rule.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import ConnectorKind
from app.db.models import (
    FreelancerProfile,
    PlatformConnection,
    ProfileConfig,
    Role,
    User,
)

logger = logging.getLogger(__name__)

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


async def get_or_create_extension_connection(
    session: AsyncSession,
    user_id: uuid.UUID,
    platform: str,
    account_id: str,
    username: str | None = None,
) -> PlatformConnection:
    """The connection for one extension-captured account, keyed on its stable id.

    Keyed on ``platform_user_id`` (the account's real id), not the handle: a rename can't spawn a
    duplicate, and this is the same field OAuth stores — so an account connected both ways is one
    row. Creates a token-less :data:`ConnectorKind.EXTENSION` row when the account is new.
    """
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
            PlatformConnection.platform_user_id == account_id,
        )
    )
    if connection is None:
        connection = PlatformConnection(
            user_id=user_id,
            platform=platform,
            platform_user_id=account_id,
            platform_username=username,
            status="ACTIVE",
            kind=ConnectorKind.EXTENSION,
            scope=None,
        )
        session.add(connection)
        await session.flush()  # needs an id before a profile can link to it
    elif username:
        # Keep the display handle current — it's the mutable field; the id is what we matched on.
        connection.platform_username = username
    return connection


async def get_or_create_profile_for_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    platform: str,
    account_id: str | None,
) -> FreelancerProfile:
    """The profile a capture should attribute to: the account it came from, else the selected one.

    A capture that names its account (``account_id``) scores against *that* account's profile, even
    when several are connected. Without one — an older extension — it falls back to the selected
    profile, so nothing breaks; the fallback is logged because a silent misattribution is the bug
    this exists to prevent.
    """
    if not account_id:
        logger.info(
            "Capture on %s carried no account id — attributing to the selected profile.", platform
        )
        return await get_or_create_profile(session, user_id)

    connection = await get_or_create_extension_connection(session, user_id, platform, account_id)
    return await get_or_create_profile_for_connection(session, connection)
