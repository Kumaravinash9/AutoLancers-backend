"""Database models.

v1 runs single-user, but every table carries ``user_id`` from the first migration so the
multi-tenant retrofit never has to move rows or re-key credentials.
"""

from __future__ import annotations

import datetime as dt
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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

    # new -> drafted -> approved | dismissed. No "submitted": v1 never submits.
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refreshed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
