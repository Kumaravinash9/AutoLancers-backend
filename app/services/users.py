"""Single-user helpers.

v1 has exactly one user. These functions are the only place that assumption lives, so adding real
auth later means changing these call sites rather than hunting hardcoded IDs through the codebase.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Profile, User

DEFAULT_USER_EMAIL = "owner@localhost"


async def get_or_create_default_user(session: AsyncSession) -> User:
    user = await session.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    if user is None:
        user = User(email=DEFAULT_USER_EMAIL)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def get_or_create_profile(session: AsyncSession, user_id: int) -> Profile:
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    if profile is None:
        profile = Profile(user_id=user_id)
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
    return profile
