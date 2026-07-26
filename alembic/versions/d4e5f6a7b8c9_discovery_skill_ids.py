"""cached discovery skill ids on freelancer profiles

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-27 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'freelancer_profiles',
        sa.Column(
            'search_skill_ids',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='[]',
        ),
    )
    op.add_column(
        'freelancer_profiles',
        sa.Column('search_skill_ids_key', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('freelancer_profiles', 'search_skill_ids_key')
    op.drop_column('freelancer_profiles', 'search_skill_ids')
