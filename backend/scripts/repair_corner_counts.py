"""One-off: correct corner counts that were read before the provider's statistics were final.

WHY THIS IS NEEDED SEPARATELY FROM THE SWEEP. ingest_live_scores now re-reads counts for
CORNER_RECHECK_HOURS after kickoff, which fixes everything from here on. It deliberately does
not reach further back, because past that window a provider does not revise a published figure
and re-asking spends calls to be told what we already hold. That leaves the counts already
frozen wrong before the fix existed, and those produce a WRONG TICK OR CROSS on a card users
open specifically to see whether their pick came in.

Measured 2026-08-15 across 45 recently-settled fixtures: 8 disagreed with the provider (18%),
every one an undercount, 4 of them flipping a shown verdict.

    Austria Lustenau v Wolfsberger AC   stored  9   real 10   over 9.5 shown as LOST
    Tianjin Teda v Beijing Guoan        stored 10   real 11
    Vissel Kobe v FC Tokyo              stored 10   real 11
    JEF United Chiba v Machida Zelvia   stored  9   real 10

NEVER CLEARS A COUNT. A provider that has stopped answering must not erase a real figure --
and for Veikkausliiga, whose counts come from TheStatsAPI because API-Football has none at all,
every re-read looks exactly like that.

Dry run by default. Pass --confirm to write.

    PYTHONPATH=. python scripts/repair_corner_counts.py
    PYTHONPATH=. python scripts/repair_corner_counts.py --confirm
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.adapters.api_football import fetch_corner_stats  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team  # noqa: E402
from app.sports.models import Sport  # noqa: E402

# Wide enough to cover everything stored before the re-read existed, without sweeping history
# that predates corner capture altogether.
LOOKBACK_DAYS = 30
# Both lines the product actually sells. A disagreement that does not cross one of these is
# still corrected, but it is not a wrong verdict on anybody's card.
CORNER_LINES = (9.5, 10.5)
PACE_SECONDS = 0.3


def flips_a_verdict(stored: int, real: int) -> bool:
    return any((stored > line) != (real > line) for line in CORNER_LINES)


async def main(confirm: bool) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Fixture, FixtureLiveState)
                .join(Sport, Sport.id == Fixture.sport_id)
                .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(
                    Sport.slug == "football",
                    Fixture.status == FixtureStatus.COMPLETED,
                    Fixture.kickoff_utc > now - timedelta(days=LOOKBACK_DAYS),
                    FixtureLiveState.home_corners.is_not(None),
                    Fixture.external_id.is_not(None),
                )
                .order_by(Fixture.kickoff_utc.desc())
            )
        ).all()
        print(f"settled football fixtures with stored corners: {len(rows)}")

        checked = wrong = flipped = unavailable = 0
        for fixture, live_state in rows:
            try:
                by_team = await fetch_corner_stats(fixture.external_id)
            except httpx.HTTPError as exc:
                print(f"  skip {fixture.external_id}: {exc}")
                continue
            await asyncio.sleep(PACE_SECONDS)

            home_ext = (
                await db.execute(select(Team.external_id).where(Team.id == fixture.home_team_id))
            ).scalar_one_or_none()
            away_ext = (
                await db.execute(select(Team.external_id).where(Team.id == fixture.away_team_id))
            ).scalar_one_or_none()
            real_home = by_team.get(str(home_ext)) if home_ext else None
            real_away = by_team.get(str(away_ext)) if away_ext else None
            if real_home is None or real_away is None:
                unavailable += 1
                continue

            checked += 1
            stored = (live_state.home_corners, live_state.away_corners)
            real = (real_home, real_away)
            if stored == real:
                continue

            wrong += 1
            marker = ""
            if flips_a_verdict(sum(stored), sum(real)):
                flipped += 1
                marker = "  <- FLIPS A SHOWN VERDICT"
            print(
                f"  {fixture.external_id}  {sum(stored)} -> {sum(real)}  "
                f"{stored} -> {real}{marker}"
            )
            if confirm:
                live_state.home_corners = real_home
                live_state.away_corners = real_away

        if confirm and wrong:
            await db.commit()

        print(
            f"\nchecked {checked}, disagreed {wrong}, of which {flipped} flip a verdict; "
            f"{unavailable} had no provider figure to compare against"
        )
        print("WROTE the corrections" if confirm else "dry run — pass --confirm to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    asyncio.run(main(parser.parse_args().confirm))
