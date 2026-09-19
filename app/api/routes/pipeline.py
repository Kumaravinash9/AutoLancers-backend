from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.accounts import current_user
from app.db.models import User
from app.db.session import get_session
from app.services.pipeline import prune_stale_jobs, run_cycle
from app.services.users import get_or_create_profile

router = APIRouter(tags=["pipeline"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/pipeline/run")
async def run_once(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Trigger one ingest cycle by hand. Useful for testing without waiting for the poller.

    Runs against the caller's own scoped profile — the poller is what sweeps every account.
    """
    profile = await get_or_create_profile(session, user.id)
    return (await run_cycle(session, profile, trigger="manual")).as_dict()


@router.post("/pipeline/prune")
async def prune(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return {"pruned": await prune_stale_jobs(session)}
