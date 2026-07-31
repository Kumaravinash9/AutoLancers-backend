"""Keep the pages the extension reads that have no modelled home yet.

Contracts, proposals, orders and message-room lists arrive as loose rows with no assumed columns.
Modelling them means decisions not yet made — a proposal needs a resolved project FK, a contract needs
a table of its own — but the data only exists while someone is looking at the page. So it accumulates
verbatim in ``page_captures`` and v2 extracts whatever shape it settles on, over history rather than
from scratch.

Unique on (user_id, platform, page_key, content_hash): re-collecting an unchanged page bumps
``times_seen`` rather than storing another copy, while a changed page is a new row — that difference
is what makes the accumulation worth reading later.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "page_captures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Owned by a user, unlike projects: a posting is public and shared, your contracts and your
        # inbox are not. CASCADE so deleting an account takes its captures with it.
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("page_key", sa.String(length=100), nullable=False),
        sa.Column("page_label", sa.String(length=200), nullable=True),
        sa.Column("reads", sa.String(length=20), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column(
            "items", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_text", sa.Text(), nullable=True),
        # Null means nobody has paid to interpret this capture yet — not that it cannot be.
        sa.Column("parsed", postgresql.JSONB(), nullable=True),
        sa.Column("parsed_model", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "platform", "page_key", "content_hash", name="uq_capture_user_page_content"
        ),
    )
    op.create_index("ix_page_captures_user_id", "page_captures", ["user_id"])
    op.create_index("ix_page_captures_platform", "page_captures", ["platform"])
    op.create_index("ix_page_captures_page_key", "page_captures", ["page_key"])
    op.create_index("ix_page_captures_content_hash", "page_captures", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_page_captures_content_hash", table_name="page_captures")
    op.drop_index("ix_page_captures_page_key", table_name="page_captures")
    op.drop_index("ix_page_captures_platform", table_name="page_captures")
    op.drop_index("ix_page_captures_user_id", table_name="page_captures")
    op.drop_table("page_captures")
