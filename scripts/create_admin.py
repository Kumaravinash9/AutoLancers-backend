#!/usr/bin/env python
"""Create or promote an admin account.

The only way to mint the first admin — registration always creates a plain user, so admin is
never something a signup form can grant itself.

    uv run python scripts/create_admin.py you@example.com
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.auth.accounts import AuthError, hash_password  # noqa: E402
from app.db.models import Role, User  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


async def main(email: str) -> int:
    email = email.strip().lower()
    password = getpass.getpass("Password (min 8 chars): ")
    if password != getpass.getpass("Confirm: "):
        print("Passwords do not match.")
        return 1

    try:
        digest = hash_password(password)
    except AuthError as exc:
        print(exc)
        return 1

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, role=Role.ADMIN, password_hash=digest)
            session.add(user)
            action = "Created"
        else:
            user.role = Role.ADMIN
            user.password_hash = digest
            user.is_active = True
            action = "Promoted"
        await session.commit()

    print(f"{action} admin: {email}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
