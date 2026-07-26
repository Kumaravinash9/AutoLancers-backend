"""Background polling.

Freelancer publishes no webhook, SSE, or public socket for third parties — the live Project Alerts
panel on their site runs on an internal session-authenticated channel that is not part of the
public API. Short-interval polling of the REST endpoint is the sanctioned mechanism.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.connections import purge_disconnected_connections
from app.services.pipeline import prune_stale_jobs, run_cycle

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _poll() -> None:
    # APScheduler swallows exceptions into its own logger; catching here keeps the failure in our
    # logs and guarantees the job stays scheduled.
    try:
        async with SessionLocal() as session:
            await run_cycle(session)
    except Exception:
        logger.exception("Poll cycle failed")


async def _prune() -> None:
    try:
        async with SessionLocal() as session:
            removed = await prune_stale_jobs(session)
            if removed:
                logger.info("Pruned %d stale jobs", removed)
    except Exception:
        logger.exception("Prune failed")


async def _purge_connections() -> None:
    # The one place a connection row is actually deleted — batched, off the request path, and only
    # for accounts disconnected long enough that no reconnect or undo is expected.
    try:
        async with SessionLocal() as session:
            removed = await purge_disconnected_connections(session)
            if removed:
                logger.info("Purged %d long-disconnected connections", removed)
    except Exception:
        logger.exception("Connection purge failed")


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _poll,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="poll_freelancer",
        max_instances=1,      # never let a slow cycle overlap the next one
        coalesce=True,        # after a pause, run once rather than catching up
        misfire_grace_time=60,
    )
    _scheduler.add_job(_prune, "interval", hours=1, id="prune_stale_jobs", max_instances=1)
    _scheduler.add_job(
        _purge_connections, "interval", hours=24, id="purge_connections", max_instances=1
    )
    _scheduler.start()
    logger.info("Scheduler started (polling every %ds)", settings.poll_interval_seconds)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
