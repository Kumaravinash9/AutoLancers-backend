"""Marketplace connection lifecycle: soft delete now, hard purge later.

Disconnecting an account must not run a destructive delete against the live database inline — a bad
click or a bug would take an OAuth credential and its history with it, irreversibly. Instead we soft
delete: mark the row disconnected and **scrub its tokens immediately** (we have no business keeping
an access or refresh token for an account the user has disconnected), but keep the row. A scheduled
purge hard-deletes rows that have been disconnected long enough that no undo is expected.

Reads of "live" connections filter ``disconnected_at IS NULL`` everywhere; a soft-deleted row is
invisible to the app but still on disk until the purge.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlatformConnection, utcnow

logger = logging.getLogger(__name__)

# How long a disconnected row lingers before the purge hard-deletes it. Long enough that a
# reconnect (which reactivates the same row) or a support "undo" is still possible.
PURGE_AFTER = dt.timedelta(days=30)


def disconnect_connection(connection: PlatformConnection) -> None:
    """Soft-delete a connection: mark it disconnected, scrub its tokens, and drop its selection.

    Pure mutation of the row — the caller owns the commit — so a wrong flow can't destroy the
    credential inline, and the token never lingers for an account the user has disconnected.
    """
    connection.disconnected_at = utcnow()
    connection.status = "DISCONNECTED"
    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    # A disconnected account can't be the one the app is scoped to. The partial unique index on
    # is_selected only counts selected rows, so clearing this also frees the slot for another.
    connection.is_selected = False


async def purge_disconnected_connections(
    session: AsyncSession, older_than: dt.timedelta = PURGE_AFTER
) -> int:
    """Hard-delete connections soft-deleted longer ago than ``older_than``. Returns the count.

    This is the only place a connection row is actually removed, and it runs off the request path
    in the scheduler — so the irreversible step is deliberate and batched, never inline with a
    user's click.
    """
    cutoff = utcnow() - older_than
    rows = (
        await session.scalars(
            select(PlatformConnection).where(
                PlatformConnection.disconnected_at.is_not(None),
                PlatformConnection.disconnected_at < cutoff,
            )
        )
    ).all()

    for row in rows:
        await session.delete(row)

    if rows:
        await session.commit()
    return len(rows)
