"""The marketplace-connector contract and its errors.

One :class:`Connector` interface, one factory (:func:`app.connectors.factory.create_connector`), so
the rest of the app never names a concrete client. The only implementation today is
:class:`app.connectors.freelancer.FreelancerClient`; when Upwork's OAuth lands, its client
implements this same Protocol and registers in the factory — no caller changes.

``JobPosting`` (the platform-neutral normalised posting) still lives in ``freelancer`` for now, and
is referenced here only for typing, so this module imports nothing at runtime and can't cycle.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.connectors.freelancer import JobPosting


class ConnectorKind(StrEnum):
    """How a platform's account is connected — which decides what it can do.

    ``OAUTH``: authenticated API access. There's a live :class:`Connector` client, discovery can be
    polled, and bids can be placed (Freelancer).
    ``EXTENSION``: no API. Data arrives via the browser extension as paste-in — a read-only mirror
    with no tokens, so it can never place a bid (Upwork). There is no client to build for these.
    """

    OAUTH = "OAUTH"
    EXTENSION = "EXTENSION"


class ConnectorError(RuntimeError):
    """Base class for every connector failure. A caller can catch this to stay platform-neutral;
    a concrete client (e.g. ``FreelancerAPIError``) subclasses it."""


class UnsupportedPlatformError(ConnectorError):
    """Raised by the factory for a platform with no registered connector (e.g. Upwork, for now)."""


@runtime_checkable
class Connector(Protocol):
    """What a marketplace connector must provide. Structural — a client satisfies it by shape, so
    the concrete class needn't import or inherit anything here.

    Methods a given platform can't support yet should raise ``ConnectorError`` rather than silently
    returning empty, so the gap is visible.
    """

    async def fetch_self(self) -> dict[str, Any]:
        """The authenticated account's own record: identity, reputation, public profile."""
        ...

    async def fetch_self_id(self) -> int:
        """The authenticated account's own id (needed as the bidder id when placing a bid)."""
        ...

    async def fetch_portfolio(self, user_id: int) -> list[dict[str, Any]]:
        """The account's portfolio items, normalised for the profile."""
        ...

    async def submit_bid(
        self,
        project_id: int,
        bidder_id: int,
        amount: float,
        period_days: int,
        description: str,
        milestone_percentage: int = 100,
    ) -> str:
        """Place a real bid; return the platform's bid id."""
        ...

    async def fetch_my_bids(self, bidder_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """The account's own bids, for pulling outcomes back."""
        ...

    async def fetch_projects_by_id(self, project_ids: list[int]) -> dict[str, JobPosting]:
        """Fetch specific postings by id (e.g. the project a synced bid points at)."""
        ...

    async def fetch_skill_catalogue(self) -> dict[str, int]:
        """The platform's skill/tag vocabulary, name → id, for the discovery filter."""
        ...

    async def resolve_skill_ids(self, names: list[str]) -> tuple[list[int], list[str]]:
        """Map skill names onto the platform's tag ids; return (resolved ids, unresolved names)."""
        ...

    async def search_active_projects(
        self,
        query: str | None = None,
        skill_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
        from_time: int | None = None,
        project_types: tuple[str, ...] = ("fixed", "hourly"),
        max_retries: int = 3,
    ) -> list[JobPosting]:
        """One page of active postings, newest first."""
        ...
