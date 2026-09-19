"""Make the profile the hub: one per connection, marketplace mirror moves off the connection,
scoring tuning split into profile_configs.

Revision ID: b1c2d3e4f5a6
Revises: f068744b26ed
Create Date: 2026-07-30 00:00:00.000000

Data is preserved, not dropped. Before any column is removed:

* every existing profile gets a ``profile_configs`` row carrying its weights, thresholds and
  keyword filters;
* each user's primary connection (selected first, else oldest live) adopts that user's existing
  profile — its marketplace mirror is copied onto the profile;
* every *additional* connection gets a fresh profile carrying its own mirror and default scoring;
* ``is_selected`` moves from the connection to the profile.

Only then are the moved/renamed columns dropped from ``platform_connections`` and
``freelancer_profiles``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "f068744b26ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The marketplace-mirror columns moving from platform_connections onto freelancer_profiles.
_MIRROR = [
    ("avatar_url", sa.Text()),
    ("tagline", sa.Text()),
    ("summary", sa.Text()),
    ("hourly_rate", sa.Float()),
    ("rating", sa.Float()),
    ("total_reviews", sa.Integer()),
    ("portfolio_count", sa.Integer()),
    ("member_since", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. profile_configs table (scoring tuning surface) ---
    op.create_table(
        "profile_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("freelancer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weight_skills", sa.Float(), nullable=False, server_default="60"),
        sa.Column("weight_budget", sa.Float(), nullable=False, server_default="20"),
        sa.Column("weight_competition", sa.Float(), nullable=False, server_default="10"),
        sa.Column("weight_recency", sa.Float(), nullable=False, server_default="10"),
        sa.Column("crowded_at_bids", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("min_match_score", sa.Float(), nullable=False, server_default="55"),
        sa.Column(
            "keywords_include", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "keywords_exclude", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_profile_configs_profile_id", "profile_configs", ["profile_id"], unique=True
    )

    # --- 2. new columns on freelancer_profiles (nullable/defaulted for the data migration) ---
    op.add_column(
        "freelancer_profiles",
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "freelancer_profiles",
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "freelancer_profiles",
        sa.Column("account_skills", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    for name, type_ in _MIRROR:
        op.add_column("freelancer_profiles", sa.Column(name, type_, nullable=True))

    # --- 3. seed profile_configs from the tuning columns still on freelancer_profiles ---
    bind.execute(
        sa.text(
            """
            INSERT INTO profile_configs
                (id, profile_id, weight_skills, weight_budget, weight_competition,
                 weight_recency, crowded_at_bids, min_match_score,
                 keywords_include, keywords_exclude, created_at, updated_at)
            SELECT gen_random_uuid(), id, weight_skills, weight_budget, weight_competition,
                   weight_recency, crowded_at_bids, min_match_score,
                   COALESCE(keywords_include, '[]'::jsonb), COALESCE(keywords_exclude, '[]'::jsonb),
                   now(), now()
            FROM freelancer_profiles
            """
        )
    )

    # --- 4. primary connection adopts the user's existing profile; copy its mirror across ---
    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT pc.*, ROW_NUMBER() OVER (
                    PARTITION BY pc.user_id
                    ORDER BY pc.is_selected DESC, pc.connected_at ASC
                ) AS rn
                FROM platform_connections pc
                WHERE pc.disconnected_at IS NULL
            ),
            primary_conn AS (SELECT * FROM ranked WHERE rn = 1)
            UPDATE freelancer_profiles fp
            SET connection_id  = pc.id,
                is_selected    = COALESCE(pc.is_selected, false),
                display_name   = COALESCE(NULLIF(fp.display_name, ''), pc.display_name, fp.display_name),
                avatar_url     = pc.avatar_url,
                tagline        = pc.tagline,
                summary        = pc.summary,
                account_skills = COALESCE(pc.account_skills, '[]'::jsonb),
                hourly_rate    = pc.hourly_rate,
                rating         = pc.rating,
                total_reviews  = pc.total_reviews,
                portfolio_count= pc.portfolio_count,
                member_since   = pc.member_since,
                country        = COALESCE(fp.country, pc.country),
                currency       = CASE
                    WHEN fp.rate_min = 0 AND fp.fixed_project_min = 0 AND pc.currency IS NOT NULL
                    THEN pc.currency ELSE fp.currency END,
                last_synced_at = COALESCE(pc.last_synced_at, fp.last_synced_at)
            FROM primary_conn pc
            WHERE fp.user_id = pc.user_id
            """
        )
    )

    # Drop the tuning columns now (configs are seeded, the primary adopt is done) so the insert of
    # extra profiles below doesn't have to satisfy their NOT NULL constraints.
    for col in (
        "weight_skills",
        "weight_budget",
        "weight_competition",
        "weight_recency",
        "crowded_at_bids",
        "min_match_score",
        "keywords_include",
        "keywords_exclude",
        "payment_provider_customer_id",
    ):
        op.drop_column("freelancer_profiles", col)

    # user_id was UNIQUE (one profile per user); make it a plain index BEFORE inserting the extra
    # per-connection profiles, or the second profile for a user trips the old unique index.
    op.drop_index("ix_freelancer_profiles_user_id", table_name="freelancer_profiles")
    op.create_index("ix_freelancer_profiles_user_id", "freelancer_profiles", ["user_id"])

    # --- 5. a fresh profile for every additional connection, carrying its own mirror ---
    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT pc.*, ROW_NUMBER() OVER (
                    PARTITION BY pc.user_id
                    ORDER BY pc.is_selected DESC, pc.connected_at ASC
                ) AS rn
                FROM platform_connections pc
                WHERE pc.disconnected_at IS NULL
            ),
            extra AS (SELECT * FROM ranked WHERE rn > 1)
            INSERT INTO freelancer_profiles
                (id, user_id, connection_id, is_selected, display_name, avatar_url, tagline,
                 summary, account_skills, hourly_rate, rating, total_reviews, portfolio_count,
                 member_since, headline, bio, skills, suggested_skills, portfolio, experience,
                 education, tone_samples, rate_min, rate_max, fixed_project_min, currency, country,
                 availability, search_skill_ids, search_skill_ids_key, proposal_notes,
                 last_synced_at, status, created_at, updated_at)
            SELECT gen_random_uuid(), e.user_id, e.id, false,
                   COALESCE(e.display_name, ''), e.avatar_url, e.tagline,
                   e.summary, COALESCE(e.account_skills, '[]'::jsonb), e.hourly_rate, e.rating,
                   e.total_reviews, e.portfolio_count, e.member_since, '', '', '[]'::jsonb,
                   '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 0, 0, 0,
                   COALESCE(e.currency, 'USD'), e.country, 'FULL_TIME', '[]'::jsonb, NULL, '',
                   e.last_synced_at, 'ACTIVE', now(), now()
            FROM extra e
            """
        )
    )
    # profile_configs for the profiles just created (defaults)
    bind.execute(
        sa.text(
            """
            INSERT INTO profile_configs (id, profile_id, created_at, updated_at)
            SELECT gen_random_uuid(), fp.id, now(), now()
            FROM freelancer_profiles fp
            WHERE fp.id NOT IN (SELECT profile_id FROM profile_configs)
            """
        )
    )

    # --- 6. guarantee each user has exactly one selected profile ---
    bind.execute(
        sa.text(
            """
            UPDATE freelancer_profiles
            SET is_selected = true
            WHERE id IN (
                SELECT DISTINCT ON (user_id) id
                FROM freelancer_profiles
                WHERE user_id NOT IN (
                    SELECT user_id FROM freelancer_profiles WHERE is_selected
                )
                ORDER BY user_id, created_at ASC
            )
            """
        )
    )

    # --- 7. constraints and indexes on freelancer_profiles ---
    op.create_foreign_key(
        "freelancer_profiles_connection_id_fkey",
        "freelancer_profiles",
        "platform_connections",
        ["connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_freelancer_profiles_connection_id",
        "freelancer_profiles",
        ["connection_id"],
        unique=True,
    )
    op.create_index(
        "uq_selected_profile_per_user",
        "freelancer_profiles",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_selected"),
    )
    op.alter_column("freelancer_profiles", "is_selected", server_default=None)
    op.alter_column("freelancer_profiles", "account_skills", server_default=None)

    # (the tuning columns were dropped earlier, before the extra-profile insert)

    # --- 8. slim platform_connections: drop selection + the whole marketplace mirror ---
    op.drop_index("uq_selected_connection_per_user", table_name="platform_connections")
    for col in (
        "is_selected",
        "rating",
        "total_reviews",
        "avatar_url",
        "display_name",
        "tagline",
        "summary",
        "account_skills",
        "hourly_rate",
        "currency",
        "country",
        "portfolio_count",
        "member_since",
        "last_synced_at",
    ):
        op.drop_column("platform_connections", col)


def downgrade() -> None:
    """Best-effort reverse: restores the old shape and copies data back from each user's selected
    profile. It cannot perfectly reconstruct multiple profiles into one row, so extra per-connection
    profiles are dropped after their mirror is written back onto their connection."""
    bind = op.get_bind()

    # --- restore platform_connections columns ---
    op.add_column("platform_connections", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("platform_connections", sa.Column("member_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("platform_connections", sa.Column("portfolio_count", sa.Integer(), nullable=True))
    op.add_column("platform_connections", sa.Column("country", sa.String(length=100), nullable=True))
    op.add_column("platform_connections", sa.Column("currency", sa.String(length=10), nullable=True))
    op.add_column("platform_connections", sa.Column("hourly_rate", sa.Float(), nullable=True))
    op.add_column("platform_connections", sa.Column("account_skills", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("platform_connections", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("platform_connections", sa.Column("tagline", sa.Text(), nullable=True))
    op.add_column("platform_connections", sa.Column("display_name", sa.String(length=255), nullable=True))
    op.add_column("platform_connections", sa.Column("avatar_url", sa.Text(), nullable=True))
    op.add_column("platform_connections", sa.Column("total_reviews", sa.Integer(), nullable=True))
    op.add_column("platform_connections", sa.Column("rating", sa.Float(), nullable=True))
    op.add_column("platform_connections", sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()))

    # copy the mirror back onto each connection from its profile
    bind.execute(
        sa.text(
            """
            UPDATE platform_connections pc
            SET is_selected   = fp.is_selected,
                rating        = fp.rating,
                total_reviews = fp.total_reviews,
                avatar_url    = fp.avatar_url,
                display_name  = fp.display_name,
                tagline       = fp.tagline,
                summary       = fp.summary,
                account_skills= COALESCE(fp.account_skills, '[]'::jsonb),
                hourly_rate   = fp.hourly_rate,
                currency      = fp.currency,
                country       = fp.country,
                portfolio_count = fp.portfolio_count,
                member_since  = fp.member_since,
                last_synced_at= fp.last_synced_at
            FROM freelancer_profiles fp
            WHERE fp.connection_id = pc.id
            """
        )
    )
    op.create_index(
        "uq_selected_connection_per_user",
        "platform_connections",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_selected"),
    )
    op.alter_column("platform_connections", "is_selected", server_default=None)

    # --- restore the tuning columns on freelancer_profiles and copy config back ---
    op.add_column("freelancer_profiles", sa.Column("payment_provider_customer_id", sa.String(length=255), nullable=True))
    op.add_column("freelancer_profiles", sa.Column("keywords_exclude", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("freelancer_profiles", sa.Column("keywords_include", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("freelancer_profiles", sa.Column("min_match_score", sa.Float(), nullable=False, server_default="55"))
    op.add_column("freelancer_profiles", sa.Column("crowded_at_bids", sa.Integer(), nullable=False, server_default="25"))
    op.add_column("freelancer_profiles", sa.Column("weight_recency", sa.Float(), nullable=False, server_default="10"))
    op.add_column("freelancer_profiles", sa.Column("weight_competition", sa.Float(), nullable=False, server_default="10"))
    op.add_column("freelancer_profiles", sa.Column("weight_budget", sa.Float(), nullable=False, server_default="20"))
    op.add_column("freelancer_profiles", sa.Column("weight_skills", sa.Float(), nullable=False, server_default="60"))
    bind.execute(
        sa.text(
            """
            UPDATE freelancer_profiles fp
            SET weight_skills = c.weight_skills, weight_budget = c.weight_budget,
                weight_competition = c.weight_competition, weight_recency = c.weight_recency,
                crowded_at_bids = c.crowded_at_bids, min_match_score = c.min_match_score,
                keywords_include = c.keywords_include, keywords_exclude = c.keywords_exclude
            FROM profile_configs c
            WHERE c.profile_id = fp.id
            """
        )
    )

    # drop the extra per-connection profiles (keep each user's selected one)
    bind.execute(
        sa.text(
            """
            DELETE FROM freelancer_profiles
            WHERE id NOT IN (
                SELECT DISTINCT ON (user_id) id
                FROM freelancer_profiles
                ORDER BY user_id, is_selected DESC, created_at ASC
            )
            """
        )
    )

    op.drop_index("uq_selected_profile_per_user", table_name="freelancer_profiles")
    op.drop_index("ix_freelancer_profiles_connection_id", table_name="freelancer_profiles")
    op.drop_constraint(
        "freelancer_profiles_connection_id_fkey", "freelancer_profiles", type_="foreignkey"
    )
    op.drop_index("ix_freelancer_profiles_user_id", table_name="freelancer_profiles")
    op.create_index(
        "ix_freelancer_profiles_user_id", "freelancer_profiles", ["user_id"], unique=True
    )
    for name, _ in _MIRROR:
        op.drop_column("freelancer_profiles", name)
    op.drop_column("freelancer_profiles", "account_skills")
    op.drop_column("freelancer_profiles", "is_selected")
    op.drop_column("freelancer_profiles", "connection_id")

    op.drop_index("ix_profile_configs_profile_id", table_name="profile_configs")
    op.drop_table("profile_configs")

    for col in ("weight_skills", "weight_budget", "weight_competition", "weight_recency",
                "crowded_at_bids", "min_match_score", "keywords_include", "keywords_exclude"):
        op.alter_column("freelancer_profiles", col, server_default=None)
    op.alter_column("platform_connections", "account_skills", server_default=None)
