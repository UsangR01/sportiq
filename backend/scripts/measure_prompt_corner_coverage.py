"""Which leagues can settle a corners pick AT FULL TIME — re-derive the exclusion set.

RUN THIS THE DAY A RICHER DATA SOURCE IS IN PLACE. It is the whole re-enabling procedure: if
every league clears MIN_PROMPT_CORNER_COVERAGE, set LEAGUES_WITHOUT_PROMPT_CORNERS to
frozenset() in app/fixtures/corners_availability.py and corners are offered everywhere again.

Asks the PROMPT source directly rather than reading our own stored counts, because those no
longer say which source filled them — TheStatsAPI backfills the gaps hours later, so a league
can look fully covered in our database while every one of its cards sat grey overnight.

    PYTHONPATH=. python scripts/measure_prompt_corner_coverage.py
    PYTHONPATH=. python scripts/measure_prompt_corner_coverage.py --per-league 12
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.adapters.api_football import fetch_match_stats  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.corners_availability import (  # noqa: E402
    LEAGUES_WITHOUT_PROMPT_CORNERS,
    MIN_PROMPT_CORNER_COVERAGE,
)
from app.fixtures.models import Fixture, FixtureStatus, Team  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402

PACE_SECONDS = 0.25


async def main(per_league: int) -> None:
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
        work = defaultdict(list)
        for league in leagues:
            fixtures = (
                (
                    await db.execute(
                        select(Fixture)
                        .where(
                            Fixture.league_id == league.id,
                            Fixture.status == FixtureStatus.COMPLETED,
                            Fixture.external_id.is_not(None),
                        )
                        .order_by(Fixture.kickoff_utc.desc())
                        .limit(per_league)
                    )
                )
                .scalars()
                .all()
            )
            for fixture in fixtures:
                home = (
                    await db.execute(select(Team).where(Team.id == fixture.home_team_id))
                ).scalar_one_or_none()
                away = (
                    await db.execute(select(Team).where(Team.id == fixture.away_team_id))
                ).scalar_one_or_none()
                if home and away:
                    work[league.slug].append((fixture, home, away))

    print(f"{'league':24} {'prompt':>10}  verdict")
    print("-" * 58)
    coverage = {}
    for slug in sorted(work):
        have = total = 0
        for fixture, home, away in work[slug]:
            try:
                stats = await fetch_match_stats(fixture.external_id)
            except httpx.HTTPError:
                continue
            await asyncio.sleep(PACE_SECONDS)
            total += 1
            if (
                getattr(stats.get(home.external_id), "corners", None) is not None
                and getattr(stats.get(away.external_id), "corners", None) is not None
            ):
                have += 1
        if not total:
            print(f"{slug:24} {'no sample':>10}")
            continue
        coverage[slug] = have / total
        ok = coverage[slug] >= MIN_PROMPT_CORNER_COVERAGE
        print(f"{slug:24} {have}/{total} = {coverage[slug]:4.0%}  {'OK' if ok else 'EXCLUDE'}")

    should_exclude = {s for s, c in coverage.items() if c < MIN_PROMPT_CORNER_COVERAGE}
    print(f"\nmeasured exclusion set : {sorted(should_exclude)}")
    print(f"configured in code     : {sorted(LEAGUES_WITHOUT_PROMPT_CORNERS)}")
    if not should_exclude:
        print(
            "\nEVERY SAMPLED LEAGUE SETTLES CORNERS PROMPTLY.\n"
            "Set LEAGUES_WITHOUT_PROMPT_CORNERS = frozenset() in "
            "app/fixtures/corners_availability.py to offer corners everywhere again."
        )
    elif should_exclude != set(LEAGUES_WITHOUT_PROMPT_CORNERS):
        print("\nThe configured set no longer matches the measurement — update it to match.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-league", type=int, default=6)
    asyncio.run(main(parser.parse_args().per_league))
