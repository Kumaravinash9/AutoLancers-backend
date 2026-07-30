"""Postings and profiles captured by the browser extension.

Upwork has no usable public API for discovery, so nothing here is polled. Every row arrives
because a signed-in person opened a page and clicked — which is why the stored
``discovery_method`` is ``PASTE_IN`` and not ``API_POLL``. Keeping those distinguishable is what
lets anyone later ask "where did this come from?" and get a true answer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CapturedItemResult,
    CapturedPage,
    CapturedPageResult,
    CapturedPosting,
    CapturedProfile,
    CaptureResult,
    CaptureStatusOut,
    PageParseIn,
    PageParseOut,
)
from app.auth.accounts import current_user, optional_user
from app.config import get_settings
from app.connectors import ConnectorKind
from app.connectors.freelancer import JobPosting
from app.db.models import (
    CaptureStatus,
    PlatformConnection,
    User,
    utcnow,
)
from app.db.session import get_session
from app.services.capture import (
    PARSE_KIND_BY_PAGE,
    CapturedItem,
    dedupe,
    item_from_card,
    match_llm_items,
    merge_llm_fields,
    record_session,
    store_capture,
    store_posting,
)
from app.services.page_parse import PageParseError, parse_page
from app.services.users import (
    get_or_create_profile,
    get_or_create_profile_for_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/posting", response_model=CaptureResult)
async def capture_posting(
    payload: CapturedPosting,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CaptureResult:
    """Store a posting read off a page, score it, and hand the score straight back.

    Scored inline rather than left for the next cycle because the person is looking at the job
    right now — a verdict that arrives half an hour later is a verdict they will never see.

    Shares its upsert with the collection endpoint below, via ``services.capture.store_posting``, so
    the same job stores identically whether it arrived from its own page or from a listing.
    """
    profile = await get_or_create_profile(session, user.id)

    # Recorded here too, not only on the collection path. A single job read while signed out is the
    # same news as a whole run read while signed out, and reporting it from one path only meant the
    # app's banner stayed silent for the other two — and a status recorded only sometimes is worse
    # than one never recorded, because its silence reads as "fine".
    await record_session(session, user.id, payload.platform, "ok", page_key="job_page")

    scraped_at = payload.posted_at or utcnow()
    item = CapturedItem(
        posting=JobPosting(
            platform=payload.platform,
            external_id=payload.external_id,
            title=payload.title,
            description=payload.description,
            url=payload.url,
            skills_listed=payload.skills,
            budget_type=payload.work_type,
            budget_min=payload.budget_min,
            budget_max=payload.budget_max,
            currency=payload.currency,
            bid_count=payload.proposal_count,
            posted_at=payload.posted_at,
        ),
        client=payload.client.columns() if payload.client else {},
        bid_information={
            "experience_level": payload.experience_level,
            "project_length": payload.project_length,
            "client_total_spent": payload.client.total_spent if payload.client else None,
            "client_total_hires": payload.client.total_hires if payload.client else None,
            "client_payment_verified": payload.client.payment_verified if payload.client else None,
            "client_member_since": payload.client.member_since if payload.client else None,
            # A job's own page carries the whole brief, not the listing's preview.
            "description_complete": bool(payload.description),
            "source_page": "job_page",
        },
        gaps=[],
    )

    # Read the page with the LLM when asked, filling only what the selectors missed. Routed through
    # the one page-reading entry point (``parse_page``); a model failure costs the enrichment, not
    # the capture — the posting is still stored and scored, and the response says the read failed.
    llm_used = False
    llm_model: str | None = None
    llm_filled = 0
    llm_error: str | None = None
    if payload.is_llm_required and payload.page_text:
        try:
            parsed = await parse_page("job", payload.page_text)
            llm_used = True
            llm_model = parsed.get("model")
            llm_filled = merge_llm_fields(item, parsed.get("fields") or {}, scraped_at)
        except PageParseError as exc:
            llm_error = str(exc)

    stored = await store_posting(
        session,
        profile,
        item.posting,
        client=item.client or None,
        bid_information=item.bid_information,
    )
    await session.commit()

    return CaptureResult(
        project_id=stored.project.id,
        recommendation_id=stored.recommendation.id,
        created=stored.created,
        score=stored.result.score,
        rejected=stored.result.rejected,
        rejection_reason=stored.result.rejection_reason,
        reasons=stored.result.reasons,
        llm_used=llm_used,
        llm_model=llm_model,
        llm_fields_filled=llm_filled,
        llm_error=llm_error,
    )


@router.post("/collection", response_model=CapturedPageResult)
async def capture_collection(
    payload: CapturedPage,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CapturedPageResult:
    """Store one whole page the extension just finished reading.

    Every job-listing page maps onto the same table. "Best matches" and "Most recent" are not two
    kinds of thing — they are two places the same marketplace shows the same postings, so both land
    in ``projects`` deduped on ``(platform, external_id)``, and a job on both pages is one row
    scored once. Which page it was seen on is recorded in ``bid_information.source_page`` rather
    than in the identity of the row.

    Pages with no modelled home — contracts, proposals, orders, message rooms — are accumulated
    whole instead (see :func:`_accumulate`): those rows only exist while someone is on the page.

    One request per page, and one database transaction per request: sixty jobs is one round trip,
    not sixty. A page that fails leaves the pages already sent alone.
    """
    scraped_at = payload.scraped_at or utcnow()

    # Recorded on every page, success or wall — the app needs to know it is being read as much as it
    # needs to know it is not, and a status that only ever reports failure can never be cleared.
    recorded = await record_session(
        session,
        user.id,
        payload.freelance_platform,
        payload.page_status,
        detail=payload.status_detail,
        page_key=payload.page_key,
    )

    if payload.page_status != "ok":
        # Nothing to store: a login page holds no jobs, and its text is not your contracts. The
        # point of the request was the status, and that is already recorded.
        await session.commit()
        return CapturedPageResult(
            freelance_platform=payload.freelance_platform,
            page_key=payload.page_key,
            reads=payload.reads,
            received=0,
            stored=0,
            created=0,
            updated=0,
            session_status=recorded.status,
            note=payload.status_detail
            or (
                "Not signed in to that marketplace."
                if payload.page_status == "signed_out"
                else "The marketplace served a challenge page."
            ),
        )

    if payload.reads != "jobs":
        return await _accumulate(payload, user, session, scraped_at, recorded.status)

    profile = await get_or_create_profile(session, user.id)

    items = []
    skipped_no_id = 0
    for card in payload.items:
        card = {**card, "page_key": payload.page_key}
        item = item_from_card(payload.freelance_platform, card, scraped_at)
        if item is None:
            # No id in the link means nothing to dedupe a later sighting against, so this row would
            # arrive again as a new project on every collection. Counted, not stored.
            skipped_no_id += 1
            continue
        items.append(item)

    items, duplicates = dedupe(items)

    llm_used = False
    llm_model: str | None = None
    llm_filled = 0
    llm_unmatched = 0
    llm_error: str | None = None

    # Only pay for the model when asked *and* when something is actually missing. A page whose
    # selectors filled every field has nothing to gain from a reading, and charging for it anyway is
    # how a per-page cost becomes invisible.
    if payload.is_llm_required and payload.page_text and any(item.gaps for item in items):
        try:
            parsed = await parse_page("jobs", payload.page_text)
            llm_used = True
            llm_model = parsed.get("model")
            llm_filled, llm_unmatched = match_llm_items(
                items, parsed.get("fields", {}).get("items") or [], scraped_at
            )
        except PageParseError as exc:
            # The scraped rows are still worth storing. A model that was unreachable must not cost
            # the page — it costs the enrichment, and the response says which.
            llm_error = str(exc)

    results: list[CapturedItemResult] = []
    created = 0
    for item in items:
        stored = await store_posting(
            session,
            profile,
            item.posting,
            client=item.client,
            bid_information=item.bid_information,
        )
        created += 1 if stored.created else 0
        results.append(
            CapturedItemResult(
                external_id=item.external_id,
                title=item.posting.title,
                project_id=stored.project.id,
                created=stored.created,
                score=stored.result.score,
                rejected=stored.result.rejected,
                rejection_reason=stored.result.rejection_reason,
            )
        )

    await session.commit()

    return CapturedPageResult(
        freelance_platform=payload.freelance_platform,
        page_key=payload.page_key,
        reads=payload.reads,
        received=len(payload.items),
        stored=len(results),
        created=created,
        updated=len(results) - created,
        skipped_no_id=skipped_no_id,
        duplicates=duplicates,
        llm_used=llm_used,
        llm_model=llm_model,
        llm_fields_filled=llm_filled,
        llm_unmatched=llm_unmatched,
        llm_error=llm_error,
        session_status=recorded.status,
    )


async def _accumulate(
    payload: CapturedPage,
    user: User,
    session: AsyncSession,
    scraped_at,
    session_status: str = "OK",
) -> CapturedPageResult:
    """Keep a page that has no modelled home yet, whole, for v2 to make sense of.

    Contracts, proposals, orders and room lists arrive as loose rows — the reader assumes nothing
    about their columns, because every marketplace lays them out differently. Storing them verbatim
    costs one row and buys the history: the rows exist only while a person is on the page, so a
    decision deferred without accumulating is a month of data thrown away.

    An unchanged page bumps ``times_seen`` rather than filing another copy. A changed one is a new
    row, because that difference is the whole value of keeping them.
    """
    parsed: dict | None = None
    parsed_model: str | None = None
    llm_error: str | None = None

    kind = PARSE_KIND_BY_PAGE.get(payload.page_key)
    if payload.is_llm_required and payload.page_text and kind:
        try:
            reading = await parse_page(kind, payload.page_text)
            parsed = reading.get("fields")
            parsed_model = reading.get("model")
        except PageParseError as exc:
            # The raw rows are the thing worth keeping; a model that was unreachable must not cost
            # them. Recorded so a null `parsed` is distinguishable from one nobody paid for.
            llm_error = str(exc)

    stored = await store_capture(
        session,
        user.id,
        platform=payload.freelance_platform,
        page_key=payload.page_key,
        page_label=payload.page_label,
        reads=payload.reads,
        page_url=payload.page_url,
        items=payload.items,
        page_text=payload.page_text,
        scraped_at=scraped_at,
        parsed=parsed,
        parsed_model=parsed_model,
    )
    await session.commit()

    read_items = len(parsed.get("items") or []) if isinstance(parsed, dict) else 0
    return CapturedPageResult(
        freelance_platform=payload.freelance_platform,
        page_key=payload.page_key,
        reads=payload.reads,
        received=len(payload.items),
        # The rows are stored, as rows. Reporting them as `stored` is the truth: they are in the
        # database and queryable, just not yet modelled into contracts or proposals.
        stored=len(payload.items),
        created=len(payload.items) if stored.created else 0,
        updated=0 if stored.created else len(payload.items),
        llm_used=parsed is not None,
        llm_model=parsed_model,
        llm_fields_filled=read_items,
        llm_error=llm_error,
        capture_id=stored.capture.id,
        session_status=session_status,
        note=(
            f"Kept as a raw capture "
            f"({'new' if stored.created else f'seen {stored.capture.times_seen}×'})"
            + (f", {read_items} rows read by the AI" if read_items else "")
            + f". '{payload.page_key}' has no modelled table yet — v2 reads these back."
        ),
    )


def _fill_profile_gaps(payload: CapturedProfile, fields: dict) -> None:
    """Fill only the blank fields on a captured profile from an LLM reading.

    Selectors win where they got a value; the model supplies what they missed — the same
    fill-gaps policy the job paths use, so a reading can't override a value already scraped.
    """

    def text(key: str) -> str | None:
        v = fields.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def num(key: str) -> float | None:
        v = fields.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    payload.display_name = payload.display_name or text("display_name")
    payload.tagline = payload.tagline or text("tagline")
    payload.summary = payload.summary or text("summary")
    payload.currency = payload.currency or text("currency")
    payload.country = payload.country or text("country")
    if not payload.skills:
        payload.skills = [x for x in (fields.get("skills") or []) if isinstance(x, str)]
    if payload.hourly_rate is None:
        payload.hourly_rate = num("hourly_rate")
    if payload.rating is None:
        payload.rating = num("rating")
    if payload.total_reviews is None:
        reviews = num("total_reviews")
        payload.total_reviews = int(reviews) if reviews is not None else None


@router.post("/profile", response_model=dict)
async def capture_profile(
    payload: CapturedProfile,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mirror **your own** marketplace profile onto its profile row.

    The connection is only the account's identity (a handle, and — for Freelancer — its tokens).
    The public profile it advertises lives on the 1:1 :class:`FreelancerProfile`, which is where the
    rest of the app reads it. An Upwork account carries no tokens, so nothing here can place a bid.

    The ``is_own`` gate matters more than it looks. No URL pattern can tell your Upwork profile
    from anyone else's — ``/freelancers/~01…`` matches every freelancer on the site — and this
    endpoint overwrites your display name, tagline, rate and skills with whatever arrives. Before
    the gate, opening a competitor's profile and clicking Send replaced your profile with theirs.

    So the extension decides by comparing the account id in the URL against the id the marketplace's
    own header links to, and reports the answer. ``None`` means it could not tell, which is refused
    like a ``False``: "probably yours" is not a good enough reason to overwrite the row that every
    score in the app is computed from.
    """
    # Before the guard: reaching a profile page at all proves the session works, and that is worth
    # recording even when the profile turns out not to be yours.
    await record_session(session, user.id, payload.platform, "ok", page_key="profile_page")

    if payload.is_own is not True:
        raise HTTPException(
            status_code=409,
            detail=(
                "That profile isn't confirmed as yours, so it wasn't stored — storing it would "
                "overwrite your own. Open your profile from the site's account menu and try again."
                if payload.is_own is False
                else "Couldn't confirm that profile is yours, so it wasn't stored. Open it from "
                "the site's own account menu, which links only to your profile."
            ),
        )

    # Read the page with the LLM when asked, filling only the fields the selectors missed. Same one
    # entry point (``parse_page``) the job paths use; a model failure costs the enrichment, not the
    # capture — the fields the selectors did get are still stored.
    llm_used = False
    llm_model: str | None = None
    llm_error: str | None = None
    if payload.is_llm_required and payload.page_text:
        try:
            parsed = await parse_page("profile", payload.page_text)
            fields = parsed.get("fields") or {}
            llm_used = True
            llm_model = parsed.get("model")
            _fill_profile_gaps(payload, fields)
        except PageParseError as exc:
            llm_error = str(exc)

    # Key on the account's stable id, not its handle: a rename would otherwise spawn a duplicate
    # connection, and this is the same id OAuth stores — so an account connected both ways
    # reconciles to one row. Falls back to the username for older extensions that don't send an id.
    account_id = payload.account_id or payload.username
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == payload.platform,
            PlatformConnection.platform_user_id == account_id,
        )
    )
    if connection is None:
        connection = PlatformConnection(
            user_id=user.id,
            platform=payload.platform,
            platform_user_id=account_id,
            platform_username=payload.username,
            status="ACTIVE",
            # Captured by the browser extension, not OAuth — a read-only mirror with no tokens.
            kind=ConnectorKind.EXTENSION,
            # No OAuth happened, so there is no scope to record. Saying "read only" here would
            # imply a token exists.
            scope=None,
        )
        session.add(connection)
        await session.flush()  # needs an id before a profile can link to it
    else:
        # Keep the display handle current — it's the mutable field; the id is what we matched on.
        connection.platform_username = payload.username

    profile = await get_or_create_profile_for_connection(session, connection)

    # display_name is non-null on the profile — only overwrite it when the capture actually carried
    # one, so a partial scrape can't blank a good name.
    if payload.display_name:
        profile.display_name = payload.display_name
    profile.tagline = payload.tagline
    profile.summary = payload.summary
    profile.account_skills = payload.skills
    profile.hourly_rate = payload.hourly_rate
    if payload.country:
        profile.country = payload.country
    profile.avatar_url = payload.avatar_url
    profile.rating = payload.rating
    profile.total_reviews = payload.total_reviews
    # Adopt the account currency only while no budget floor has been set — see store_token.
    if payload.currency and not profile.rate_min and not profile.fixed_project_min:
        profile.currency = payload.currency
    profile.last_synced_at = utcnow()
    await session.commit()

    return {
        "connection_id": str(connection.id),
        "profile_id": str(profile.id),
        "skills": len(payload.skills),
        "llm_used": llm_used,
        "llm_model": llm_model,
        "llm_error": llm_error,
    }


@router.get("/status", response_model=list[CaptureStatusOut])
async def capture_status(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CaptureStatus]:
    """Whether the extension can currently read each marketplace — one row per platform.

    What the app renders a banner from. The failure this reports has no other symptom: signed out
    of Upwork, the extension reads a login page, finds no jobs, and files an honest-looking zero,
    while the board keeps showing yesterday's scores as though they were current. Nothing errors, so
    nothing else in the system can tell anyone.

    Only platforms the extension has actually tried appear. An empty list means it has never
    reported in — which the UI must not call "signed out", because nobody has looked.
    """
    rows = await session.execute(
        select(CaptureStatus)
        .where(CaptureStatus.user_id == user.id)
        # Problems first, then most recently checked — the order a banner wants to read them in.
        .order_by(
            (CaptureStatus.status == "OK").asc(), CaptureStatus.last_checked_at.desc()
        )
    )
    return list(rows.scalars().all())


@router.post("/parse", response_model=PageParseOut)
async def parse(
    payload: PageParseIn,
    user: User | None = Depends(optional_user),
) -> PageParseOut:
    """Read a page with an LLM when the selectors come back empty. Writes nothing.

    Deliberately a separate endpoint rather than an automatic fallback inside capture: this costs
    money and seconds per call, so it happens because someone chose it, not because a selector
    quietly broke. It is the extension's "Read with AI" button — you see what the model made of the
    page before deciding whether to store anything.

    Authentication is optional while ``PARSE_REQUIRES_AUTH`` is false, so that button works before
    anyone has issued themselves a token. That is a testing convenience and nothing more: this calls
    a paid model, so left open on a reachable host it is an unmetered proxy to your LLM quota. Turn
    it on before exposing this service.
    """
    settings = get_settings()
    if settings.parse_requires_auth and user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if user is None:
        # Logged per request, not once at startup: the line that matters is the one beside the
        # request it allowed, in whatever log someone is actually reading.
        logger.warning(
            "Anonymous /ingest/parse — allowed because PARSE_REQUIRES_AUTH is false. "
            "This endpoint calls a paid model; set it to true before exposing this service."
        )
    try:
        result = await parse_page(payload.kind, payload.text)
    except PageParseError as exc:
        # 502, not 500: the failure is upstream at the model, and the client can sensibly retry.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PageParseOut(**result)
