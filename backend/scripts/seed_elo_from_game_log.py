"""One-off: seed Team.elo_rating from the historical game logs, where it is NULL.

WHY. Team.elo_rating is only ever written at settlement, so a league whose fixtures have never
settled in THIS database has no Elo at all -- measured: EPL, La Liga, Ligue 1, Serie A and the
Bundesliga all sit at zero teams with a rating, because their seasons ended before this
database existed. Their opening-weekend vectors therefore lose the single strongest
team-strength signal, and every new season would begin from the 1500 default as if five
collected seasons had never happened.

The game logs hold that history, and settlement continues the walk from wherever this seeding
leaves it -- the same INITIAL_ELO/K_FACTOR/update as both training and live settlement.

ONLY FILLS NULL. A team with a real rating evolved from our own settlements (MLS, Brasileirão,
the CSL...) is never touched: overwriting it would discard genuinely observed matches in favour
of the parquet's possibly-older cut of the same story.

Dry run by default. Pass --confirm to write.

    PYTHONPATH=. python scripts/seed_elo_from_game_log.py
    PYTHONPATH=. python scripts/seed_elo_from_game_log.py --confirm
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import Team  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402
from app.workers.backfill_predictions import _league_game_log, final_elo_ratings  # noqa: E402


async def main(confirm: bool) -> None:
    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one_or_none()
        if sport is None:
            print("no football sport row")
            return
        leagues = (
            (await db.execute(select(League).where(League.sport_id == sport.id))).scalars().all()
        )

        seeded = skipped_has_rating = no_history = 0
        for league in leagues:
            game_log, _lineups = await _league_game_log(db, league)
            if game_log.empty:
                continue
            ratings = final_elo_ratings(game_log)
            teams = (
                (await db.execute(select(Team).where(Team.league_id == league.id))).scalars().all()
            )
            filled = []
            for team in teams:
                if team.elo_rating is not None:
                    skipped_has_rating += 1
                    continue
                rating = ratings.get(str(team.external_id)) if team.external_id else None
                if rating is None:
                    no_history += 1
                    continue
                filled.append((team.name, rating))
                if confirm:
                    team.elo_rating = rating
                seeded += 1
            if filled:
                sample = ", ".join(
                    f"{n} {r:.0f}" for n, r in sorted(filled, key=lambda x: -x[1])[:3]
                )
                print(f"  {league.slug:22} {len(filled):3} seeded   top: {sample}")

        if confirm:
            await db.commit()
        print(
            f"\nseeded {seeded}, left alone (already rated) {skipped_has_rating}, "
            f"no history {no_history}"
        )
        print("WROTE" if confirm else "dry run — pass --confirm to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    asyncio.run(main(parser.parse_args().confirm))
