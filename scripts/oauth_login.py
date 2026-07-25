#!/usr/bin/env python
"""One-time Freelancer.com authorization.

Opens your browser, captures the callback on a temporary local listener, exchanges the code for a
token, and stores it encrypted in Postgres. Refresh is automatic from then on.

    uv run python scripts/oauth_login.py
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.freelancer_oauth import (  # noqa: E402
    OAuthError,
    build_authorize_url,
    exchange_code,
    store_token,
)
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.users import get_or_create_default_user  # noqa: E402

_result: dict[str, str] = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result["code"] = params.get("code", [""])[0]
        _result["state"] = params.get("state", [""])[0]
        _result["error"] = params.get("error", [""])[0]

        body = (
            b"<h2>Freelancer account connected.</h2><p>You can close this tab.</p>"
            if _result["code"]
            else b"<h2>Authorization failed.</h2><p>Check the terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep the terminal clean


def _listen(port: int) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server


async def main() -> int:
    settings = get_settings()

    missing = [
        name
        for name, value in (
            ("FREELANCER_CLIENT_ID", settings.freelancer_client_id),
            ("FREELANCER_CLIENT_SECRET", settings.freelancer_client_secret),
            ("TOKEN_ENCRYPTION_KEY", settings.token_encryption_key),
        )
        if not value
    ]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}")
        return 1

    redirect = urllib.parse.urlparse(settings.freelancer_redirect_uri)
    port = redirect.port or (443 if redirect.scheme == "https" else 80)
    if redirect.hostname not in ("localhost", "127.0.0.1"):
        print(
            f"FREELANCER_REDIRECT_URI points at {redirect.hostname}, so this script cannot catch "
            "the callback. Point it at localhost for the CLI flow."
        )
        return 1

    try:
        server = _listen(port)
    except OSError as exc:
        print(f"Could not listen on port {port}: {exc}")
        return 1

    url, state = build_authorize_url()
    print(f"Opening browser for authorization...\n\nIf it doesn't open:\n  {url}\n")
    webbrowser.open(url)

    print(f"Waiting for the callback on port {port}...")
    for _ in range(300):  # ~5 minutes
        if _result:
            break
        await asyncio.sleep(1)
    else:
        print("Timed out waiting for the callback.")
        server.server_close()
        return 1

    server.server_close()

    if _result.get("error"):
        print(f"Authorization denied: {_result['error']}")
        return 1
    if _result.get("state") != state:
        print("State mismatch — aborting. Try again in a clean browser session.")
        return 1
    if not _result.get("code"):
        print("No authorization code in the callback.")
        return 1

    try:
        token = await exchange_code(_result["code"])
    except OAuthError as exc:
        print(f"Token exchange failed: {exc}")
        return 1

    async with SessionLocal() as session:
        user = await get_or_create_default_user(session)
        row = await store_token(session, user.id, token)

    expiry = row.expires_at.isoformat() if row.expires_at else "no stated expiry"
    print(f"\nConnected. Token stored encrypted (scope: {row.scope or 'n/a'}, expires: {expiry}).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
