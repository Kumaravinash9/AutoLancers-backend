"""The connector factory: decide the client from a platform string.

Callers ask for ``create_connector(connection.platform, access_token=...)`` and get back something
satisfying :class:`~app.connectors.base.Connector`, never a named concrete class. Adding Upwork
later is a one-line registration here plus its client module — no caller touches a class name.

Each platform is also tagged with a :class:`ConnectorKind`. OAuth platforms have an API client here;
extension platforms (Upwork) have none — their data arrives via ``routes.ingest`` — so asking the
factory to build one is a clear error rather than a silent Freelancer fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.connectors.base import Connector, ConnectorKind, UnsupportedPlatformError
from app.connectors.freelancer import FreelancerClient


@dataclass(frozen=True)
class _Platform:
    kind: ConnectorKind
    # How to build the API client — None for extension platforms, which have no client.
    build: Callable[..., Connector] | None


# The one place platforms are registered. New platforms add a row here and nothing else.
_REGISTRY: dict[str, _Platform] = {
    "freelancer": _Platform(ConnectorKind.OAUTH, FreelancerClient),
    # When Upwork's browser-extension capture is wired end to end, register it as extension-only:
    # "upwork": _Platform(ConnectorKind.EXTENSION, None),
}


def supported_platforms() -> list[str]:
    return sorted(_REGISTRY)


def _lookup(platform: str) -> _Platform:
    entry = _REGISTRY.get((platform or "").lower())
    if entry is None:
        raise UnsupportedPlatformError(
            f"Unknown platform {platform!r} (supported: {', '.join(supported_platforms())})"
        )
    return entry


def connector_kind(platform: str) -> ConnectorKind:
    """Whether ``platform`` connects by OAuth or by the browser extension."""
    return _lookup(platform).kind


def create_connector(
    platform: str, *, access_token: str | None = None, timeout: float = 30.0
) -> Connector:
    """Build the API client for ``platform``.

    Raises :class:`UnsupportedPlatformError` for an unknown platform, or for an extension-only one
    (Upwork) that has no client — a wrong or absent marketplace is a louder bug than a silent
    default to Freelancer.
    """
    entry = _lookup(platform)
    if entry.build is None:
        raise UnsupportedPlatformError(
            f"{platform!r} is {entry.kind} — it has no API client; its data arrives via the "
            "browser extension (see routes.ingest)."
        )
    return entry.build(access_token=access_token, timeout=timeout)
