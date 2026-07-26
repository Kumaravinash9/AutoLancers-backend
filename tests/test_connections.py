"""Connection soft delete: the destructive step never runs inline, and tokens don't linger."""

from __future__ import annotations

from app.db.models import PlatformConnection
from app.services.connections import disconnect_connection


class TestDisconnect:
    def _connected(self) -> PlatformConnection:
        return PlatformConnection(
            status="ACTIVE",
            is_selected=True,
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
        )

    def test_marks_disconnected_and_keeps_the_row(self):
        c = self._connected()
        disconnect_connection(c)
        assert c.disconnected_at is not None
        assert c.status == "DISCONNECTED"

    def test_scrubs_both_tokens_immediately(self):
        c = self._connected()
        disconnect_connection(c)
        # We have no business holding an access or refresh token for a disconnected account.
        assert c.access_token_encrypted is None
        assert c.refresh_token_encrypted is None

    def test_drops_selection(self):
        c = self._connected()
        disconnect_connection(c)
        assert c.is_selected is False
