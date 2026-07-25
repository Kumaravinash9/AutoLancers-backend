"""The ingest cycle: fetch -> normalise -> score -> persist -> draft.

Nothing in here is allowed to raise past ``run_cycle``. The poller calls this every ~25s forever,
so one bad response, one rate limit, or one refused draft must cost us that item and nothing more.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.freelancer_oauth import OAuthError, get_valid_access_token
from app.connectors.freelancer import FreelancerAPIError, FreelancerClient, JobPosting
from app.db.models import Job, Profile, utcnow
from app.services.drafting import DraftingError, draft_proposal
from app.services.scoring import score_job
from app.services.users import get_or_create_default_user, get_or_create_profile

logger = logging.getLogger(__name__)

# Freelancer's API terms require cached data to be refreshed at least every 24h.
STALE_AFTER = dt.timedelta(hours=24)

# Cap drafts per cycle so a backlog can't produce a burst of API spend in one go.
MAX_DRAFTS_PER_CYCLE = 5


@dataclass
class CycleReport:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    rejected: int = 0
    drafted: int = 0
    draft_failures: int = 0
    authenticated: bool = True
    unmatched_skills: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


async def run_cycle(session: AsyncSession) -> CycleReport:
    report = CycleReport()

    user = await get_or_create_default_user(session)
    profile = await get_or_create_profile(session, user.id)

    # A token is optional. The public project search works unauthenticated — it just returns
    # fewer fields and has lower rate limits — so an unconnected account degrades to reduced
    # discovery rather than no product at all.
    try:
        token = await get_valid_access_token(session, user.id)
    except OAuthError as exc:
        token = None
        report.authenticated = False
        logger.info("Running unauthenticated: %s", exc)

    client = FreelancerClient(access_token=token)

    # Filter server-side by the profile's skills. Without this the search returns whatever was
    # posted most recently across the entire marketplace, which for a specialist profile is
    # overwhelmingly irrelevant — you end up scoring logo and SEO gigs against a Next.js profile.
    skill_ids: list[int] = []
    try:
        names = [s["name"] for s in (profile.skills or []) if s.get("name")]
        if names:
            skill_ids, unmatched = await client.resolve_skill_ids(names)
            report.unmatched_skills = unmatched
            if unmatched:
                logger.info(
                    "Profile skills with no Freelancer equivalent (ignored in search): %s",
                    ", ".join(unmatched),
                )
    except FreelancerAPIError as exc:
        # Losing the catalogue is not fatal — fall back to an unfiltered search.
        logger.warning("Could not resolve skill filters, searching unfiltered: %s", exc)

    try:
        postings = await client.search_active_projects(skill_ids=skill_ids or None)
    except FreelancerAPIError as exc:
        report.error = str(exc)
        logger.warning("Skipping cycle: %s", exc)
        return report

    report.fetched = len(postings)

    for posting in postings:
        try:
            created, rejected = await _upsert_scored(session, user.id, profile, posting)
        except Exception:  # one malformed posting must not end the cycle
            logger.exception("Failed to store posting %s", posting.external_id)
            continue
        if created:
            report.new += 1
        else:
            report.updated += 1
        if rejected:
            report.rejected += 1

    await session.commit()

    report.drafted, report.draft_failures = await _draft_pending(session, user.id, profile)

    logger.info(
        "Cycle: fetched=%d new=%d updated=%d drafted=%d draft_failures=%d",
        report.fetched,
        report.new,
        report.updated,
        report.drafted,
        report.draft_failures,
    )
    return report


async def _upsert_scored(
    session: AsyncSession, user_id: int, profile: Profile, posting: JobPosting
) -> tuple[bool, bool]:
    """Insert or update one posting with a fresh score. Returns ``(created, rejected)``.

    Updating matters: an existing row's bid count and description change, and re-scoring on every
    sighting is what makes profile tuning actually take effect on jobs already in the table.
    """
    result = score_job(posting, profile)

    row = await session.scalar(
        select(Job).where(
            Job.user_id == user_id,
            Job.platform == posting.platform,
            Job.external_id == posting.external_id,
        )
    )
    created = row is None
    if row is None:
        row = Job(
            user_id=user_id,
            platform=posting.platform,
            external_id=posting.external_id,
            first_seen_at=utcnow(),
        )
        session.add(row)

    row.title = posting.title
    row.description = posting.description
    row.url = posting.url
    row.skills_listed = posting.skills_listed
    row.budget_type = posting.budget_type
    row.budget_min = posting.budget_min
    row.budget_max = posting.budget_max
    row.currency = posting.currency
    row.bid_count = posting.bid_count
    row.posted_at = posting.posted_at
    row.refreshed_at = utcnow()

    row.score = result.score
    row.reasons = result.reasons
    row.rejected = result.rejected
    row.rejection_reason = result.rejection_reason

    # Never walk back a decision you've already made about a job.
    if row.status in ("new", "drafted"):
        row.status = "drafted" if row.proposal_text else "new"

    return created, result.rejected


async def _draft_pending(
    session: AsyncSession, user_id: int, profile: Profile
) -> tuple[int, int]:
    """Draft proposals for the best undrafted, unrejected jobs."""
    rows = (
        await session.scalars(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.rejected.is_(False),
                Job.proposal_text.is_(None),
                Job.status == "new",
            )
            .order_by(Job.score.desc())
            .limit(MAX_DRAFTS_PER_CYCLE)
        )
    ).all()

    drafted = failures = 0
    for row in rows:
        try:
            draft = await draft_proposal(_to_posting(row), profile)
        except DraftingError as exc:
            failures += 1
            logger.warning("Draft failed for job %s: %s", row.external_id, exc)
            continue
        except Exception:
            failures += 1
            logger.exception("Unexpected drafting error for job %s", row.external_id)
            continue

        row.proposal_text = draft.text
        row.proposal_model = draft.model
        row.proposal_input_tokens = draft.input_tokens
        row.proposal_output_tokens = draft.output_tokens
        row.proposal_drafted_at = utcnow()
        row.status = "drafted"
        drafted += 1

    if drafted:
        await session.commit()
    return drafted, failures


async def rescore_all(session: AsyncSession, user_id: int, profile: Profile) -> int:
    """Re-run scoring over every stored job. Called after a profile change."""
    rows = (await session.scalars(select(Job).where(Job.user_id == user_id))).all()

    for row in rows:
        result = score_job(_to_posting(row), profile)
        row.score = result.score
        row.reasons = result.reasons
        row.rejected = result.rejected
        row.rejection_reason = result.rejection_reason

    await session.commit()
    return len(rows)


async def prune_stale_jobs(session: AsyncSession) -> int:
    """Drop rows not seen in the last 24h, per Freelancer's cached-data refresh requirement.

    Jobs you've acted on are kept — they're your record, not cached platform data.
    """
    cutoff = utcnow() - STALE_AFTER
    rows = (
        await session.scalars(
            select(Job).where(
                Job.refreshed_at < cutoff,
                Job.status.notin_(("approved", "dismissed")),
            )
        )
    ).all()

    for row in rows:
        await session.delete(row)

    if rows:
        await session.commit()
    return len(rows)


def _to_posting(row: Job) -> JobPosting:
    return JobPosting(
        platform=row.platform,
        external_id=row.external_id,
        title=row.title,
        description=row.description,
        url=row.url,
        skills_listed=row.skills_listed or [],
        budget_type=row.budget_type,
        budget_min=row.budget_min,
        budget_max=row.budget_max,
        currency=row.currency,
        bid_count=row.bid_count,
        posted_at=row.posted_at,
    )
