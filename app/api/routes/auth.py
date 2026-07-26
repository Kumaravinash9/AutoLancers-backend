from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AuthStatus
from app.auth.freelancer_oauth import (
    OAuthError,
    build_authorize_url,
    exchange_code,
    store_token,
)
from app.config import get_settings
from app.db.models import PlatformConnection
from app.db.session import get_session
from app.services.users import get_or_create_default_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Single-user, single-process: an in-memory set is enough to make state checkable. It resets on
# restart, which only means an in-flight authorization has to be started again.
_pending_states: set[str] = set()


@router.get("/freelancer/login")
async def login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    try:
        url, _ = build_authorize_url(state=state)
    except OAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url)


@router.get("/freelancer/callback")
async def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    frontend = get_settings().frontend_origin

    if error:
        return RedirectResponse(f"{frontend}/settings?connected=0&error={error}")
    if not code:
        return RedirectResponse(f"{frontend}/settings?connected=0&error=missing_code")
    if not state or state not in _pending_states:
        return RedirectResponse(f"{frontend}/settings?connected=0&error=state_mismatch")

    _pending_states.discard(state)

    try:
        token = await exchange_code(code)
        user = await get_or_create_default_user(session)
        await store_token(session, user.id, token)
    except OAuthError as exc:
        return RedirectResponse(f"{frontend}/settings?connected=0&error={exc}")

    return RedirectResponse(f"{frontend}/settings?connected=1")


@router.get("/status", response_model=AuthStatus)
async def status(
    platform: str = Query(default="freelancer"),
    session: AsyncSession = Depends(get_session),
) -> AuthStatus:
    user = await get_or_create_default_user(session)
    row = await session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == platform,
            PlatformConnection.disconnected_at.is_(None),
        )
    )
    if row is None:
        return AuthStatus(connected=False, platform=platform, detail="No token stored")
    return AuthStatus(
        connected=True, platform=platform, scope=row.scope, expires_at=row.token_expires_at
    )
