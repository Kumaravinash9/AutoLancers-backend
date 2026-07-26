"""Demo requests from the marketing page.

The submit route is public — the whole point is that someone without an account can reach us.
Reading them back is admin-only, which is why the two live on separate routers rather than one
with a mixed dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DemoRequestIn, DemoRequestOut
from app.auth.accounts import require_admin
from app.db.models import DemoRequest
from app.db.session import get_session

router = APIRouter(tags=["demo"])
admin_router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


@router.post("/demo-requests", response_model=DemoRequestOut, status_code=201)
async def create_demo_request(
    payload: DemoRequestIn, session: AsyncSession = Depends(get_session)
) -> DemoRequest:
    """Record a request for a walkthrough.

    Stored rather than emailed: a mailto to an unwatched address drops the request with no trace,
    and the person who sent it has no way to know.
    """
    request = DemoRequest(
        name=payload.name.strip(),
        email=payload.email.strip().lower(),
        note=(payload.note or "").strip() or None,
        marketplace=payload.marketplace,
    )
    session.add(request)
    await session.commit()
    await session.refresh(request)
    return request


@admin_router.get("/demo-requests", response_model=list[DemoRequestOut])
async def list_demo_requests(
    session: AsyncSession = Depends(get_session),
) -> list[DemoRequest]:
    """Newest first — an unanswered request from an hour ago matters more than one from June."""
    return list(
        await session.scalars(select(DemoRequest).order_by(DemoRequest.created_at.desc()))
    )
