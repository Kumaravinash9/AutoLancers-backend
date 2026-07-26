from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ProfileIn, ProfileOut
from app.db.models import FreelancerProfile, utcnow
from app.db.session import get_session
from app.services.users import get_or_create_default_user, get_or_create_profile

router = APIRouter(prefix="/profile", tags=["profile"])

# Freelancer's terms expect cached marketplace data to be refreshed at least daily, so a board
# older than that is stale by their definition as well as ours.
STALE_AFTER = dt.timedelta(hours=24)


def _out(profile: FreelancerProfile) -> ProfileOut:
    synced = profile.last_synced_at
    if synced is not None and synced.tzinfo is None:
        synced = synced.replace(tzinfo=dt.UTC)
    stale = synced is None or (utcnow() - synced) > STALE_AFTER
    return ProfileOut.model_validate(profile, from_attributes=True).model_copy(
        update={"sync_is_stale": stale}
    )


@router.get("", response_model=ProfileOut)
async def read_profile(session: AsyncSession = Depends(get_session)) -> ProfileOut:
    user = await get_or_create_default_user(session)
    return _out(await get_or_create_profile(session, user.id))


@router.put("", response_model=ProfileOut)
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
