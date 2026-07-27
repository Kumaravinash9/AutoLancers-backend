"""Database models — the multi-user data model from ``docs/target-schema.sql``.

Two decisions from that schema drive everything here:

**Projects are global; recommendations are per-user.** A posting on Freelancer.com is one row in
``projects`` no matter how many people are watching for it. What differs per person is the score,
the reasoning and the status, which live in ``recommendations``. Storing a copy of the posting per
user would mean re-fetching and re-storing the same text a thousand times over.

**Clients are not users.** People who post jobs exist only as columns on ``projects`` — we know
them through a marketplace API, never as accounts here.

UUID primary keys throughout: they don't leak how many users or jobs exist, and they can be minted
without a round trip to the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)


def _fk(target: str, **kw: Any) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete="CASCADE"), **kw)


# --- enumerations -------------------------------------------------------------------


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AccountType(StrEnum):
    SOLO = "SOLO_FREELANCER"
    AGENCY = "AGENCY"


class AuthProvider(StrEnum):
    EMAIL = "EMAIL"
    GOOGLE = "GOOGLE"


class DiscoveryMethod(StrEnum):
    """How a posting reached us.

    Upwork prohibits automated discovery, so anything from there arrives as pasted text. Recording
    which is which keeps that distinction auditable rather than tribal knowledge.
    """

    API_POLL = "API_POLL"
    PASTE_IN = "PASTE_IN"


class RecommendationStatus(StrEnum):
    NEW = "NEW"
    VIEWED = "VIEWED"
    APPLIED = "APPLIED"
    DISMISSED = "DISMISSED"  # the user passed on it


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"  # the client said no — distinct from DISMISSED above
    WITHDRAWN = "WITHDRAWN"


class SubmittedVia(StrEnum):
    API = "API"
    MANUAL_COPY = "MANUAL_COPY"


# --- accounts -----------------------------------------------------------------------


class User(Base):
    """An account on this product — a freelancer using the tool."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Null when the account was created through Google and has never set a password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(20), default=AuthProvider.EMAIL)
    profile_image: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A label only. An agency is still one account with one profile; real multi-seat support
    # would need a memberships table, and pretending otherwise here would be a lie.
    account_type: Mapped[str] = mapped_column(String(20), default=AccountType.SOLO)

    role: Mapped[str] = mapped_column(String(16), default=Role.USER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    profile: Mapped[FreelancerProfile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    connections: Mapped[list[PlatformConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


class PlatformConnection(Base):
    """A user's OAuth link to one marketplace.

    Tokens hold Fernet ciphertext, never plaintext — read and write them through
    ``app.auth.crypto``. Reputation lives here rather than on the profile because the same person
    has different ratings on Freelancer.com and Upwork.
    """

    __tablename__ = "platform_connections"
    # Keyed by the marketplace account, not just the platform: one person can operate several
    # Freelancer accounts, and a per-platform constraint would silently overwrite one with the next.
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform", "platform_user_id", name="uq_connection_user_platform_account"
        ),
        # At most one selected account per user. A partial index enforces that in the database
        # rather than trusting every write path to clear the previous one first.
        Index(
            "uq_selected_connection_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_selected"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id", index=True)

    platform: Mapped[str] = mapped_column(String(50), default="freelancer")
    platform_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platform_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)

    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_reviews: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The avatar as the marketplace serves it. Stored per connection rather than per user because
    # the same person can present differently on Freelancer and Upwork.
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which account the app is scoped to. All false means all accounts — a real state, not an
    # unset one, so it is the default rather than something the user has to choose.
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)

    # The account's own public profile, as the marketplace holds it — distinct from the
    # ``freelancer_profiles`` row, which is our scoring configuration. Two Freelancer accounts run
    # by the same person advertise different skills and rates, and that is what a client sees.
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tagline: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    hourly_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    portfolio_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    member_since: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    connected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    token_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    # Soft delete. Disconnecting an account sets this and scrubs the tokens, but keeps the row so a
    # destructive delete never runs on the live database inline; a background purge removes rows
    # long-disconnected (see ``services.connections``). Every read of "live" connections filters
    # ``disconnected_at IS NULL``; ``None`` means active.
    disconnected_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="connections")


class FreelancerProfile(Base):
    """The matching and drafting profile. One per user.

    Scoring weights are explicit columns rather than keys inside a JSONB blob because they are the
    tuning surface — the profile screen edits them directly, and burying them would make every
    change a blind write.
    """

    __tablename__ = "freelancer_profiles"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id", unique=True, index=True)

    display_name: Mapped[str] = mapped_column(String(255), default="")
    headline: Mapped[str] = mapped_column(String(255), default="")
    bio: Mapped[str] = mapped_column(Text, default="")

    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # LLM-proposed skills the freelancer hasn't confirmed yet (see ``services.skill_suggest``).
    # Kept apart from ``skills`` on purpose: only a confirmed skill feeds matching and scoring, so
    # a suggestion can't inflate a score until the freelancer accepts it. Each entry is
    # ``{name, weight, reason, source}`` — the reason is what the accept/reject UI shows.
    suggested_skills: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    portfolio: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    experience: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    education: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    tone_samples: Mapped[list[str]] = mapped_column(JSONB, default=list)

    rate_min: Mapped[float] = mapped_column(Float, default=0.0)
    rate_max: Mapped[float] = mapped_column(Float, default=0.0)
    fixed_project_min: Mapped[float] = mapped_column(Float, default=0.0)
    # The freelancer's home currency and country, mirrored from their connected account during sync
    # (Freelancer derives currency from the account's country). ``currency`` is the one currency the
    # app reasons in: scoring floors are stated in it, and job budgets are converted into it for an
    # at-a-glance read. Defaults to USD until an account is connected.
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    availability: Mapped[str] = mapped_column(String(30), default="FULL_TIME")

    keywords_include: Mapped[list[str]] = mapped_column(JSONB, default=list)
    keywords_exclude: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # The bid count at which competition scores half marks. Not a cap: a crowded posting loses
    # points, it is never hidden. See ``scoring._score_competition``.
    crowded_at_bids: Mapped[int] = mapped_column(Integer, default=25)
    min_match_score: Mapped[float] = mapped_column(Float, default=55.0)

    weight_skills: Mapped[float] = mapped_column(Float, default=60.0)
    weight_budget: Mapped[float] = mapped_column(Float, default=20.0)
    weight_competition: Mapped[float] = mapped_column(Float, default=10.0)
    weight_recency: Mapped[float] = mapped_column(Float, default=10.0)

    # Resolved Freelancer skill IDs for the server-side discovery filter — the confirmed skills
    # broadened into the marketplace's tag vocabulary (see ``services.skill_expansion``). Cached
    # because computing it hits the LLM and the skill catalogue; ``search_skill_ids_key`` hashes
    # the inputs so it recomputes only when the confirmed skills change, not every cycle. Distinct
    # from ``skills``: this widens *what is fetched*, while ``skills`` decides *how good* each fetch
    # is — so a broad discovery tag never inflates a match score.
    search_skill_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    search_skill_ids_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    proposal_notes: Mapped[str] = mapped_column(Text, default="")

    # Tokenised reference only. Never raw card or bank details.
    payment_provider_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # When this freelancer's board was last recalculated — distinct from a connection's
    # last_synced_at, which tracks the last pull from that platform's API.
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When their real bids were last pulled back from the marketplace. Null means no outcome has
    # ever been checked, which is why the UI must not report zero wins as a result.
    bids_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # High-watermark for incremental discovery: the submit-time of the newest posting ingested so
    # far. Each cycle fetches only projects posted after this, so a busy board can't push new
    # postings past a single page of "newest" (see ``services.pipeline``). Null means never pulled
    # — the first cycle bootstraps from the current newest page instead of backfilling everything.
    last_project_submitted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="profile")


# --- marketplace data ---------------------------------------------------------------


class Project(Base):
    """A posting ingested from a marketplace. Global — not owned by any user."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_project_platform_external"),
    )

    id: Mapped[uuid.UUID] = _pk()

    platform: Mapped[str] = mapped_column(String(50), default="freelancer", index=True)
    # Null for pasted text with no id of its own.
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    discovery_method: Mapped[str] = mapped_column(String(20), default=DiscoveryMethod.API_POLL)

    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    client_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_reviews_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    required_skills: Mapped[list[str]] = mapped_column(JSONB, default=list)

    work_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # FIXED | HOURLY
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    min_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_budget: Mapped[float | None] = mapped_column(Float, nullable=True)

    # bid_count and friends as reported at fetch time.
    bid_information: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    project_url: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bid_deadline: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)

    # Fingerprint of the fields worth reacting to. Comparing it is how a re-fetch tells a genuine
    # edit from the same posting seen again — `updated_at` alone only says when we last looked.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def bid_count(self) -> int | None:
        value = (self.bid_information or {}).get("bid_count")
        return int(value) if isinstance(value, int | float) else None


class Recommendation(Base):
    """One user's verdict on one project: the score, the reasoning, and what they did about it."""

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint(
            "freelancer_id", "project_id", name="uq_recommendation_freelancer_project"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    freelancer_id: Mapped[uuid.UUID] = _fk("freelancer_profiles.id", index=True)
    project_id: Mapped[uuid.UUID] = _fk("projects.id", index=True)

    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # Per-component trace: [{"label": ..., "detail": ..., "points": ...}, ...]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    # Cached semantic skill match (see ``services.matching``). The LLM call is only worth making on
    # new or changed postings, so its 0-1 fit and one-line reason are stored here and reused on
    # unchanged sightings. ``skill_match_key`` hashes the inputs (profile skills + job content) so
    # the cache invalidates itself when either moves; ``None`` means it was never computed (no key,
    # feature off, or the call failed) and scoring falls back to substring matching.
    skill_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    skill_match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_match_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # A hard reject is the tool's decision; DISMISSED below is the user's. Keeping them apart is
    # what lets the rejected view explain itself.
    is_hard_rejected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(30), default=RecommendationStatus.NEW, index=True)

    # Set when the underlying posting changed after this was last reviewed, so a board can show
    # "budget raised" or "now 40 bids" rather than looking identical to yesterday.
    has_changes: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    recommended_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(lazy="joined")
    proposal: Mapped[Proposal | None] = relationship(
        back_populates="recommendation", uselist=False, lazy="joined"
    )


class Proposal(Base):
    """A drafted or submitted bid."""

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _fk("projects.id", index=True)
    freelancer_id: Mapped[uuid.UUID] = _fk("freelancer_profiles.id", index=True)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Which marketplace account placed it. Nullable because a draft has no account yet, and
    # SET NULL on delete so disconnecting an account doesn't take its bid history with it.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    proposal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    bid_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    milestones: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    submitted_via: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default=ProposalStatus.DRAFT, index=True)

    # The marketplace's own id for the bid. Its presence is what makes a duplicate submission
    # detectable rather than merely unlikely.
    external_bid_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # What the draft cost, kept per proposal so spend is attributable.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drafted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    submitted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    recommendation: Mapped[Recommendation | None] = relationship(back_populates="proposal")


class ProposalAIVersion(Base):
    """Every generated draft, kept because ``proposal_text`` is overwritten by hand-editing.

    Comparing what the model wrote against what was actually sent is the only honest read on
    whether drafts are improving.
    """

    __tablename__ = "proposal_ai_versions"

    id: Mapped[uuid.UUID] = _pk()
    proposal_id: Mapped[uuid.UUID] = _fk("proposals.id", index=True)

    version: Mapped[int] = mapped_column(Integer, default=1)
    generated_text: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecommendationFeedback(Base):
    """Signal for improving match quality."""

    __tablename__ = "recommendation_feedback"

    id: Mapped[uuid.UUID] = _pk()
    recommendation_id: Mapped[uuid.UUID] = _fk("recommendations.id", index=True)
    feedback_type: Mapped[str] = mapped_column(String(30))  # LIKE | DISLIKE | APPLIED
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- normalised skill tags ----------------------------------------------------------


class Skill(Base):
    """Canonical skill tag. The JSONB profile field stays editable; this is its queryable index."""

    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # The marketplace's own id, so a resolved skill needn't be resolved again every cycle.
    external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class FreelancerSkill(Base):
    __tablename__ = "freelancer_skills"

    freelancer_id: Mapped[uuid.UUID] = _fk("freelancer_profiles.id", primary_key=True)
    skill_id: Mapped[uuid.UUID] = _fk("skills.id", primary_key=True, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    tier: Mapped[str] = mapped_column(String(20), default="PRIMARY")


class ProjectSkill(Base):
    __tablename__ = "project_skills"

    project_id: Mapped[uuid.UUID] = _fk("projects.id", primary_key=True)
    skill_id: Mapped[uuid.UUID] = _fk("skills.id", primary_key=True, index=True)


# --- preferences and billing --------------------------------------------------------


class NotificationPreference(Base):
    """Per-user channel opt-ins. Email is the always-on baseline; the rest are opt-in."""

    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id", unique=True, index=True)

    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    slack_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    slack_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    teams_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    teams_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A channel toggle alone would wake someone for a 56-point match.
    min_score_to_notify: Mapped[float] = mapped_column(Float, default=70.0)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Subscription(Base):
    """Billing state. Provider identifiers only — never card details."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id", unique=True, index=True)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    plan: Mapped[str] = mapped_column(String(50), default="free")
    status: Mapped[str] = mapped_column(String(30), default="trialing")

    # Enforced before drafting rather than discovered on a bill.
    monthly_budget_limit: Mapped[float | None] = mapped_column(Float, nullable=True)

    current_period_start: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --- operations ---------------------------------------------------------------------


class CycleRun(Base):
    """One completed poll cycle.

    Not in the target schema, kept because without it the poller is unobservable: "nothing matched
    today" and "discovery has been failing for six hours" look identical from the board.
    """

    __tablename__ = "cycle_runs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

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
    trigger: Mapped[str] = mapped_column(String(16), default="poll")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DemoRequest(Base):
    """Someone asking for a walkthrough from the marketing page.

    Deliberately not a ``User``: they have no account and may never make one. Storing the ask
    means a request that arrives while nobody is watching is still there tomorrow — a mailto link
    to an unmonitored address loses it silently.
    """

    __tablename__ = "demo_requests"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), index=True)
    # What they work on and what they want to see. Free text, because a dropdown here would guess
    # at categories we have no evidence for yet.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    marketplace: Mapped[str | None] = mapped_column(String(50), nullable=True)

    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiToken(Base):
    """A long-lived credential for a client that has no browser session — the extension.

    Only the hash is stored. A token is shown once at creation and is unrecoverable afterwards,
    which is the whole point: a leaked database gives an attacker nothing to replay.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id", index=True)

    # What it's for, so revoking the right one doesn't take guesswork.
    name: Mapped[str] = mapped_column(String(100), default="Chrome extension")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Revoked rather than deleted: "this token was used, then revoked" is worth being able to see.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
