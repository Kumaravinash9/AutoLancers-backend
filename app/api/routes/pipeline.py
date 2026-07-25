from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.pipeline import prune_stale_jobs, run_cycle

router = APIRouter(tags=["pipeline"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/pipeline/run")
async def run_once(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """Trigger one ingest cycle by hand. Useful for testing without waiting for the poller."""
    return (await run_cycle(session)).as_dict()


@router.post("/pipeline/prune")
async def prune(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return {"pruned": await prune_stale_jobs(session)}
