from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import Credentials, UserOut
from app.auth.accounts import (
    SESSION_COOKIE,
    AuthError,
    authenticate,
    create_session_token,
    current_user,
    register_user,
)
from app.config import get_settings
from app.db.models import User
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
