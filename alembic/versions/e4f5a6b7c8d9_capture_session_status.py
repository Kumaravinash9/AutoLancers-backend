"""Whether the extension can currently read each marketplace, per user.

Signed out of Upwork, every find-work URL redirects to a login page that loads perfectly — the reader
finds no job links on it and reports zero. Nothing errors, and the board silently stops being
refreshed while continuing to look authoritative. The extension now recognises that wall; this is
where it lands so the app can tell someone.

Latest state per platform rather than a log: a banner needs one answer, and an OK capture clears the
problem instead of appending to it.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capture_statuses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("page_key", sa.String(length=100), nullable=True),
        # Null while OK. Held across repeated failures, so "signed out for three days" is sayable.
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        # Null means never successfully read — which the UI must not call "expired".
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "platform", name="uq_capture_status_user_platform"),
    )
    op.create_index("ix_capture_statuses_user_id", "capture_statuses", ["user_id"])
    op.create_index("ix_capture_statuses_platform", "capture_statuses", ["platform"])
    op.create_index("ix_capture_statuses_status", "capture_statuses", ["status"])


def downgrade() -> None:
    op.drop_index("ix_capture_statuses_status", table_name="capture_statuses")
    op.drop_index("ix_capture_statuses_platform", table_name="capture_statuses")
    op.drop_index("ix_capture_statuses_user_id", table_name="capture_statuses")
    op.drop_table("capture_statuses")
