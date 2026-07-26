"""Wire shapes.

``JobOut`` deliberately keeps the flat shape the frontend already consumes, even though the data
now comes from three tables. Splitting projects from recommendations was a storage decision; it
shouldn't force every client to relearn the API.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobOut(BaseModel):
    """A recommendation, flattened with its project and proposal."""

    id: uuid.UUID
    platform: str
    external_id: str | None
    title: str
    description: str
    url: str
    skills_listed: list[str]
    budget_type: str | None
    budget_min: float | None
    budget_max: float | None
    currency: str | None
    bid_count: int | None
    posted_at: dt.datetime | None
    score: float
    reasons: list[dict[str, Any]]
    rejected: bool
    rejection_reason: str | None
    proposal_text: str | None
    status: str
    first_seen_at: dt.datetime
    bid_amount: float | None
    bid_period_days: int | None
    bid_submitted_at: dt.datetime | None
    external_bid_id: str | None
    has_changes: bool
    changed_at: dt.datetime | None


class JobPatch(BaseModel):
    proposal_text: str | None = None
    # "submitted" isn't settable here — it's only reached by actually placing a bid, so the
    # status can't drift away from what happened on Freelancer.
    status: str | None = Field(default=None, pattern="^(NEW|VIEWED|APPLIED|DISMISSED)$")


class BidRequest(BaseModel):
    amount: float = Field(gt=0)
    period_days: int = Field(default=7, gt=0, le=365)
    milestone_percentage: int = Field(default=100, ge=1, le=100)
    # No default: placing a bid must be an explicit act, never the consequence of a bare POST.
    confirm: bool


class BidResult(BaseModel):
    bid_id: str
    amount: float
    period_days: int


class BidAvailabilityOut(BaseModel):
    available: bool
    reason: str


class SkillIn(BaseModel):
    name: str
    weight: float = 1.0


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)


    display_name: str
    headline: str
    skills: list[dict[str, Any]]
    keywords_include: list[str]
    keywords_exclude: list[str]
    fixed_project_min: float
    rate_min: float
    currency: str
    max_existing_bids: int
    min_match_score: float
    weight_skills: float
    weight_budget: float
    weight_competition: float
    weight_recency: float
    proposal_notes: str

    # When the board was last recalculated against the marketplace, and when the profile itself
    # was last edited. A profile that drifts out of date is the quiet failure mode here: the
    # scores stay confident while the thing they were computed from has moved on.
    last_synced_at: dt.datetime | None
    updated_at: dt.datetime
    # Computed server-side: a wrong client clock shouldn't decide whether scores look stale.
    # Defaulted so it can be validated from the ORM row, then filled in by the route.
    sync_is_stale: bool = False


class ProfileIn(BaseModel):
    display_name: str = ""
    headline: str = ""
    skills: list[SkillIn] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    fixed_project_min: float = 0.0
    rate_min: float = 0.0
    currency: str = "USD"
    max_existing_bids: int = 25
    min_match_score: float = 55.0
    weight_skills: float = 60.0
    weight_budget: float = 20.0
    weight_competition: float = 10.0
    weight_recency: float = 10.0
    proposal_notes: str = ""


class AuthStatus(BaseModel):
    connected: bool
    platform: str = "freelancer"
    scope: str | None = None
    expires_at: dt.datetime | None = None
    detail: str | None = None


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None


class RoleUpdate(BaseModel):
    role: str = Field(pattern="^(user|admin)$")


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None
    job_count: int
    connected: bool
    connection_scope: str | None


class CycleRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    started_at: dt.datetime
    duration_ms: int
    fetched: int
    created: int
    updated: int
    rejected: int
    drafted: int
    draft_failures: int
    authenticated: bool
    trigger: str
    error: str | None


class AdminOverview(BaseModel):
    total_users: int
    active_users: int
    connected_accounts: int
    total_jobs: int
    matched_jobs: int
    drafted_jobs: int
    bids_placed: int
    proposal_input_tokens: int
    proposal_output_tokens: int
    cycles_24h: int
    failed_cycles_24h: int
    draft_failures_24h: int
    last_cycle_at: dt.datetime | None


class ProposalOut(BaseModel):
    """A bid or draft, with the score that recommended it.

    Pairing the two is the point: it's the only way to tell whether the scoring is picking work
    you actually win, rather than work that merely looks good on paper.
    """

    id: uuid.UUID
    recommendation_id: uuid.UUID | None

    # Who placed it. Two people on the same platform can bid the same project, so a proposal is
    # only meaningful attached to a freelancer.
    user_id: uuid.UUID
    user_email: str
    freelancer_name: str

    # Whether this came from our recommendation or was bid independently. Without the
    # distinction, our score would appear to "cover" bids it never influenced, which would
    # flatter the calibration numbers below.
    was_recommended: bool

    project_title: str
    project_url: str
    platform: str
    external_id: str | None

    score: float | None
    reasons: list[dict[str, Any]]

    proposal_text: str | None
    bid_amount: float | None
    estimated_days: int | None
    currency: str | None

    status: str
    submitted_via: str | None
    external_bid_id: str | None
    submitted_at: dt.datetime | None
    drafted_at: dt.datetime | None
    created_at: dt.datetime

    model: str | None
    input_tokens: int | None
    output_tokens: int | None


class ProposalStats(BaseModel):
    """Outcome by score band — does a higher score actually convert?"""

    total: int
    drafted: int
    submitted: int
    accepted: int
    rejected: int
    avg_score_submitted: float | None
    avg_score_accepted: float | None
    total_output_tokens: int

    # Split by origin, so the score is judged only on the bids it actually drove.
    from_recommendation: int
    self_directed: int
