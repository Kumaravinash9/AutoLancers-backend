"""Marketplace connectors: one contract, one factory, a client per platform."""

from app.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorKind,
    UnsupportedPlatformError,
)
from app.connectors.factory import (
    connector_kind,
    create_connector,
    supported_platforms,
)

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorKind",
    "UnsupportedPlatformError",
    "connector_kind",
    "create_connector",
    "supported_platforms",
]
