"""Database models.

v1 runs single-user, but every table carries ``user_id`` from the first migration so the
multi-tenant retrofit never has to move rows or re-key credentials.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    """Roles are a closed set, checked server-side on every request.

    Kept deliberately coarse: an `admin` operates the platform, a `user` operates their own
    account. Anything finer would be invented complexity until there are real teams.
    """

    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # bcrypt digest. Nullable so the pre-auth single-user row stays valid until it gets a password.
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.USER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    tokens: Mapped[list[OAuthToken]] = relationship(back_populates="user")
    profile: Mapped[Profile | None] = relationship(back_populates="user", uselist=False)


class OAuthToken(Base):
    """Per-user, per-platform credentials.

    ``access_token`` and ``refresh_token`` hold Fernet ciphertext, never plaintext — read and
    write them through ``app.auth.crypto`` rather than touching the columns directly.
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("user_id", "platform", name="uq_token_user_platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), default="freelancer")

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="tokens")


class Profile(Base):
    """Scoring configuration.

    Everything here is tuning surface — nothing downstream hardcodes a weight or threshold.
    """

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )

    display_name: Mapped[str] = mapped_column(String(120), default="")
    headline: Mapped[str] = mapped_column(String(255), default="")

    # [{"name": "next.js", "weight": 5}, ...]
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    keywords_include: Mapped[list[str]] = mapped_column(JSONB, default=list)
    keywords_exclude: Mapped[list[str]] = mapped_column(JSONB, default=list)

    fixed_project_min: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_min: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    max_existing_bids: Mapped[int] = mapped_column(Integer, default=25)
    min_match_score: Mapped[float] = mapped_column(Float, default=55.0)

    # Component weights, summed and normalised at scoring time.
    weight_skills: Mapped[float] = mapped_column(Float, default=60.0)
    weight_budget: Mapped[float] = mapped_column(Float, default=20.0)
    weight_competition: Mapped[float] = mapped_column(Float, default=10.0)
    weight_recency: Mapped[float] = mapped_column(Float, default=10.0)

    # Free text woven into the "about us" beat of the proposal.
    proposal_notes: Mapped[str] = mapped_column(Text, default="")

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="profile")


class CycleRun(Base):
    """One completed poll cycle.

    Without this the poller is unobservable: you can't tell "no good jobs today" apart from
    "discovery has been failing for six hours", and both look identical from the queue.
    """

    __tablename__ = "cycle_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    drafted: Mapped[int] = mapped_column(Integer, default=0)
    draft_failures: Mapped[int] = mapped_column(Integer, default=0)

    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger: Mapped[str] = mapped_column(String(16), default="poll")  # poll | manual
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base):
    """A normalised posting plus everything we derived from it."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", "external_id", name="uq_job_user_platform_ext"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    platform: Mapped[str] = mapped_column(String(32), default="freelancer")
    external_id: Mapped[str] = mapped_column(String(64))

    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    skills_listed: Mapped[list[str]] = mapped_column(JSONB, default=list)

    budget_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # fixed | hourly
    budget_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    bid_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    score: Mapped[float] = mapped_column(Float, default=0.0)
    # Human-readable scoring trace: [{"label": ..., "detail": ..., "points": ...}, ...]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_drafted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # new -> drafted -> approved | dismissed | submitted.
    # "submitted" is only ever reached through an explicit per-job confirmation.
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)

    # Set only when a bid was actually placed through the API. `external_bid_id` is Freelancer's
    # id for it, which is what makes a duplicate submission detectable rather than merely unlikely.
    bid_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid_submitted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_bid_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refreshed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
