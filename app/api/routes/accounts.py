from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ApiTokenCreated,
    ApiTokenIn,
    ApiTokenOut,
    Credentials,
    ExtensionToken,
    UserOut,
)
from app.auth import tokens as api_tokens
from app.auth.accounts import (
    SESSION_COOKIE,
    AuthError,
    authenticate,
    create_extension_token,
    create_session_token,
    current_user,
    register_user,
)
from app.config import get_settings
from app.db.models import ApiToken, User, utcnow
from app.db.session import get_session

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _set_session_cookie(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user),
        max_age=settings.session_ttl_hours * 3600,
        # httponly: the token is never readable from JavaScript, so an XSS bug can't lift it.
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: Credentials, response: Response, session: AsyncSession = Depends(get_session)
) -> User:
    """Create an account and sign in. New accounts are always role `user`.

    There is no way to request `admin` here — promotion is a deliberate act by an existing admin.
    """
    try:
        user = await register_user(session, body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    _set_session_cookie(response, user)
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: Credentials, response: Response, session: AsyncSession = Depends(get_session)
) -> User:
    try:
        user = await authenticate(session, body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    _set_session_cookie(response, user)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/session/extension", response_model=ExtensionToken)
async def issue_extension_token(user: User = Depends(current_user)) -> ExtensionToken:
    """Mint a JWT the app can hand to the browser extension.

    Authenticated by the session cookie, which is the whole point: the extension runs on Upwork's
    origin where that cookie is never sent, so it cannot ask for this itself. The app can, and the
    two share a browser.

    The session cookie is *not* what gets handed over. It stays ``httponly`` and unreadable by
    JavaScript; this is a separate token carrying ``aud: extension``, refused anywhere a session
    belongs. A token lifted off a machine reads pages rather than becoming the account.

    ``user_id`` comes back alongside so the caller can check the extension holds a token for the
    person signed in *now*. A browser where someone else signed in afterwards would otherwise file
    one user's jobs into another's account, silently and with a progress bar — which is the failure
    this endpoint exists to make impossible.
    """
    token, expires = create_extension_token(user)
    return ExtensionToken(
        token=token, user_id=user.id, email=user.email, expires_at=expires
    )


# --- API tokens (for the browser extension) ---------------------------------------


@router.post("/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: ApiTokenIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiTokenCreated:
    """Mint an API token. The plaintext is in this response and nowhere else, ever again."""
    row, plaintext = await api_tokens.create(session, user.id, payload.name)
    return ApiTokenCreated(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        token=plaintext,
    )


@router.get("/tokens", response_model=list[ApiTokenOut])
async def list_tokens(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[ApiToken]:
    return list(
        await session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke, not delete — that this token existed and was used is worth keeping."""
    row = await session.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id)
    )
    # 404 rather than 403: whether someone else's token exists is not ours to confirm.
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such token.")
    row.revoked_at = utcnow()
    await session.commit()
