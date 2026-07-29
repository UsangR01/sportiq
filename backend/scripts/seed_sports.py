"""One-time, idempotent seed for sport/league rows.

Nothing in this codebase seeds a Sport/League row anywhere else — ingest_fixtures.py only
ever iterates `Sport.active == True`, which is empty on a fresh database. Run this once
before the first real ingestion:

    python scripts/seed_sports.py

Matches the TDD §6.1 example insert for NBA (slug, name, model_type, active).
"""

import asyncio

from sqlalchemy import select

from app.core.database import async_session_factory
from app.sports.models import League, Sport


async def seed_nba() -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "nba"))).scalar_one_or_none()
        if sport is None:
            sport = Sport(slug="nba", name="NBA Basketball", model_type="nba_xgb_v1", active=True)
            db.add(sport)
            await db.flush()
            print("created sport: nba")
        else:
            print("sport already exists: nba")

        league = (
            await db.execute(
                select(League).where(League.sport_id == sport.id, League.slug == "nba")
            )
        ).scalar_one_or_none()
        if league is None:
            db.add(
                League(
                    sport_id=sport.id, slug="nba", name="NBA", country="USA", tier=1, active=True
                )
            )
            print("created league: nba")
        else:
            print("league already exists: nba")

        await db.commit()


# slug/name/country must match both app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys and
# app/adapters/api_football.py's LEAGUE_IDS keys exactly, or odds/fixture ingestion silently
# resolve to nothing for that league.
FOOTBALL_LEAGUES = [
    ("epl", "Premier League", "England"),
    ("ligue1", "Ligue 1", "France"),
    ("bundesliga", "Bundesliga", "Germany"),
    ("laliga", "La Liga", "Spain"),
    ("seriea", "Serie A", "Italy"),
]


async def seed_football() -> None:
    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one_or_none()
        if sport is None:
            sport = Sport(
                slug="football", name="Football", model_type="football_xgb_v1", active=True
            )
            db.add(sport)
            await db.flush()
            print("created sport: football")
        else:
            print("sport already exists: football")

        for slug, name, country in FOOTBALL_LEAGUES:
            league = (
                await db.execute(
                    select(League).where(League.sport_id == sport.id, League.slug == slug)
                )
            ).scalar_one_or_none()
            if league is None:
                db.add(
                    League(
                        sport_id=sport.id,
                        slug=slug,
                        name=name,
                        country=country,
                        tier=1,
                        active=True,
                    )
                )
                print(f"created league: {slug}")
            else:
                print(f"league already exists: {slug}")

        await db.commit()


async def main() -> None:
    await seed_nba()
    await seed_football()


if __name__ == "__main__":
    asyncio.run(main())
