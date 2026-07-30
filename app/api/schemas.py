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
    # The same budget converted into the freelancer's local currency (from their account's country)
    # for an at-a-glance read, alongside the listed figures above. Null when the job is already in
    # the local currency or no conversion rate is available — the UI then shows only the listed one.
    local_currency: str | None = None
    budget_min_local: float | None = None
    budget_max_local: float | None = None
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
    # Mirrored from the connected account during sync; read-only here. Display uses it to label the
    # freelancer's home currency alongside each job's listed one.
    country: str | None = None
    crowded_at_bids: int
    min_match_score: float
    weight_skills: float
    weight_budget: float
    weight_competition: float
    weight_recency: float
    proposal_notes: str

    # LLM-proposed skills awaiting the freelancer's decision (see services.skill_suggest). Ride the
    # same shape the edit form already reads so the suggestions render without a second fetch.
    suggested_skills: list[dict[str, Any]] = []

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
    crowded_at_bids: int = 25
    min_match_score: float = 55.0
    weight_skills: float = 60.0
    weight_budget: float = 20.0
    weight_competition: float = 10.0
    weight_recency: float = 10.0
    proposal_notes: str = ""


class SkillsAcceptIn(BaseModel):
    """Skills the freelancer confirmed from the suggestions — editable, so name and weight may
    differ from what was proposed. Each is added to the profile's real ``skills`` at full trust."""

    skills: list[SkillIn] = Field(default_factory=list)


class SkillNamesIn(BaseModel):
    """Suggestion names to drop, unaccepted."""

    names: list[str] = Field(default_factory=list)


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

    # Nothing currently syncs outcomes back from the marketplace, so a sent bid's result is
    # genuinely unknown rather than pending. Saying so is the difference between an honest
    # dashboard and one that implies it lost work it simply never heard about.
    outcome_tracking_enabled: bool = False
    awaiting_outcome: int


class ConnectionOut(BaseModel):
    """One marketplace account linked to this user.

    Counts are per account, not per user: with two accounts connected, a shared total would say
    nothing about which one is actually winning work.
    """

    id: uuid.UUID
    platform: str
    proposals: int = 0
    wins: int = 0
    platform_username: str | None
    scope: str | None
    rating: float | None
    total_reviews: int | None
    avatar_url: str | None
    status: str
    # True for the account the app is currently scoped to. At most one connection has it.
    is_selected: bool = False
    # The account's public profile on the marketplace, mirrored on each sync.
    display_name: str | None = None
    tagline: str | None = None
    summary: str | None = None
    account_skills: list[str] = []
    hourly_rate: float | None = None
    currency: str | None = None
    country: str | None = None
    portfolio_count: int | None = None
    member_since: dt.datetime | None = None
    connected_at: dt.datetime | None
    last_synced_at: dt.datetime | None


class ProfileCard(BaseModel):
    """Summary for the browse view — enough to recognise a profile, not enough to edit it."""

    id: uuid.UUID
    display_name: str
    headline: str
    profile_image: str | None
    initials: str
    skills: list[str]
    skill_count: int
    rate_min: float
    rate_max: float
    currency: str
    availability: str
    status: str
    platforms: list[str]
    # True for the account (profile) the app is currently scoped to. At most one per user.
    is_selected: bool = False
    last_synced_at: dt.datetime | None
    bids_synced_at: dt.datetime | None
    recommendations: int
    proposals: int
    wins: int


class ProfileDetail(ProfileCard):
    """Everything, for the opened profile."""

    bio: str
    weighted_skills: list[dict[str, Any]]
    portfolio: list[dict[str, Any]]
    experience: list[dict[str, Any]]
    education: list[dict[str, Any]]
    keywords_include: list[str]
    keywords_exclude: list[str]
    fixed_project_min: float
    crowded_at_bids: int
    min_match_score: float
    weight_skills: float
    weight_budget: float
    weight_competition: float
    weight_recency: float
    proposal_notes: str
    connections: list[ConnectionOut]
    avg_score: float | None
    bids_synced_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime



class FullSyncOut(BaseModel):
    """Result of syncing everything for a profile.

    Both halves report separately even though one button triggers them: a marketplace fetch and a
    bid pull fail for different reasons, and collapsing them into one status would hide which.
    """

    board_fetched: int
    board_new: int
    board_changed: int
    board_drafted: int
    board_error: str | None

    bids_fetched: int
    bids_imported: int
    outcomes_updated: int
    bids_error: str | None

    last_synced_at: dt.datetime | None
    bids_synced_at: dt.datetime | None


class SelectionIn(BaseModel):
    """Which account to scope the app to. ``None`` means all of them."""

    connection_id: uuid.UUID | None = None


class DemoRequestIn(BaseModel):
    """A demo request from the marketing page. Public — no account required."""

    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    note: str | None = Field(default=None, max_length=2000)
    marketplace: str | None = Field(default=None, max_length=50)


class DemoRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    note: str | None
    marketplace: str | None
    handled: bool
    created_at: dt.datetime


class CapturedClient(BaseModel):
    """The hiring client, as the posting advertises them.

    Stored on the project but never scored directly — a client's history is what a person reads to
    decide whether a bid is worth the connects, so it travels with the posting rather than being a
    second lookup.
    """

    name: str | None = None
    rating: float | None = None
    country: str | None = None
    reviews: int | None = None
    total_spent: float | None = None
    total_hires: int | None = None
    payment_verified: bool | None = None
    member_since: str | None = None

    def columns(self) -> dict[str, Any]:
        """Only the four that have a column of their own on ``projects``."""
        return {
            "client_name": self.name,
            "client_rating": self.rating,
            "client_country": self.country,
            "client_reviews_count": self.reviews,
        }


class CapturedPosting(BaseModel):
    """A posting read off a page by the extension.

    Everything optional is genuinely optional: marketplace layouts vary and a field the scraper
    couldn't find must arrive as null, never as zero. Scoring skips a filter it has no input for,
    so a missing budget means "unknown" rather than "free".
    """

    platform: str = Field(default="upwork", max_length=50)
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=1000)
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    skills: list[str] = []
    work_type: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str | None = None
    proposal_count: int | None = None
    posted_at: dt.datetime | None = None
    # Sent by the extension's job-page reader, which has always scraped it. Optional so an older
    # extension build keeps working unchanged.
    client: CapturedClient | None = None
    experience_level: str | None = None
    project_length: str | None = None

    # Ask the LLM to read the raw page and fill any field the selectors missed (see /ingest/parse).
    # Costs a model call, so it's opt-in: the extension sets it when it wants accuracy over speed,
    # and sends the visible page text for the model to read.
    is_llm_required: bool = False
    page_text: str | None = Field(default=None, max_length=200_000)


class CapturedPage(BaseModel):
    """One page the extension finished reading, sent as its selectors found it.

    Raw on purpose. ``budget`` arrives as ``"$500.00 - $1,000.00"`` and ``posted`` as
    ``"3 hours ago"``, because parsing those belongs in one tested place on this side rather than
    duplicated in JavaScript — see ``services.capture``.
    """

    # Named for what it is rather than reusing ``platform``: this is the marketplace the pages were
    # read from, and the extension speaks in those terms throughout.
    freelance_platform: str = Field(max_length=50)
    page_key: str = Field(max_length=100)
    page_label: str = Field(default="", max_length=200)
    # Which reader ran, which is what decides how ``items`` is shaped. Mirrors the ``reads``
    # declaration in the extension's platform registry.
    reads: str = Field(pattern="^(jobs|rows|rooms)$")
    page_url: str = Field(default="", max_length=1000)
    # The moment the page was read, from the client's clock. Relative timestamps ("3 hours ago") are
    # resolved against this, so a payload that waited in a queue does not drift.
    scraped_at: dt.datetime | None = None

    # Whether the reader got the page or a wall in front of it.
    #
    # Signed out, a marketplace serves a login page that loads perfectly and holds no jobs — so
    # without this a logged-out collection reports an honest-looking zero for every page, and the
    # board stops being refreshed while still looking authoritative. Recorded against the user so
    # the app can say "your Upwork session expired" instead of showing stale scores.
    page_status: str = Field(default="ok", pattern="^(ok|signed_out|blocked)$")
    # The reader's own words, so the app shows a reason rather than a status code.
    status_detail: str = Field(default="", max_length=500)

    # Whether to pay for an LLM reading of ``page_text`` to fill what the selectors missed. A
    # request, not an instruction: the server still skips the call when nothing is actually missing,
    # and the response says whether it happened.
    is_llm_required: bool = False

    items: list[dict[str, Any]] = []
    # Only sent when ``is_llm_required``; the LLM's entire input.
    page_text: str = Field(default="", max_length=200_000)


class CapturedItemResult(BaseModel):
    external_id: str | None
    title: str
    project_id: uuid.UUID | None
    created: bool
    score: float | None
    rejected: bool
    rejection_reason: str | None


class CapturedPageResult(BaseModel):
    """What became of one captured page. Counts first, then per-item detail.

    Deliberately granular. "Sent ✓" is the least useful thing a response can say — a page whose
    selectors found twelve links and stored none is a broken selector, and that must be visible
    without opening the database.
    """

    freelance_platform: str
    page_key: str
    reads: str
    received: int
    stored: int
    created: int
    updated: int
    # Rows that arrived with no marketplace id, and so nothing to dedupe a later sighting against.
    skipped_no_id: int = 0
    # Rows that were the same posting as another in this payload.
    duplicates: int = 0
    llm_used: bool = False
    llm_model: str | None = None
    llm_fields_filled: int = 0
    llm_unmatched: int = 0
    llm_error: str | None = None
    # Set instead of ``items`` when the page was kept as a raw capture — contracts, proposals,
    # orders, room lists. Something to look the accumulated rows up by.
    capture_id: uuid.UUID | None = None
    # Echoed back so the extension can confirm the app knows, rather than assume it landed.
    session_status: str = "OK"
    note: str | None = None
    items: list[CapturedItemResult] = []


class CaptureStatusOut(BaseModel):
    """Whether the extension can currently read one marketplace — what a banner renders from."""

    model_config = ConfigDict(from_attributes=True)

    platform: str
    status: str
    detail: str | None
    page_key: str | None
    # When it first went wrong; null while OK. "Signed out for three days" and "signed out just
    # now" deserve different words, which one timestamp cannot say.
    since: dt.datetime | None
    last_checked_at: dt.datetime
    # Null means the extension has never read this marketplace — not that anything expired.
    last_ok_at: dt.datetime | None


class CaptureResult(BaseModel):
    project_id: uuid.UUID
    recommendation_id: uuid.UUID
    created: bool
    score: float
    rejected: bool
    rejection_reason: str | None
    reasons: list[dict[str, Any]]

    # Whether the LLM was asked to read the page, which model answered, how many fields it filled,
    # and — distinct from "not used" — whether it was asked but failed.
    llm_used: bool = False
    llm_model: str | None = None
    llm_fields_filled: int = 0
    llm_error: str | None = None


class CapturedProfile(BaseModel):
    """Your own marketplace profile, read off your profile page."""

    platform: str = Field(default="upwork", max_length=50)
    username: str = Field(min_length=1, max_length=255)

    # The account's stable marketplace id, read from the profile URL (Upwork's ~01… cipher id, a
    # numeric id elsewhere). This — not the mutable username — is the account's identity: it keys
    # the connection and lines up with the id OAuth stores, so one real account is one connection.
    # Optional so an older extension that only sends the handle still works (username is fallback).
    account_id: str | None = Field(default=None, max_length=255)

    # Whether the extension confirmed this is the *signed-in user's own* profile, by comparing the
    # account id in the URL against the id the marketplace's own header links to.
    #
    # No URL pattern can make that distinction — ``/freelancers/~01…`` matches every freelancer on
    # Upwork — and this payload overwrites the profile row every score in the app is computed from.
    # ``None`` means the extension could not tell, and is refused like ``False``: "probably yours"
    # is not good enough. Defaults to ``None`` so an older extension build fails closed.
    is_own: bool | None = None
    display_name: str | None = None
    tagline: str | None = None
    summary: str | None = None
    skills: list[str] = []
    hourly_rate: float | None = None
    currency: str | None = None
    country: str | None = None

    # Ask the LLM to read the raw profile page and fill any field the selectors missed. Opt-in and
    # costs a model call — the extension sets it and sends the page text when accuracy matters most.
    is_llm_required: bool = False
    page_text: str | None = Field(default=None, max_length=200_000)
    avatar_url: str | None = None
    rating: float | None = None
    total_reviews: int | None = None


class ApiTokenOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None


class ApiTokenCreated(ApiTokenOut):
    """The plaintext is returned exactly once, at creation, and never stored."""

    token: str


class ApiTokenIn(BaseModel):
    name: str = Field(default="Chrome extension", max_length=100)


class PageParseIn(BaseModel):
    """Visible text of a page, for the LLM reader."""

    # The list kinds (jobs, proposals, contracts) return an ``items`` array in ``fields``; the
    # singular kinds return one object. See ``services.page_parse.LIST_KINDS``.
    kind: str = Field(pattern="^(profile|job|jobs|proposals|contracts)$")
    url: str = Field(default="", max_length=1000)
    text: str = Field(min_length=40, max_length=200_000)


class PageParseOut(BaseModel):
    fields: dict[str, Any]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    truncated_input: bool
