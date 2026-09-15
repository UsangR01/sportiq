"""Make sure every league in the catalog has a League row to attach fixtures to.

INGEST ITERATES ROWS, NOT THE CATALOG, so a league with no row is simply never fetched -- no
error, no warning, nothing in the feed. Adding one has always meant running
scripts/seed_sports.py by hand in a production shell, and that shell has repeatedly been the
weak link: the serving-registry outage in September sat unfixed for forty minutes because the
Render shell kept dropping its connection, which is why the model repair now runs at startup
too. This is the same lesson applied to the same class of problem.

STRICTLY ADDITIVE. It inserts rows that do not exist and touches nothing that does -- never a
rename, never a deactivation, never a delete. A league removed from the catalog keeps its row
and its history; retiring one is a deliberate act, not a side effect of an edit.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sports.catalog import FOOTBALL_LEAGUES
from app.sports.models import League, Sport

logger = logging.getLogger(__name__)


async def ensure_football_leagues(db: AsyncSession) -> list[str]:
    """Insert a League row for any catalog entry that has none. Returns the slugs created."""
    sport = (await db.execute(select(Sport).where(Sport.slug == "football"))).scalar_one_or_none()
    if sport is None:
        # Deliberately NOT created here. A missing Sport row means the database was never
        # seeded at all, which is a bootstrap problem for seed_sports.py to solve loudly rather
        # than something an API start should paper over.
        logger.warning("no football sport row - run scripts/seed_sports.py; skipping league sync")
        return []

    existing = {
        slug
        for (slug,) in (
            await db.execute(select(League.slug).where(League.sport_id == sport.id))
        ).all()
    }
    created = []
    for slug, name, country in FOOTBALL_LEAGUES:
        if slug in existing:
            continue
        db.add(
            League(sport_id=sport.id, slug=slug, name=name, country=country, tier=1, active=True)
        )
        created.append(slug)
    if created:
        await db.commit()
        logger.warning("seeded %d new football leagues: %s", len(created), ", ".join(created))
    return created
