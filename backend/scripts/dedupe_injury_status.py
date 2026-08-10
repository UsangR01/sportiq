"""One-off: collapse duplicate player_injury_status rows to the newest per (sport, team, player).

API-Football's /injuries is fixture-scoped, so every run re-reported the same standing injury
for every upcoming fixture date. That produced 12,330 rows for 130 distinct players — 95
duplicates each — before ingest_injuries.py was changed to upsert.

Nothing ever read the duplicates: Stage 2 already ordered by updated_at and took the newest.
They were storage and query noise, and they made "how many players are currently injured"
unanswerable without a DISTINCT — which is how the availability signal looked far healthier
than it was.

Keeps the NEWEST row per key, since that is the one Stage 2 would have selected anyway, so the
collapse cannot change any current availability answer.

Dry-run by default, --confirm to execute. Mirrors purge_test_push_tokens.py.
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlalchemy import delete, func, select  # noqa: E402

from app.core.database import async_session_factory, engine  # noqa: E402
from app.fixtures.models import PlayerInjuryStatus  # noqa: E402


async def main(confirm: bool) -> None:
    async with async_session_factory() as db:
        total = (await db.execute(select(func.count()).select_from(PlayerInjuryStatus))).scalar()

        # Resolved in Python rather than SQL: the primary key is a UUID and Postgres has no
        # max(uuid), so the usual "keep max(id) per group" trick does not apply. At ~12k rows
        # this is a single cheap pass and stays readable, which matters more here than
        # cleverness in a one-off script.
        rows = (
            await db.execute(
                select(
                    PlayerInjuryStatus.id,
                    PlayerInjuryStatus.sport_id,
                    PlayerInjuryStatus.team_id,
                    PlayerInjuryStatus.player_id,
                    PlayerInjuryStatus.updated_at,
                )
            )
        ).all()
        newest: dict[tuple, tuple] = {}
        for row_id, sport_id, team_id, player_id, updated_at in rows:
            key = (sport_id, team_id, player_id)
            current = newest.get(key)
            if current is None or updated_at > current[1]:
                newest[key] = (row_id, updated_at)
        keep_ids = {row_id for row_id, _ in newest.values()}
        doomed = total - len(keep_ids)

        print(f"  rows now: {total:,}")
        print(f"  distinct (sport, team, player): {len(keep_ids):,}")
        print(f"  duplicates to remove: {doomed:,}")

        if not doomed:
            print("  nothing to do")
            return
        if not confirm:
            print("\n  dry run — re-run with --confirm to apply")
            return

        result = await db.execute(
            delete(PlayerInjuryStatus).where(PlayerInjuryStatus.id.notin_(keep_ids))
        )
        await db.commit()
        print(f"\n  removed {result.rowcount:,} duplicate rows")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually delete")
    asyncio.run(main(parser.parse_args().confirm))
