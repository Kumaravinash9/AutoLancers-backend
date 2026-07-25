#!/usr/bin/env python
"""Seed a few realistic postings so you can exercise scoring and the UI without live credentials.

Development only — these are hand-written fixtures, not real Freelancer listings. Real jobs arrive
through the poller once you've connected an account.

    uv run python scripts/seed_demo_jobs.py
    uv run python scripts/seed_demo_jobs.py --clear   # remove them again
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.connectors.freelancer import JobPosting  # noqa: E402
from app.db.models import Job, utcnow  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.scoring import score_job  # noqa: E402
from app.services.users import get_or_create_default_user, get_or_create_profile  # noqa: E402

DEMO_PREFIX = "demo-"

DEMO_POSTINGS = [
    JobPosting(
        platform="freelancer",
        external_id=f"{DEMO_PREFIX}1",
        title="Next.js dashboard for a logistics startup",
        description=(
            "We run a small logistics company and track every shipment in spreadsheets. It is "
            "eating about 10 hours a week and mistakes are getting expensive. We want a web "
            "dashboard where our ops team can see live shipment status, filter by customer, and "
            "export a weekly report. We already have a Postgres database. Prefer React/Next.js. "
            "Needs a simple admin login for 5 users."
        ),
        url="https://www.freelancer.com/projects/demo/nextjs-logistics-dashboard",
        skills_listed=["Next.js", "React", "PostgreSQL", "Node.js"],
        budget_type="fixed",
        budget_min=1200.0,
        budget_max=2500.0,
        currency="USD",
        bid_count=6,
        posted_at=utcnow() - dt.timedelta(hours=2),
    ),
    JobPosting(
        platform="freelancer",
        external_id=f"{DEMO_PREFIX}2",
        title="AI chatbot to answer customer support questions from our docs",
        description=(
            "We get the same 20 support questions over and over. We'd like a chatbot on our site "
            "that answers from our existing documentation and hands off to a human when it isn't "
            "confident. Open to whatever stack you recommend. Must be able to update the docs "
            "without a developer."
        ),
        url="https://www.freelancer.com/projects/demo/ai-support-chatbot",
        skills_listed=["Python", "AI", "Chatbot", "LLM"],
        budget_type="fixed",
        budget_min=800.0,
        budget_max=1500.0,
        currency="USD",
        bid_count=14,
        posted_at=utcnow() - dt.timedelta(hours=9),
    ),
    JobPosting(
        platform="freelancer",
        external_id=f"{DEMO_PREFIX}3",
        title="Logo and brand palette for a bakery",
        description=(
            "Looking for a designer to create a logo, colour palette and business card for a new "
            "neighbourhood bakery. Illustrator source files required."
        ),
        url="https://www.freelancer.com/projects/demo/bakery-logo",
        skills_listed=["Graphic Design", "Illustrator", "Logo Design"],
        budget_type="fixed",
        budget_min=80.0,
        budget_max=150.0,
        currency="USD",
        bid_count=42,
        posted_at=utcnow() - dt.timedelta(hours=30),
    ),
    JobPosting(
        platform="freelancer",
        external_id=f"{DEMO_PREFIX}4",
        title="Co-founder / CTO wanted for fintech app (equity only)",
        description=(
            "Seeking a technical co-founder to build our MVP. Equity only, no cash upfront. Huge "
            "upside once we raise."
        ),
        url="https://www.freelancer.com/projects/demo/equity-cofounder",
        skills_listed=["React Native", "Node.js"],
        budget_type="fixed",
        budget_min=None,
        budget_max=None,
        currency="USD",
        bid_count=3,
        posted_at=utcnow() - dt.timedelta(hours=1),
    ),
]


async def seed() -> None:
    async with SessionLocal() as session:
        user = await get_or_create_default_user(session)
        profile = await get_or_create_profile(session, user.id)

        for posting in DEMO_POSTINGS:
            result = score_job(posting, profile)
            row = await session.scalar(
                select(Job).where(
                    Job.user_id == user.id,
                    Job.platform == posting.platform,
                    Job.external_id == posting.external_id,
                )
            )
            if row is None:
                row = Job(
                    user_id=user.id,
                    platform=posting.platform,
                    external_id=posting.external_id,
                )
                session.add(row)

            row.title = posting.title
            row.description = posting.description
            row.url = posting.url
            row.skills_listed = posting.skills_listed
            row.budget_type = posting.budget_type
            row.budget_min = posting.budget_min
            row.budget_max = posting.budget_max
            row.currency = posting.currency
            row.bid_count = posting.bid_count
            row.posted_at = posting.posted_at
            row.score = result.score
            row.reasons = result.reasons
            row.rejected = result.rejected
            row.rejection_reason = result.rejection_reason

            verdict = f"rejected — {result.rejection_reason}" if result.rejected else f"{result.score}"
            print(f"  {posting.title[:52]:<54} {verdict}")

        await session.commit()
    print(f"\nSeeded {len(DEMO_POSTINGS)} demo jobs.")


async def clear() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Job).where(Job.external_id.like(f"{DEMO_PREFIX}%")))
        await session.commit()
    print("Removed demo jobs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true", help="remove seeded demo jobs")
    args = parser.parse_args()
    asyncio.run(clear() if args.clear else seed())
