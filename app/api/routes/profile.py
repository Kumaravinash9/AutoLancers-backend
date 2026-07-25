from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ProfileIn, ProfileOut
from app.db.models import Profile
from app.db.session import get_session
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=ProfileOut)
async def read_profile(session: AsyncSession = Depends(get_session)) -> Profile:
    user = await get_or_create_default_user(session)
    return await get_or_create_profile(session, user.id)


@router.put("", response_model=ProfileOut)
async def update_profile(
    payload: ProfileIn, session: AsyncSession = Depends(get_session)
) -> Profile:
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
    return profile
