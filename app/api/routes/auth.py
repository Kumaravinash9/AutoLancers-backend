from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AuthStatus
from app.auth.accounts import current_user
from app.auth.freelancer_oauth import (
    OAuthError,
    build_authorize_url,
    exchange_code,
    store_token,
)
from app.config import get_settings
from app.db.models import PlatformConnection, User
from app.db.session import get_session

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
    request: Request,
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

    # Resolved by hand rather than as a dependency: this endpoint answers a browser redirect, so a
    # signed-out visitor belongs back on /settings with a reason, not looking at a raw 401 body.
    # The session cookie is SameSite=lax, which a top-level navigation from the provider still
    # carries — so arriving here without one means genuinely signed out.
    try:
        user = await current_user(request, session)
    except HTTPException:
        return RedirectResponse(f"{frontend}/settings?connected=0&error=not_signed_in")

    try:
        token = await exchange_code(code)
        await store_token(session, user.id, token)
    except OAuthError as exc:
        return RedirectResponse(f"{frontend}/settings?connected=0&error={exc}")

    return RedirectResponse(f"{frontend}/settings?connected=1")


@router.get("/status", response_model=AuthStatus)
async def status(
    platform: str = Query(default="freelancer"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AuthStatus:
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
