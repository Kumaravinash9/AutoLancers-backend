"""User and profile helpers.

Every user has exactly one ``FreelancerProfile``, and it is the profile — not the user — that
recommendations and proposals hang off. Resolving one from the other happens here so no caller
has to know that.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FreelancerProfile, Role, User

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


async def get_or_create_profile(session: AsyncSession, user_id: uuid.UUID) -> FreelancerProfile:
    profile = await session.scalar(
        select(FreelancerProfile).where(FreelancerProfile.user_id == user_id)
    )
    if profile is None:
        profile = FreelancerProfile(user_id=user_id)
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
    return profile
