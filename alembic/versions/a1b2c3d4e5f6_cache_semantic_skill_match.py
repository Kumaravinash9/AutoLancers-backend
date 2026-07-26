"""cache semantic skill match on recommendations

Revision ID: a1b2c3d4e5f6
Revises: 7b8250614176
Create Date: 2026-07-27 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '7b8250614176'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('recommendations', sa.Column('skill_match_score', sa.Float(), nullable=True))
    op.add_column('recommendations', sa.Column('skill_match_reason', sa.Text(), nullable=True))
    op.add_column('recommendations', sa.Column('skill_match_key', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('recommendations', 'skill_match_key')
    op.drop_column('recommendations', 'skill_match_reason')
    op.drop_column('recommendations', 'skill_match_score')
