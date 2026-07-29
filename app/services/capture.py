"""Turn what the browser extension scraped into stored projects.

The extension reads a marketplace page and sends the rows exactly as its selectors found them —
``budget`` as the string ``"$500.00 - $1,000.00"``, ``posted`` as ``"3 hours ago"``. Parsing that
into columns happens **here**, once, rather than in the extension: there is one parser to test, and
the same normalisation applies whether a row arrived from a listing page, from a single job page, or
from the LLM reader.

Two rules the rest of this module exists to enforce:

1. **A listing page and the job's own page are the same project.** "Best matches" and "Most recent"
   overlap heavily, and Upwork shows the same posting on both. Dedupe is on
   ``(platform, external_id)`` — the marketplace's own id, taken from the link's href, never from
   text — so the second sighting updates the first row instead of creating a twin.
2. **The LLM may only fill what selectors left empty.** It never overwrites a found value and it
   never introduces a project of its own. A model cannot see the address bar, so an item it invents
   has no id to dedupe on and would arrive as a new orphan row on every single collection.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.freelancer import JobPosting
from app.db.models import (
    CaptureStatus,
    DiscoveryMethod,
    FreelancerProfile,
    PageCapture,
    Project,
    Recommendation,
    utcnow,
)
from app.services.pipeline import to_posting
from app.services.scoring import ScoreResult, score_job

logger = logging.getLogger(__name__)

# The page shows a symbol; every comparison downstream is against an ISO code, and "$" would
# silently fail to match "USD".
_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY", "A$": "AUD", "C$": "CAD"}

_MONEY = re.compile(r"(A\$|C\$|[$£€₹¥])?\s*([\d][\d,]*(?:\.\d+)?)\s*([KkMm])?")
_RELATIVE = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_UNITS = {
    "minute": dt.timedelta(minutes=1),
    "min": dt.timedelta(minutes=1),
    "hour": dt.timedelta(hours=1),
    "hr": dt.timedelta(hours=1),
    "day": dt.timedelta(days=1),
    "week": dt.timedelta(weeks=1),
    "month": dt.timedelta(days=30),
    "year": dt.timedelta(days=365),
}

# Fields the LLM is allowed to fill in when the selectors came back empty. Anything outside this set
# arriving from a model is dropped — an allowlist rather than a blocklist, so a new key in the
# schema cannot quietly start writing to a column nobody reviewed.
LLM_FILLABLE = frozenset(
    {
        "title",
        "description",
        "required_skills",
        "work_type",
        "currency",
        "min_budget",
        "max_budget",
        "posted_at",
        "experience_level",
        "project_length",
        "bid_count",
        "interviewing",
        "invites_sent",
        "unanswered_invites",
        "connects_required",
        "last_viewed_by_client",
        "client_name",
        "client_rating",
        "client_country",
        "client_reviews_count",
        "client_total_spent",
        "client_payment_verified",
    }
)


def normalise_currency(value: Any) -> str | None:
    """A symbol or a code, as an ISO code. ``None`` when there is nothing to read."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return _SYMBOLS.get(text, text.upper() if len(text) <= 4 else None)


def parse_money(text: Any) -> tuple[float | None, float | None, str | None]:
    """``"$500.00 - $1,000.00"`` as ``(500.0, 1000.0, "USD")``.

    A single figure gives the same value for both ends: Upwork's fixed-price cards show one number,
    and reporting it as a minimum with no maximum would read as "unbounded" to the budget filter.
    """
    if not isinstance(text, str) or not text.strip():
        return None, None, None

    amounts: list[float] = []
    currency: str | None = None
    for symbol, digits, suffix in _MONEY.findall(text):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= 1000 if suffix.lower() == "k" else 1_000_000
        amounts.append(value)
        currency = currency or normalise_currency(symbol)

    if not amounts:
        return None, None, currency
    if len(amounts) == 1:
        return amounts[0], amounts[0], currency
    return min(amounts), max(amounts), currency


def parse_relative_time(text: Any, base: dt.datetime) -> dt.datetime | None:
    """``"3 hours ago"`` against the moment the page was read.

    Cards carry no timestamp, only this phrasing, so ``base`` has to come from the client — which is
    why the extension sends ``scraped_at``. Resolving it against the server's clock instead would
    drift by however long the payload sat in a queue.
    """
    if not isinstance(text, str):
        return None
    match = _RELATIVE.search(text)
    if not match:
        return None
    unit = _UNITS.get(match.group(2).lower())
    if unit is None:
        return None
    return base - unit * int(match.group(1))


def parse_work_type(*texts: Any) -> str | None:
    """``"hourly"`` or ``"fixed"`` — lowercase, because that is what scoring compares against.

    ``_budget_floor`` in ``services.scoring`` tests ``budget_type == "hourly"``, so an uppercase
    value here would pick no floor at all and every job would pass the budget filter. The column
    comment says ``FIXED | HOURLY``; the code is the authority.
    """
    joined = " ".join(t for t in texts if isinstance(t, str)).lower()
    if not joined:
        return None
    if "hourly" in joined or "/hr" in joined or "per hour" in joined:
        return "hourly"
    if "fixed" in joined:
        return "fixed"
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        low, _, _ = parse_money(value)
        return low
    return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _as_datetime(value: Any, base: dt.datetime) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    relative = parse_relative_time(value, base)
    if relative:
        return relative
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


@dataclass
class CapturedItem:
    """One scraped row, normalised, with the parts ``JobPosting`` has no room for kept alongside.

    ``JobPosting`` is the platform-neutral shape scoring works with, and it deliberately carries
    only what scoring reads. The client's reputation and the competition counts are stored on the
    project but never scored directly, so they ride here rather than being forced into that shape.
    """

    posting: JobPosting
    client: dict[str, Any]
    bid_information: dict[str, Any]
    # Which of the interesting fields came back empty. What decides whether an LLM call is worth
    # paying for, and what the response reports so a silent no-op is distinguishable from a fill.
    gaps: list[str]

    @property
    def external_id(self) -> str:
        return self.posting.external_id


# Empty is not the same as absent for scoring: a description of "" scores as no keywords matched,
# and a missing budget means "unknown" rather than "free". These are the fields worth an LLM call.
#
# Reported under the column name, checked against the attribute — ``JobPosting`` calls them
# ``budget_min`` and ``budget_type`` while the table calls them ``min_budget`` and ``work_type``.
# One list of names for both read every posting as missing those two, so a page with nothing
# missing still paid for a model call.
_GAP_FIELDS = (
    ("description", "description"),
    ("work_type", "budget_type"),
    ("min_budget", "budget_min"),
    ("posted_at", "posted_at"),
    ("skills", "skills_listed"),
)


def _gaps_of(posting: JobPosting, client: dict[str, Any]) -> list[str]:
    missing = [name for name, attr in _GAP_FIELDS if not getattr(posting, attr, None)]
    if not any(client.get(key) for key in ("client_name", "client_country", "client_rating")):
        missing.append("client")
    return missing


def item_from_card(
    platform: str, card: dict[str, Any], scraped_at: dt.datetime
) -> CapturedItem | None:
    """One row of ``readJobCards()`` output as a normalised item, or ``None`` if unusable.

    No id means no way to dedupe it, and a project that cannot be deduped arrives again on every
    collection. Dropping it is the only honest option — hence the count in the response.
    """
    external_id = (card.get("external_id") or "").strip()
    url = (card.get("url") or "").strip()
    if not external_id:
        return None

    budget_text = card.get("budget")
    low, high, currency = parse_money(budget_text)
    description = card.get("description") or ""

    posting = JobPosting(
        platform=platform,
        external_id=external_id,
        title=(card.get("title") or "").strip(),
        description=description,
        url=url,
        skills_listed=[s for s in (card.get("skills") or []) if isinstance(s, str)],
        # Cards drop the "/hr" when the scraper matches the money, so this is usually unknown here.
        # Left as None rather than guessed: scoring skips the budget floor it has no type for.
        budget_type=parse_work_type(budget_text, card.get("work_type")),
        budget_min=low,
        budget_max=high,
        currency=currency,
        bid_count=_as_int(card.get("proposals")),
        posted_at=_as_datetime(card.get("posted") or card.get("posted_at"), scraped_at),
    )

    client = {
        "client_name": card.get("client_name") or None,
        "client_rating": _as_float(card.get("client_rating")),
        "client_country": card.get("client_country") or None,
        "client_reviews_count": _as_int(card.get("client_reviews_count")),
    }
    bid_information = {
        "bid_count": posting.bid_count,
        # What the card claimed the description was: a preview, not the posting. Recorded so nothing
        # downstream mistakes a truncated blurb for the whole brief.
        "description_complete": bool(card.get("description_complete")),
        "source_page": card.get("page_key"),
    }

    return CapturedItem(
        posting=posting,
        client=client,
        bid_information=bid_information,
        gaps=_gaps_of(posting, client),
    )


def dedupe(items: list[CapturedItem]) -> tuple[list[CapturedItem], int]:
    """One item per ``external_id``, keeping the richest sighting. Returns ``(kept, dropped)``.

    The same posting appears on Best matches and Most recent, and the two cards are not equally
    complete — one may carry a budget the other truncated away. Keeping whichever has more filled in
    means a page's turn in the run order stops deciding what gets stored.
    """
    best: dict[str, CapturedItem] = {}
    dropped = 0
    for item in items:
        existing = best.get(item.external_id)
        if existing is None:
            best[item.external_id] = item
            continue
        dropped += 1
        if len(item.gaps) < len(existing.gaps) or (
            len(item.gaps) == len(existing.gaps)
            and len(item.posting.description or "") > len(existing.posting.description or "")
        ):
            best[item.external_id] = item
    return list(best.values()), dropped


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def merge_llm_fields(item: CapturedItem, fields: dict[str, Any], scraped_at: dt.datetime) -> int:
    """Fill this item's empty fields from an LLM reading. Returns how many were filled.

    Only gaps are touched, and only keys in :data:`LLM_FILLABLE`. A model asked to read a page it
    can see most of will happily restate a title slightly differently; letting that win over the
    text inside the anchor tag would make the same job's title flicker between collections.
    """
    if not isinstance(fields, dict):
        return 0

    filled = 0
    posting = item.posting

    def offer(attr: str, key: str, cast) -> None:
        nonlocal filled
        if key not in LLM_FILLABLE or not _blank(getattr(posting, attr, None)):
            return
        value = cast(fields.get(key))
        if _blank(value):
            return
        setattr(posting, attr, value)
        filled += 1

    offer("title", "title", lambda v: v.strip() if isinstance(v, str) else None)
    offer("description", "description", lambda v: v if isinstance(v, str) else None)
    offer(
        "skills_listed",
        "required_skills",
        lambda v: [s for s in v if isinstance(s, str)] if isinstance(v, list) else None,
    )
    offer("budget_type", "work_type", parse_work_type)
    offer("budget_min", "min_budget", _as_float)
    offer("budget_max", "max_budget", _as_float)
    offer("currency", "currency", normalise_currency)
    offer("bid_count", "bid_count", _as_int)
    offer("posted_at", "posted_at", lambda v: _as_datetime(v, scraped_at))

    for key, cast in (
        ("client_name", lambda v: v if isinstance(v, str) else None),
        ("client_rating", _as_float),
        ("client_country", lambda v: v if isinstance(v, str) else None),
        ("client_reviews_count", _as_int),
    ):
        if key in LLM_FILLABLE and _blank(item.client.get(key)):
            value = cast(fields.get(key))
            if not _blank(value):
                item.client[key] = value
                filled += 1

    # Competition and terms have no column of their own; they live in the project's JSONB alongside
    # the bid count, which is where the scorer and the UI already look for them.
    for key in (
        "interviewing",
        "invites_sent",
        "unanswered_invites",
        "connects_required",
        "experience_level",
        "project_length",
        "last_viewed_by_client",
        "client_total_spent",
        "client_payment_verified",
    ):
        if key in LLM_FILLABLE and _blank(item.bid_information.get(key)):
            value = fields.get(key)
            if not _blank(value):
                item.bid_information[key] = value
                filled += 1

    item.gaps = _gaps_of(posting, item.client)
    return filled


def _normalise_title(text: Any) -> str:
    """Titles as a matching key: case, punctuation and whitespace collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() if isinstance(text, str) else ""


def match_llm_items(
    items: list[CapturedItem], parsed: list[dict[str, Any]], scraped_at: dt.datetime
) -> tuple[int, int]:
    """Attach LLM readings to the scraped items they describe. Returns ``(filled, unmatched)``.

    Matched on the **title**, not the id — a job's id lives in its link's href and the model is only
    ever shown the page's visible text, so it has no id to return. An item the model describes that
    no scraped row matches is dropped, not stored: without a link there is no id, and without an id
    there is nothing to dedupe the next collection against.
    """
    by_title: dict[str, CapturedItem] = {}
    for item in items:
        key = _normalise_title(item.posting.title)
        if key:
            by_title.setdefault(key, item)

    filled = 0
    unmatched = 0
    for fields in parsed:
        key = _normalise_title((fields or {}).get("title"))
        item = by_title.get(key)
        if item is None and key:
            # A listing truncates long titles with an ellipsis; the model reads the same truncation,
            # but a prefix match still lands it on the right card.
            item = next(
                (candidate for title, candidate in by_title.items() if title.startswith(key[:40])),
                None,
            )
        if item is None:
            unmatched += 1
            continue
        filled += merge_llm_fields(item, fields, scraped_at)
    return filled, unmatched


def fingerprint(posting: JobPosting) -> str:
    """Hash of the fields a freelancer would care about changing.

    Deliberately the same shape as ``services.pipeline._fingerprint``: a posting seen first by the
    poller and later by the extension must produce the same hash, or every hand-collected sighting
    would report itself as an edit.
    """
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


# What the extension reports about a page it tried to read, mapped onto the stored status. Anything
# unrecognised is treated as OK rather than as a problem: inventing a "signed out" from a value we
# do not understand would tell someone to fix a session that is working.
SESSION_STATUSES = {"ok": "OK", "signed_out": "SIGNED_OUT", "blocked": "BLOCKED"}


async def record_session(
    session: AsyncSession,
    user_id: Any,
    platform: str,
    page_status: str,
    *,
    detail: str | None = None,
    page_key: str | None = None,
) -> CaptureStatus:
    """Record whether the extension could read this marketplace, for the app to surface.

    One row per user per platform, holding the latest answer — a banner needs one answer, not a log.
    ``since`` survives repeated failures so "signed out for three days" is sayable, and a successful
    read clears the problem rather than appending to it.
    """
    status = SESSION_STATUSES.get((page_status or "ok").lower(), "OK")
    now = utcnow()

    row = await session.scalar(
        select(CaptureStatus).where(
            CaptureStatus.user_id == user_id, CaptureStatus.platform == platform
        )
    )
    if row is None:
        row = CaptureStatus(user_id=user_id, platform=platform, last_checked_at=now)
        session.add(row)

    row.last_checked_at = now
    if status == "OK":
        row.status = "OK"
        row.detail = None
        row.page_key = None
        row.since = None
        row.last_ok_at = now
    else:
        # Kept from the first failure, so the age of the problem is real rather than the age of the
        # last attempt to notice it.
        if row.status != status or row.since is None:
            row.since = now
        row.status = status
        row.detail = detail or None
        row.page_key = page_key

    await session.flush()
    return row


def capture_hash(items: list[dict[str, Any]]) -> str:
    """Fingerprint of a page's rows — what makes a re-sighting recognisable as the same page.

    Over ``items`` only, not the page text: a marketplace changes a footer or a promo strip between
    two visits without any of your contracts having moved, and hashing that would file a new row
    every single time.
    """
    payload = json.dumps(items or [], default=str, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# Which LLM schema reads which page, for the kinds that have no columns of their own. Keyed on the
# extension's page keys.
#
# Message rooms are deliberately absent. They are two-party data — half of it belongs to someone who
# never agreed to any of this — and sending those previews to a model is a further step than reading
# them, taken on purpose only if someone decides to.
PARSE_KIND_BY_PAGE = {
    "contracts": "contracts",
    "reports": "contracts",
    "pph_orders": "contracts",
    "fvr_orders": "contracts",
    "fvr_gigs": "contracts",
    "pph_proposals": "proposals",
    "pph_saved": "jobs",
}


@dataclass
class StoredCapture:
    capture: PageCapture
    created: bool
    # True when this exact page content had already been captured before.
    repeat: bool


async def store_capture(
    session: AsyncSession,
    user_id: Any,
    *,
    platform: str,
    page_key: str,
    page_label: str,
    reads: str,
    page_url: str,
    items: list[dict[str, Any]],
    page_text: str,
    scraped_at: dt.datetime,
    parsed: dict[str, Any] | None = None,
    parsed_model: str | None = None,
) -> StoredCapture:
    """Keep one page whole, for a v2 that will decide what to make of it.

    Accumulating means the rows survive the person navigating away, which is the only window they
    exist in. It does not mean storing the same page forty times: an unchanged re-collection bumps
    ``times_seen`` and moves ``last_seen_at``, and only a genuine change writes a new row.

    An LLM reading is stored *beside* the raw rows, never instead of them. A model's interpretation
    is not evidence, and a later, better prompt should get to re-read the original.
    """
    content = capture_hash(items)
    existing = await session.scalar(
        select(PageCapture).where(
            PageCapture.user_id == user_id,
            PageCapture.platform == platform,
            PageCapture.page_key == page_key,
            PageCapture.content_hash == content,
        )
    )

    if existing is not None:
        existing.times_seen = (existing.times_seen or 1) + 1
        existing.last_seen_at = utcnow()
        existing.scraped_at = scraped_at
        # A page whose rows are unchanged can still be read by a model for the first time, so a
        # reading arriving now is worth keeping. An existing one is never overwritten with null.
        if parsed is not None:
            existing.parsed = parsed
            existing.parsed_model = parsed_model
        if page_text:
            existing.page_text = page_text
        await session.flush()
        return StoredCapture(capture=existing, created=False, repeat=True)

    capture = PageCapture(
        user_id=user_id,
        platform=platform,
        page_key=page_key,
        page_label=page_label or None,
        reads=reads,
        page_url=page_url or None,
        items=items or [],
        item_count=len(items or []),
        page_text=page_text or None,
        parsed=parsed,
        parsed_model=parsed_model,
        content_hash=content,
        times_seen=1,
        scraped_at=scraped_at,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(capture)
    await session.flush()
    return StoredCapture(capture=capture, created=True, repeat=False)


@dataclass
class StoredPosting:
    project: Project
    recommendation: Recommendation
    created: bool
    content_changed: bool
    result: ScoreResult


async def store_posting(
    session: AsyncSession,
    profile: FreelancerProfile,
    posting: JobPosting,
    *,
    client: dict[str, Any] | None = None,
    bid_information: dict[str, Any] | None = None,
    discovery_method: str = DiscoveryMethod.PASTE_IN,
) -> StoredPosting:
    """Upsert one project on ``(platform, external_id)``, then score it for this profile.

    Does not commit — the caller decides the transaction boundary, which is what lets a page of
    sixty jobs be one round trip to the database rather than sixty.

    Scored inline rather than left for the next cycle because the person is looking at the job right
    now; a verdict half an hour later is a verdict they never see.
    """
    project = await session.scalar(
        select(Project).where(
            Project.platform == posting.platform, Project.external_id == posting.external_id
        )
    )
    created = project is None
    if project is None:
        project = Project(
            platform=posting.platform,
            external_id=posting.external_id,
            discovery_method=discovery_method,
        )
        session.add(project)

    incoming_hash = fingerprint(posting)
    content_changed = project.content_hash is not None and project.content_hash != incoming_hash

    project.title = posting.title
    project.description = posting.description
    project.project_url = posting.url
    project.required_skills = posting.skills_listed or []
    project.work_type = posting.budget_type
    project.min_budget = posting.budget_min
    project.max_budget = posting.budget_max
    project.currency = posting.currency
    # Merged rather than replaced: a listing page knows the bid count, the job's own page knows the
    # interview count, and whichever arrives second must not blank what the first one learned.
    project.bid_information = {
        **(project.bid_information or {}),
        **{k: v for k, v in (bid_information or {}).items() if v is not None},
        "bid_count": posting.bid_count
        if posting.bid_count is not None
        else (project.bid_information or {}).get("bid_count"),
    }
    if posting.posted_at:
        project.posted_at = posting.posted_at

    for column, value in (client or {}).items():
        # A partial scrape must not blank a value an earlier, better one found.
        if value is not None and hasattr(project, column):
            setattr(project, column, value)

    project.content_hash = incoming_hash
    project.updated_at = utcnow()
    if content_changed:
        project.last_changed_at = utcnow()

    # The recommendation's FK needs the project's id, which only exists after a flush.
    await session.flush()

    result = score_job(to_posting(project), profile)

    recommendation = await session.scalar(
        select(Recommendation).where(
            Recommendation.freelancer_id == profile.id, Recommendation.project_id == project.id
        )
    )
    if recommendation is None:
        recommendation = Recommendation(freelancer_id=profile.id, project_id=project.id)
        session.add(recommendation)

    recommendation.score = result.score
    recommendation.reasons = result.reasons
    recommendation.is_hard_rejected = result.rejected
    recommendation.rejection_reason = result.rejection_reason
    recommendation.updated_at = utcnow()
    await session.flush()

    return StoredPosting(
        project=project,
        recommendation=recommendation,
        created=created,
        content_changed=content_changed,
        result=result,
    )
