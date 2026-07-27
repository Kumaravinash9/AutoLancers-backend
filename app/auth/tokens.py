"""API tokens for clients without a browser session.

The Chrome extension can't use the session cookie: it runs on Upwork's origin, and a cookie scoped
to this API isn't sent from there. A bearer token is the honest alternative — explicit, revocable,
and visible in a list the user controls.

Tokens are hashed with SHA-256 rather than bcrypt. That is deliberate and safe *here*, unlike for
passwords: a 32-byte random token has no guessable structure, so the slow-hash property that
protects a human-chosen password buys nothing, while the speed matters on every extension request.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiToken, User, utcnow

# Prefixed so a leaked string is recognisable in a log or a paste, and so secret scanners can
# pattern-match it.
PREFIX = "al_"


def generate() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create(session: AsyncSession, user_id: uuid.UUID, name: str) -> tuple[ApiToken, str]:
    """Mint a token. Returns the row and the plaintext, which is never recoverable again."""
    plaintext = generate()
    row = ApiToken(user_id=user_id, name=name.strip() or "Chrome extension",
                   token_hash=fingerprint(plaintext))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, plaintext


async def resolve(session: AsyncSession, token: str) -> User | None:
    """The user this token belongs to, or None. Records the use."""
    if not token.startswith(PREFIX):
        return None

    row = await session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == fingerprint(token), ApiToken.revoked_at.is_(None)
        )
    )
    if row is None:
        return None

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    row.last_used_at = utcnow()
    await session.commit()
    return user
