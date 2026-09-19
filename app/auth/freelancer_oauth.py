"""Freelancer.com OAuth 2.0 — authorization code grant with refresh tokens.

Register an app at https://accounts.freelancer.com/settings/develop (you must be logged in first).
Note the path is ``/settings/develop`` — ``/settings/create_app`` is a 404.

Connecting an account is **optional**: project discovery works unauthenticated, and a token only
buys richer fields and higher rate limits.

v1 requests read scope only. The ``bid`` scope is deliberately not requested — nothing in this
codebase submits a bid, and not holding the capability is a stronger guarantee than not using it.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import decrypt, encrypt
from app.config import get_settings
from app.db.models import PlatformConnection, utcnow

AUTHORIZE_URL = "https://accounts.freelancer.com/oauth/authorise"
TOKEN_URL = "https://accounts.freelancer.com/oauth/token"

# Identity scope. Read-only on its own.
DEFAULT_SCOPE = "basic"

# Freelancer's advanced scope covering "manage your projects, bids and milestones". It travels in
# a separate `advanced_scopes` parameter, not in `scope`, and is only granted to apps Freelancer
# has approved for it — requesting it before approval fails at the consent screen.
BID_SCOPE = "fln:project_manage"


def has_bid_scope(scope: str | None) -> bool:
    """Whether a stored token actually carries the bid permission.

    Checked before every submission. A token minted under read-only scope must never be assumed
    to have gained the capability just because config was flipped on.
    """
    return bool(scope) and BID_SCOPE in scope

# Refresh this far ahead of actual expiry so a long request can't straddle the boundary.
_REFRESH_MARGIN = dt.timedelta(minutes=5)


class OAuthError(RuntimeError):
    pass


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None

    @property
    def expires_at(self) -> dt.datetime | None:
        if self.expires_in is None:
            return None
        return utcnow() + dt.timedelta(seconds=self.expires_in)


def build_authorize_url(state: str | None = None, scope: str = DEFAULT_SCOPE) -> tuple[str, str]:
    """Return ``(url, state)``. Caller must persist ``state`` and check it on callback.

    The bid scope is requested only when ``ENABLE_BIDDING`` is on, so a read-only install cannot
    accidentally hold a permission it never intends to use.
    """
    settings = get_settings()
    if not settings.freelancer_client_id:
        raise OAuthError("FREELANCER_CLIENT_ID is not set")

    state = state or secrets.token_urlsafe(24)
    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": settings.freelancer_client_id,
            "redirect_uri": settings.freelancer_redirect_uri,
            "scope": scope,
            "prompt": "select_account",
            "advanced_scopes": BID_SCOPE if settings.enable_bidding else "",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{params}", state


async def exchange_code(code: str) -> TokenResponse:
    settings = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.freelancer_client_id,
        "client_secret": settings.freelancer_client_secret,
        "redirect_uri": settings.freelancer_redirect_uri,
    }
    return await _post_token(payload)


async def refresh_access_token(refresh_token: str) -> TokenResponse:
    settings = get_settings()
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.freelancer_client_id,
        "client_secret": settings.freelancer_client_secret,
    }
    return await _post_token(payload)


async def _post_token(payload: dict[str, str]) -> TokenResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(TOKEN_URL, data=payload)

    if response.status_code >= 400:
        raise OAuthError(f"Token endpoint returned {response.status_code}: {response.text[:400]}")

    data = response.json()
    if "access_token" not in data:
        raise OAuthError(f"Token response had no access_token: {data}")

    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scope=data.get("scope"),
    )


async def store_token(
    session: AsyncSession, user_id: uuid.UUID, token: TokenResponse, platform: str = "freelancer"
) -> PlatformConnection:
    """Upsert the encrypted token for this specific marketplace account.

    Asks the marketplace who the token belongs to before storing it. Without that, connecting a
    second Freelancer account would overwrite the first — the two are indistinguishable until you
    ask.
    """
    from app.connectors import ConnectorError, create_connector
    from app.connectors.freelancer import self_profile_fields

    account_id: str | None = None
    username: str | None = None
    me: dict | None = None
    try:
        me = await create_connector(platform, access_token=token.access_token).fetch_self()
        account_id = str(me.get("id") or "") or None
        username = me.get("username")
    except ConnectorError:
        # A token we can't identify is still worth storing; it just can't be told apart yet.
        pass

    existing = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
            PlatformConnection.platform_user_id == account_id,
        )
    )

    if existing is None:
        existing = PlatformConnection(
            user_id=user_id,
            platform=platform,
            platform_user_id=account_id,
            platform_username=username,
        )
        session.add(existing)
    elif username:
        existing.platform_username = username

    # Reconnecting an account revives its soft-deleted row rather than spawning a duplicate — the
    # upsert above already matched it by account id. A no-op for a row that was never disconnected.
    existing.disconnected_at = None
    existing.status = "ACTIVE"

    existing.access_token_encrypted = encrypt(token.access_token)
    # A refresh response may omit the refresh token; keep the one we already hold.
    if token.refresh_token:
        existing.refresh_token_encrypted = encrypt(token.refresh_token)
    existing.token_expires_at = token.expires_at
    existing.scope = token.scope

    await session.commit()
    await session.refresh(existing)

    # Mirror the account's public profile onto its 1:1 profile. ``fetch_self`` was already called
    # above for identity; using the rest of it here is free. A fetch that failed leaves ``me`` None,
    # in which case there's nothing to mirror and the stored token still stands.
    if me is not None:
        from app.services.users import get_or_create_profile_for_connection

        profile = await get_or_create_profile_for_connection(session, existing)
        _mirror_self_onto_profile(profile, me, self_profile_fields(me))
        # Portfolio items live on their own endpoint (self/ only carries a count). A bonus, so a
        # failure here must not block storing the token.
        if account_id:
            try:
                profile.portfolio = await create_connector(
                    platform, access_token=token.access_token
                ).fetch_portfolio(int(account_id))
            except ConnectorError:
                pass
        await session.commit()
        await session.refresh(existing)

    return existing


def _mirror_self_onto_profile(profile, me: dict, fields: dict) -> None:
    """Write the mapped self-profile fields onto ``profile``, then stamp the sync time.

    Currency is adopted only while no budget floor has been set: once the freelancer has entered
    floors, the currency those numbers are stated in is theirs to change, not the sync's to silently
    reinterpret.
    """
    currency = fields.pop("currency", None)
    for key, value in fields.items():
        setattr(profile, key, value)
    if currency and not profile.rate_min and not profile.fixed_project_min:
        profile.currency = currency
    profile.last_synced_at = utcnow()


async def get_valid_access_token(
    session: AsyncSession,
    user_id: uuid.UUID,
    platform: str = "freelancer",
    connection: PlatformConnection | None = None,
) -> str:
    """Return a usable access token, refreshing first if it is at or near expiry.

    With several accounts connected, pass the one you mean. Omitting it picks the oldest active
    connection, which keeps single-account installs working unchanged.
    """
    row = connection or await session.scalar(
        select(PlatformConnection)
        .where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
            PlatformConnection.status == "ACTIVE",
        )
        .order_by(PlatformConnection.connected_at)
    )
    if row is None:
        raise OAuthError(
            f"No {platform} token stored. Run: uv run python scripts/oauth_login.py"
        )

    if _needs_refresh(row):
        if not row.refresh_token_encrypted:
            raise OAuthError(
                f"{platform} token expired and no refresh token is stored. Re-authorize with: "
                "uv run python scripts/oauth_login.py"
            )
        refreshed = await refresh_access_token(decrypt(row.refresh_token_encrypted))
        row = await store_token(session, user_id, refreshed, platform)

    return decrypt(row.access_token_encrypted)


def _needs_refresh(row: PlatformConnection) -> bool:
    if row.token_expires_at is None:
        return False
    expires_at = row.token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    return expires_at - _REFRESH_MARGIN <= utcnow()
