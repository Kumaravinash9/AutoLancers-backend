"""Placing a real bid.

Deliberately isolated from the pipeline. Nothing on the polling path imports this module — the
only caller is the per-job endpoint, which requires an explicit confirmation from the UI.

Four independent gates have to be open before a bid leaves the machine:

1. ``ENABLE_BIDDING`` is on in config
2. the caller passed ``confirm=True``
3. the stored connection actually carries the bid scope
4. this recommendation has not already been bid on

Any one of them closed means no request is made. They are separate on purpose: a config flag
alone should not be able to turn a review tool into an auto-bidder.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.freelancer_oauth import OAuthError, get_valid_access_token, has_bid_scope
from app.config import get_settings
from app.connectors.freelancer import FreelancerAPIError, FreelancerClient
from app.db.models import (
    PlatformConnection,
    ProposalStatus,
    Recommendation,
    RecommendationStatus,
    SubmittedVia,
    utcnow,
)

logger = logging.getLogger(__name__)

# A proposal that would arrive truncated is worse than none.
MIN_DESCRIPTION_CHARS = 40


class BiddingError(RuntimeError):
    """Raised when a bid cannot be placed. The message is shown to the user verbatim."""


@dataclass
class BidAvailability:
    """Why the Submit button is or isn't usable, so the UI can explain rather than just disable."""

    available: bool
    reason: str


def check_availability(connection: PlatformConnection | None) -> BidAvailability:
    settings = get_settings()

    if not settings.enable_bidding:
        return BidAvailability(False, "Bidding is switched off in this install (ENABLE_BIDDING).")
    if connection is None:
        return BidAvailability(False, "Connect your Freelancer account first.")
    if not has_bid_scope(connection.scope):
        return BidAvailability(
            False,
            "Your connection is read-only. Freelancer must approve the bid permission for this "
            "app, then reconnect to grant it.",
        )
    return BidAvailability(True, "Ready to submit.")


async def submit_bid_for_recommendation(
    session: AsyncSession,
    user_id: uuid.UUID,
    recommendation: Recommendation,
    amount: float,
    period_days: int,
    confirm: bool,
    milestone_percentage: int = 100,
) -> str:
    """Place a bid and record it. Returns Freelancer's bid id."""
    settings = get_settings()

    if not settings.enable_bidding:
        raise BiddingError("Bidding is switched off in this install (ENABLE_BIDDING).")
    if not confirm:
        raise BiddingError("Bid not confirmed — nothing was submitted.")

    proposal = recommendation.proposal
    if proposal is not None and proposal.external_bid_id:
        raise BiddingError(f"Already bid on this project (bid {proposal.external_bid_id}).")
    if recommendation.is_hard_rejected:
        raise BiddingError("This job was filtered out. Re-score or adjust your profile first.")

    # Parenthesised deliberately: without them the `or ""` binds to the else branch only, so a
    # proposal row holding NULL text slips through and blows up on .strip().
    description = ((proposal.proposal_text if proposal else None) or "").strip()
    if len(description) < MIN_DESCRIPTION_CHARS:
        raise BiddingError("Write or generate a proposal before bidding.")
    if amount <= 0:
        raise BiddingError("Bid amount must be greater than zero.")
    if period_days <= 0:
        raise BiddingError("Delivery period must be at least one day.")

    # The scope gate belongs here, not only in the availability endpoint the UI reads. Enforcing
    # it in the read path alone meant a direct API call reached Freelancer holding a read-only
    # token — and a remote rejection is not a substitute for our own guarantee.
    connection = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id, PlatformConnection.platform == "freelancer"
        )
    )
    availability = check_availability(connection)
    if not availability.available:
        raise BiddingError(availability.reason)

    try:
        token = await get_valid_access_token(session, user_id)
    except OAuthError as exc:
        raise BiddingError(str(exc)) from exc

    client = FreelancerClient(access_token=token)

    try:
        bidder_id = await client.fetch_self_id()
        bid_id = await client.submit_bid(
            project_id=int(recommendation.project.external_id),
            bidder_id=bidder_id,
            amount=amount,
            period_days=period_days,
            description=description,
            milestone_percentage=milestone_percentage,
        )
    except (FreelancerAPIError, ValueError, TypeError) as exc:
        raise BiddingError(str(exc)) from exc

    proposal.external_bid_id = bid_id
    proposal.bid_amount = amount
    proposal.estimated_days = period_days
    proposal.submitted_at = utcnow()
    proposal.submitted_via = SubmittedVia.API
    proposal.status = ProposalStatus.SUBMITTED
    recommendation.status = RecommendationStatus.APPLIED
    await session.commit()

    logger.info(
        "Bid %s placed on project %s for %.2f",
        bid_id,
        recommendation.project.external_id,
        amount,
    )
    return bid_id
