"""Record how each connection is established: OAuth vs browser-extension (ConnectorKind).

Adds ``platform_connections.kind``. Backfills by platform — Freelancer accounts are OAuth, anything
else (today only extension-captured Upwork) is EXTENSION — matching the connector factory's registry.
Stored as a string, like every other enum in this schema (Role, DiscoveryMethod, …); no native PG
enum type, which keeps adding a value a code change rather than a migration.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default carries the backfill for existing rows; the ORM sets it going forward, so the
    # default is dropped afterwards to match the model (which has an app-level default, not a DB one).
    op.add_column(
        "platform_connections",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="OAUTH"),
    )
    op.execute(
        "UPDATE platform_connections SET kind = 'EXTENSION' WHERE platform <> 'freelancer'"
    )
    op.alter_column("platform_connections", "kind", server_default=None)


def downgrade() -> None:
    op.drop_column("platform_connections", "kind")
