"""The ingest cycle: fetch -> normalise -> upsert project -> score -> recommend -> draft.

Nothing in here is allowed to raise past ``run_cycle``. The poller calls this every ~25s forever,
so one bad response, one rate limit, or one refused draft must cost us that item and nothing more.

A posting is stored **once**, in ``projects``. What each user thinks of it lives in
``recommendations``. That split is why adding the thousandth user costs a thousand score rows and
not a thousand copies of every description.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.freelancer_oauth import OAuthError, get_valid_access_token
from app.config import get_settings
from app.connectors.freelancer import FreelancerAPIError, FreelancerClient, JobPosting
from app.db.models import (
    CycleRun,
    DiscoveryMethod,
    FreelancerProfile,
    Project,
    Proposal,
    ProposalAIVersion,
    ProposalStatus,
    Recommendation,
    RecommendationStatus,
    utcnow,
)
from app.services.drafting import DraftingError, draft_proposal
from app.services.matching import MatchingError, SkillMatch, match_skills
from app.services.scoring import hard_reject_reason, score_job
from app.services.skill_expansion import SkillExpansionError, expand_skill_terms
from app.services.users import get_or_create_default_user, get_or_create_profile

logger = logging.getLogger(__name__)

# Freelancer's API terms require cached data to be refreshed at least every 24h.
STALE_AFTER = dt.timedelta(hours=24)

# Cap drafts per cycle so a backlog can't produce a burst of API spend in one go.
MAX_DRAFTS_PER_CYCLE = 5

# Incremental discovery. Each cycle pages newest-first from the last watermark forward, so a busy
# board can't push new postings past a single page of "newest".
DISCOVERY_PAGE_SIZE = 100
DISCOVERY_MAX_PAGES = 5  # hard ceiling on projects pulled per cycle (page_size * pages)
# Re-include the boundary second so nothing slips between cycles; the upsert in ``_ingest`` makes
# the handful of re-seen rows idempotent.
WATERMARK_OVERLAP = dt.timedelta(seconds=60)


@dataclass
class _MatchBudget:
    """Caps how many *new* semantic matches one cycle pays the LLM for, so widening the skill
    filter can't turn discovery volume straight into spend. Cache hits and hard-rejected jobs never
    draw from it; a job denied a slot falls back to substring this cycle and gets its match on a
    later one (the cache persists once computed). ``remaining is None`` means no ceiling."""

    remaining: int | None

    def take(self) -> bool:
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass
class CycleReport:
    fetched: int = 0
    new: int = 0
    # "updated" counts postings whose content actually moved, not every one seen again.
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    drafted: int = 0
    draft_failures: int = 0
    authenticated: bool = True
    unmatched_skills: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


async def run_cycle(session: AsyncSession, trigger: str = "poll") -> CycleReport:
    """Run one cycle and record what happened."""
    started = time.monotonic()
    report = CycleReport()

    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    # A token is optional: the public project search works unauthenticated, so an unconnected
    # account degrades to reduced discovery rather than no product at all.
    try:
        token = await get_valid_access_token(session, user.id)
    except OAuthError as exc:
        token = None
        report.authenticated = False
        logger.info("Running unauthenticated: %s", exc)

    client = FreelancerClient(access_token=token)

    # Filter server-side by the profile's skills, broadened into the marketplace's tag vocabulary
    # so generically-tagged jobs surface too. Cached on the profile and recomputed only when the
    # confirmed skills change (see ``_resolve_search_skill_ids``); an empty result searches
    # unfiltered rather than not at all.
    skill_ids = await _resolve_search_skill_ids(client, profile, report)

    # First run has no watermark: bootstrap from the newest page instead of backfilling the whole
    # board. After that, page forward from where the last cycle stopped, bounded by the cap.
    max_pages = DISCOVERY_MAX_PAGES if profile.last_project_submitted_at else 1
    try:
        postings, truncated = await _fetch_new_postings(
            client, skill_ids, profile.last_project_submitted_at, max_pages
        )
    except FreelancerAPIError as exc:
        report.error = str(exc)
        logger.warning("Skipping cycle: %s", exc)
        await _record(session, user.id, report, started, trigger)
        return report

    if truncated:
        logger.warning(
            "Discovery hit the %d-page cap (%d projects); newer postings may have been skipped "
            "this cycle — consider a shorter poll interval or a higher cap.",
            max_pages,
            max_pages * DISCOVERY_PAGE_SIZE,
        )

    report.fetched = len(postings)

    settings = get_settings()
    match_budget = _MatchBudget(
        remaining=None
        if settings.skill_match_max_per_cycle <= 0
        else settings.skill_match_max_per_cycle
    )

    for posting in postings:
        try:
            created, rejected, changed = await _ingest(session, profile, posting, match_budget)
        except Exception:  # one malformed posting must not end the cycle
            logger.exception("Failed to store posting %s", posting.external_id)
            continue
        report.new += int(created)
        report.updated += int(changed)
        report.unchanged += int(not created and not changed)
        report.rejected += int(rejected)

    # Advance the watermark to the newest posting we pulled, so the next cycle starts just past it.
    newest = max((p.posted_at for p in postings if p.posted_at is not None), default=None)
    if newest is not None and (
        profile.last_project_submitted_at is None
        or newest > profile.last_project_submitted_at
    ):
        profile.last_project_submitted_at = newest

    profile.last_synced_at = utcnow()
    await session.commit()

    report.drafted, report.draft_failures = await _draft_pending(session, profile)

    await _record(session, user.id, report, started, trigger)

    logger.info(
        "Cycle: fetched=%d new=%d changed=%d unchanged=%d drafted=%d draft_failures=%d",
        report.fetched,
        report.new,
        report.updated,
        report.unchanged,
        report.drafted,
        report.draft_failures,
    )
    return report


async def _fetch_new_postings(
    client: FreelancerClient,
    skill_ids: list[int],
    since: dt.datetime | None,
    max_pages: int,
) -> tuple[list[JobPosting], bool]:
    """Page active projects newest-first from ``since`` (the last watermark) forward.

    Returns ``(postings, truncated)``. ``truncated`` is True when the page cap was hit before the
    board ran out — the honest signal that postings are arriving faster than one cycle can drain,
    so some new ones were skipped this cycle. On the first run (``since`` is None) the caller keeps
    ``max_pages`` small so this bootstraps from the newest page rather than backfilling the board.
    """
    from_time = int((since - WATERMARK_OVERLAP).timestamp()) if since is not None else None

    collected: list[JobPosting] = []
    offset = 0
    for _ in range(max_pages):
        batch = await client.search_active_projects(
            skill_ids=skill_ids or None,
            from_time=from_time,
            offset=offset,
            limit=DISCOVERY_PAGE_SIZE,
        )
        if not batch:
            return collected, False  # an empty page means the board ran out — we caught up
        collected.extend(batch)
        # Advance by what actually came back, not by our requested page size: Freelancer caps a page
        # at its own maximum (≈50), so assuming we got a full ``DISCOVERY_PAGE_SIZE`` would skip
        # rows and, worse, stop after one short page. Only an empty page means "done".
        offset += len(batch)
    return collected, True  # hit the page cap without draining — more likely remain than not


def _search_skill_ids_key(names: list[str], expansion_enabled: bool) -> str:
    """Cache key for the resolved discovery IDs: the confirmed skills plus whether expansion is on.
    It moves when either changes, so the IDs recompute exactly then rather than every cycle."""
    payload = json.dumps([sorted(names), expansion_enabled], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _resolve_search_skill_ids(
    client: FreelancerClient, profile: FreelancerProfile, report: CycleReport
) -> list[int]:
    """Resolve the profile's confirmed skills — widened for discovery — to Freelancer skill IDs.

    Cached on the profile: the LLM expansion and the catalogue lookup run only when the confirmed
    skills change, not every cycle. On any failure it keeps the last good IDs rather than dropping
    the filter, so a transient blip can't turn a specialist search into the whole marketplace.
    """
    names = [s["name"] for s in (profile.skills or []) if s.get("name")]
    key = _search_skill_ids_key(names, get_settings().skill_expansion_enabled)

    if profile.search_skill_ids_key == key:
        return list(profile.search_skill_ids or [])  # inputs unchanged — reuse the cached IDs

    if not names:
        profile.search_skill_ids = []
        profile.search_skill_ids_key = key
        return []

    try:
        direct_ids, unmatched = await client.resolve_skill_ids(names)
    except FreelancerAPIError as exc:
        logger.warning("Could not resolve skill filters, keeping the last set: %s", exc)
        return list(profile.search_skill_ids or [])

    report.unmatched_skills = unmatched
    if unmatched:
        logger.info(
            "Profile skills with no Freelancer equivalent (ignored in search): %s",
            ", ".join(unmatched),
        )

    ids = list(direct_ids)
    expansion_complete = True
    try:
        extra_terms = await expand_skill_terms(names)
    except SkillExpansionError as exc:
        expansion_complete = False
        extra_terms = []
        logger.warning("Skill expansion failed, searching on listed skills only: %s", exc)

    if extra_terms:
        try:
            extra_ids, _ = await client.resolve_skill_ids(extra_terms)
        except FreelancerAPIError as exc:
            expansion_complete = False
            extra_ids = []
            logger.warning("Could not resolve expanded skills: %s", exc)
        for skill_id in extra_ids:
            if skill_id not in ids:
                ids.append(skill_id)

    profile.search_skill_ids = ids
    # Pin the key only when the full set resolved. A partial (expansion-failed) result leaves the
    # key stale so the next cycle retries the expansion rather than sticking as direct-only.
    if expansion_complete:
        profile.search_skill_ids_key = key
    return ids


def _fingerprint(posting: JobPosting) -> str:
    """Hash of the fields a freelancer would care about changing."""
    payload = json.dumps(
        [
            posting.title,
            posting.description,
            posting.budget_min,
            posting.budget_max,
            posting.bid_count,
            sorted(posting.skills_listed or []),
        ],
        default=str,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _skill_match_key(profile: FreelancerProfile, content_hash: str) -> str:
    """Cache key for a semantic skill match: the profile's skills plus the posting's content.

    Reuses the posting's ``content_hash`` for the job side, so the match invalidates on the same
    field changes ingest already tracks. Any change to the freelancer's skill *names* moves the key
    too; skill weights don't, since the LLM only judges the names.
    """
    names = sorted(s["name"] for s in (profile.skills or []) if s.get("name"))
    payload = json.dumps([names, content_hash], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _worth_matching(posting: JobPosting, profile: FreelancerProfile) -> bool:
    """Whether a semantic match could change this job's fate — the test for spending the LLM.

    Skip it when the feature is off, the profile lists no skills, or a hard filter already rejects
    the job: hard filters run before skills in scoring and reject regardless of fit, so a match
    would be wasted. Jobs that merely score low still qualify — lifting a low skills score over the
    threshold is exactly what the semantic match is for, so those must not be gated out here.
    """
    if not get_settings().skill_match_enabled:
        return False
    if not any(s.get("name") for s in (profile.skills or [])):
        return False
    return hard_reject_reason(posting, profile) is None


def _cached_skill_match(rec: Recommendation, key: str) -> SkillMatch | None:
    """The stored match if it was computed for this exact key, else ``None`` to recompute."""
    if rec.skill_match_key == key and rec.skill_match_score is not None:
        return SkillMatch(score=rec.skill_match_score, reason=rec.skill_match_reason or "")
    return None


def _store_skill_match(rec: Recommendation, key: str, match: SkillMatch | None) -> None:
    """Cache a freshly computed match. A ``None`` match (feature off, no key, or a failed call)
    clears the cache and leaves the key set, so an unchanged posting isn't retried every cycle —
    it retries once its content or the profile's skills move."""
    rec.skill_match_key = key
    rec.skill_match_score = match.score if match else None
    rec.skill_match_reason = match.reason if match else None


async def _ingest(
    session: AsyncSession,
    profile: FreelancerProfile,
    posting: JobPosting,
    match_budget: _MatchBudget,
) -> tuple[bool, bool, bool]:
    """Upsert the project, then this profile's recommendation for it.

    Returns ``(created, rejected, content_changed)``. Re-scoring on every sighting is what makes a
    profile change take effect on postings already stored.
    """
    project = await session.scalar(
        select(Project).where(
            Project.platform == posting.platform, Project.external_id == posting.external_id
        )
    )
    if project is None:
        project = Project(
            platform=posting.platform,
            external_id=posting.external_id,
            discovery_method=DiscoveryMethod.API_POLL,
        )
        session.add(project)

    # Compare a fingerprint of the fields worth reacting to. Re-fetching the same posting every
    # half hour would otherwise look like a change every time, and "updated" would mean nothing.
    incoming_hash = _fingerprint(posting)
    content_changed = project.content_hash is not None and project.content_hash != incoming_hash

    project.title = posting.title
    project.description = posting.description
    project.project_url = posting.url
    project.required_skills = posting.skills_listed
    project.work_type = posting.budget_type
    project.min_budget = posting.budget_min
    project.max_budget = posting.budget_max
    project.currency = posting.currency
    project.bid_information = {"bid_count": posting.bid_count}
    project.posted_at = posting.posted_at
    project.content_hash = incoming_hash
    project.updated_at = utcnow()
    if content_changed:
        project.last_changed_at = utcnow()

    # The recommendation's FK needs the project's id, which only exists after a flush.
    await session.flush()

    recommendation = await session.scalar(
        select(Recommendation).where(
            Recommendation.freelancer_id == profile.id,
            Recommendation.project_id == project.id,
        )
    )
    created = recommendation is None
    if recommendation is None:
        recommendation = Recommendation(freelancer_id=profile.id, project_id=project.id)
        session.add(recommendation)

    # Semantic matching is the one paid step here. Three things keep its cost bounded as discovery
    # widens: the cache (unchanged pair → reuse, free), the hard-filter gate (skip jobs a keyword
    # or budget filter rejects regardless of skill fit), and the per-cycle budget (cap fresh
    # matches so volume can't turn into unbounded spend). A cache hit bypasses the latter two.
    match_key = _skill_match_key(profile, incoming_hash)
    skill_match = _cached_skill_match(recommendation, match_key)
    if skill_match is None and _worth_matching(posting, profile) and match_budget.take():
        try:
            skill_match = await match_skills(posting, profile)
        except MatchingError as exc:
            # A failed call must not stop ingest — scoring falls back to substring matching.
            logger.warning("Skill match failed for %s, using substring: %s", posting.external_id, exc)
            skill_match = None
        # Only cache a genuine verdict. A budget-deferred job never reaches here, so it stays a
        # cache miss and gets another turn on a later cycle.
        _store_skill_match(recommendation, match_key, skill_match)

    result = score_job(posting, profile, skill_match=skill_match)

    recommendation.score = result.score
    recommendation.reasons = result.reasons
    recommendation.is_hard_rejected = result.rejected
    recommendation.rejection_reason = result.rejection_reason
    recommendation.updated_at = utcnow()

    # Flag it only if the posting genuinely moved and the user has already looked — telling
    # someone a job they've never opened has "changed" is noise.
    if content_changed and not created and recommendation.status != RecommendationStatus.NEW:
        recommendation.has_changes = True
        recommendation.changed_at = utcnow()

    return created, result.rejected, content_changed


async def _draft_pending(session: AsyncSession, profile: FreelancerProfile) -> tuple[int, int]:
    """Draft proposals for the best recommendations that don't have one yet."""
    drafted_ids = select(Proposal.recommendation_id).where(
        Proposal.recommendation_id.is_not(None)
    )
    rows = (
        (
            await session.scalars(
                select(Recommendation)
                .where(
                    Recommendation.freelancer_id == profile.id,
                    Recommendation.is_hard_rejected.is_(False),
                    Recommendation.status == RecommendationStatus.NEW,
                    Recommendation.id.not_in(drafted_ids),
                )
                .order_by(Recommendation.score.desc())
                .limit(MAX_DRAFTS_PER_CYCLE)
            )
        )
        .unique()
        .all()
    )

    drafted = failures = 0
    for rec in rows:
        try:
            draft = await draft_proposal(_to_posting(rec.project), profile)
        except DraftingError as exc:
            failures += 1
            logger.warning("Draft failed for project %s: %s", rec.project.external_id, exc)
            continue
        except Exception:
            failures += 1
            logger.exception("Unexpected drafting error for project %s", rec.project.external_id)
            continue

        proposal = Proposal(
            project_id=rec.project_id,
            freelancer_id=profile.id,
            recommendation_id=rec.id,
            proposal_text=draft.text,
            status=ProposalStatus.DRAFT,
            model=draft.model,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            drafted_at=utcnow(),
        )
        session.add(proposal)
        await session.flush()

        # Keep the generation before any hand-editing overwrites it: comparing what the model
        # wrote against what was actually sent is the only honest read on draft quality.
        session.add(
            ProposalAIVersion(
                proposal_id=proposal.id,
                version=1,
                generated_text=draft.text,
                model=draft.model,
                input_tokens=draft.input_tokens,
                output_tokens=draft.output_tokens,
            )
        )
        drafted += 1

    if drafted:
        await session.commit()
    return drafted, failures


async def rescore_all(session: AsyncSession, profile: FreelancerProfile) -> int:
    """Re-run scoring over every stored recommendation. Called after a profile change."""
    rows = (
        (
            await session.scalars(
                select(Recommendation).where(Recommendation.freelancer_id == profile.id)
            )
        )
        .unique()
        .all()
    )

    for rec in rows:
        # Rescore is sync and never calls the LLM: reuse the cached match if the profile's skills
        # and the posting are unchanged (a weight-only tweak keeps it), else fall back to substring.
        # A skills change invalidates the key; the next poll cycle recomputes the semantic match.
        skill_match = None
        if rec.project.content_hash:
            key = _skill_match_key(profile, rec.project.content_hash)
            skill_match = _cached_skill_match(rec, key)
        result = score_job(_to_posting(rec.project), profile, skill_match=skill_match)
        rec.score = result.score
        rec.reasons = result.reasons
        rec.is_hard_rejected = result.rejected
        rec.rejection_reason = result.rejection_reason

    await session.commit()
    return len(rows)


async def prune_stale_projects(session: AsyncSession) -> int:
    """Drop projects not seen in the last 24h, per Freelancer's cached-data refresh requirement.

    Anything a user drafted or bid on is kept — that's their record, not cached platform data.
    """
    cutoff = utcnow() - STALE_AFTER
    acted_on = select(Proposal.project_id).distinct()
    rows = (
        await session.scalars(
            select(Project).where(Project.updated_at < cutoff, Project.id.not_in(acted_on))
        )
    ).all()

    for row in rows:
        await session.delete(row)

    if rows:
        await session.commit()
    return len(rows)


async def _record(
    session: AsyncSession,
    user_id: uuid.UUID,
    report: CycleReport,
    started: float,
    trigger: str,
) -> None:
    """Persist the cycle. A failure to record must never turn a good cycle into a bad one."""
    try:
        session.add(
            CycleRun(
                user_id=user_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                fetched=report.fetched,
                created=report.new,
                updated=report.updated,
                rejected=report.rejected,
                drafted=report.drafted,
                draft_failures=report.draft_failures,
                authenticated=report.authenticated,
                trigger=trigger,
                error=report.error,
            )
        )
        await session.commit()
    except Exception:
        logger.exception("Could not record cycle run")


def _to_posting(project: Project) -> JobPosting:
    """Back to the platform-neutral shape scoring and drafting work with."""
    return JobPosting(
        platform=project.platform,
        external_id=project.external_id or "",
        title=project.title,
        description=project.description,
        url=project.project_url,
        skills_listed=project.required_skills or [],
        budget_type=project.work_type,
        budget_min=project.min_budget,
        budget_max=project.max_budget,
        currency=project.currency,
        bid_count=project.bid_count,
        posted_at=project.posted_at,
    )


# The scheduler imports this name; keep it stable across the rename.
prune_stale_jobs = prune_stale_projects
