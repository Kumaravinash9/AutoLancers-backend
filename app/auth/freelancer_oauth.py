"""Freelancer.com OAuth 2.0 — authorization code grant with refresh tokens.

App registration is self-service at https://accounts.freelancer.com/settings/create_app; a client
ID and secret are issued immediately with no approval queue.

v1 requests read scope only. The ``bid`` scope is deliberately not requested — nothing in this
codebase submits a bid, and not holding the capability is a stronger guarantee than not using it.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import decrypt, encrypt
from app.config import get_settings
from app.db.models import OAuthToken, utcnow

AUTHORIZE_URL = "https://accounts.freelancer.com/oauth/authorise"
TOKEN_URL = "https://accounts.freelancer.com/oauth/token"

# Read-only. Never add "bid" here without a deliberate decision to enable submission.
DEFAULT_SCOPE = "basic"

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
    """Return ``(url, state)``. Caller must persist ``state`` and check it on callback."""
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
            "advanced_scopes": "",
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
    session: AsyncSession, user_id: int, token: TokenResponse, platform: str = "freelancer"
) -> OAuthToken:
    """Upsert the encrypted token for this user+platform."""
    existing = await session.scalar(
        select(OAuthToken).where(
            OAuthToken.user_id == user_id, OAuthToken.platform == platform
        )
    )

    if existing is None:
        existing = OAuthToken(user_id=user_id, platform=platform)
        session.add(existing)

    existing.access_token = encrypt(token.access_token)
    # A refresh response may omit the refresh token; keep the one we already hold.
    if token.refresh_token:
        existing.refresh_token = encrypt(token.refresh_token)
    existing.expires_at = token.expires_at
    existing.scope = token.scope

    await session.commit()
    await session.refresh(existing)
    return existing


async def get_valid_access_token(
    session: AsyncSession, user_id: int, platform: str = "freelancer"
) -> str:
    """Return a usable access token, refreshing first if it is at or near expiry."""
    row = await session.scalar(
        select(OAuthToken).where(
            OAuthToken.user_id == user_id, OAuthToken.platform == platform
        )
    )
    if row is None:
        raise OAuthError(
            f"No {platform} token stored. Run: uv run python scripts/oauth_login.py"
        )

    if _needs_refresh(row):
        if not row.refresh_token:
            raise OAuthError(
                f"{platform} token expired and no refresh token is stored. Re-authorize with: "
                "uv run python scripts/oauth_login.py"
            )
        refreshed = await refresh_access_token(decrypt(row.refresh_token))
        row = await store_token(session, user_id, refreshed, platform)

    return decrypt(row.access_token)


def _needs_refresh(row: OAuthToken) -> bool:
    if row.expires_at is None:
        return False
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    return expires_at - _REFRESH_MARGIN <= utcnow()
