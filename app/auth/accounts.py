"""Platform accounts: passwords, sessions, and role checks.

Distinct from ``freelancer_oauth``, which authenticates you *to Freelancer.com*. This module is
about who you are on AutoLancers itself.

Roles are enforced server-side on every request. The frontend hides admin links, but hiding a
link is presentation — the check that matters is the dependency on the route.
"""

from __future__ import annotations

import datetime as dt
import uuid

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import tokens as api_tokens
from app.config import get_settings
from app.db.models import Role, User, utcnow
from app.db.session import get_session

SESSION_COOKIE = "al_session"
ALGORITHM = "HS256"

# bcrypt silently truncates beyond 72 bytes, so reject rather than accept a password whose tail
# is ignored — a user who set a 200-character passphrase should not have it quietly shortened.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 8


class AuthError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    _validate_password(password)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:MAX_PASSWORD_BYTES], password_hash.encode())
    except ValueError:
        return False


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_CHARS:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_CHARS} characters.")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise AuthError("Password is too long (72 bytes maximum).")


# Which audience a token was minted for. A session token carries none; an extension token says so,
# and each is refused where the other belongs. Without this the two are the same string in different
# places — an extension token would work as a web session, and a stolen one would be a full account.
EXTENSION_AUDIENCE = "extension"

# Short, because the app re-issues on every sync and the extension has no way to refresh on its own.
# Long enough that a collection started at the end of one cannot outlive it.
EXTENSION_TTL_HOURS = 12


def create_extension_token(user: User) -> tuple[str, dt.datetime]:
    """A JWT for the browser extension, minted by the app on the user's behalf.

    The extension cannot obtain a credential itself: it runs on Upwork's origin, where this app's
    session cookie is never sent. The app can, and hands this over.

    A separate token rather than the session cookie's own, for two reasons worth more than the
    convenience of reusing it. The session stays ``httponly`` and never becomes readable by
    JavaScript, so an XSS bug still cannot lift it. And this one carries ``aud``, so it is refused
    as a web session — a token taken off a machine reads pages, it does not become you.

    It carries ``sub``, which is the point for identity: the extension can tell whose token it holds
    without asking anyone, so a browser that switched users cannot quietly file one person's jobs
    into another's account.
    """
    settings = get_settings()
    if not settings.jwt_secret:
        raise AuthError("JWT_SECRET is not set — cannot issue extension tokens.")

    now = utcnow()
    expires = now + dt.timedelta(hours=EXTENSION_TTL_HOURS)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "aud": EXTENSION_AUDIENCE,
        "iat": now,
        "exp": expires,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM), expires


def decode_extension_token(token: str) -> dict:
    """Verify an extension token. Refuses anything not minted for that audience."""
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            audience=EXTENSION_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise AuthError("Extension token is invalid or expired.") from exc


def create_session_token(user: User) -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        raise AuthError("JWT_SECRET is not set — cannot issue sessions.")

    now = utcnow()
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "exp": now + dt.timedelta(hours=settings.session_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_session_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            # A session carries no audience. Saying so explicitly is what stops an extension token
            # being replayed as one: both are signed with the same key, so without this check the
            # separation would hold in one direction only, which is not a separation.
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise AuthError("Session is invalid or expired.") from exc

    if payload.get("aud"):
        raise AuthError("That token is not a session.")
    return payload


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    """The signed-in user, or 401.

    The role is re-read from the database rather than trusted from the token, so revoking an
    admin takes effect immediately instead of when their session happens to expire.
    """
    # A bearer token first: the Chrome extension runs on Upwork's origin and never carries our
    # session cookie. Both are first-class credentials; neither is a fallback for the other.
    #
    # Two shapes are accepted. A stored API token, revocable and unexpiring; and an extension JWT,
    # which the app mints on the user's behalf and which carries its own identity and expiry. The
    # JWT is tried second so a stored token is never spent on a signature check.
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()

        holder = await api_tokens.resolve(session, presented)
        if holder is not None:
            return holder

        try:
            payload = decode_extension_token(presented)
        except AuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

        # Re-read rather than trust the claim, for the same reason the cookie path does: a
        # deactivated account must stop working now, not when its token happens to expire.
        user = await session.get(User, uuid.UUID(payload["sub"]))
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active.")
        return user

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")

    try:
        payload = decode_session_token(token)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active.")
    return user


async def optional_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    """The signed-in user if there is one, or ``None`` — never a 401.

    For the one endpoint that may run unauthenticated while testing. It resolves a credential when
    one is offered, so a request that *does* carry one is still attributed to its owner; it simply
    does not insist. Whether anonymous is allowed at all is the caller's decision, not this one's.
    """
    try:
        return await current_user(request, session)
    except HTTPException:
        return None


async def require_admin(user: User = Depends(current_user)) -> User:
    """Admin-only routes depend on this. 404, not 403, so the portal isn't discoverable."""
    if not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == email.strip().lower()))

    # Same message either way: distinguishing them tells an attacker which emails are registered.
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("Email or password is incorrect.")
    if not user.is_active:
        raise AuthError("This account has been deactivated.")

    user.last_login_at = utcnow()
    await session.commit()
    return user


async def register_user(
    session: AsyncSession, email: str, password: str, role: str = Role.USER
) -> User:
    email = email.strip().lower()
    if await session.scalar(select(User).where(User.email == email)):
        raise AuthError("An account with that email already exists.")

    user = User(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user
