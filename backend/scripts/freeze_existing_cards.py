"""Freeze every fixture that has already kicked off, once, when this table is introduced.

WITHOUT THIS THE FIX ONLY PROTECTS THE FUTURE. Every fixture already played keeps being
recomputed on each request, so the next guard change or model promotion rewrites it again --
which is precisely what was reported.

STATED PLAINLY, BECAUSE IT IS A REAL LIMIT: this records what each card reads TODAY, not what it
read when it was published. For the 76 cards measured as already altered between 2026-08-30 and
2026-09-04 (28 showing a different bet, 48 gone), the ORIGINAL is unrecoverable -- best_pick was
never stored, and pick_snapshots covers only a minority of fixtures from 2026-08-10 onward. This
stops the drift; it does not undo it. Rows are marked frozen_reason='backfill' so a future
reader can tell a reconstruction from a genuine capture rather than trusting them equally.

    PYTHONPATH=. python scripts/freeze_existing_cards.py            # dry run
    PYTHONPATH=. python scripts/freeze_existing_cards.py --confirm

Batched, because _bulk_best_picks' corners reference scales with distinct TEAMS and the prod web
shell shares the live container -- it has been OOM-killed twice at larger batch sizes.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.database import async_session_factory, engine
from app.fixtures.models import Fixture, FixtureStatus
from app.predictions.models import FrozenPick

BATCH = 40


async def main(confirm: bool, batch: int) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        pending = (
            (
                await db.execute(
                    select(Fixture.id)
                    .outerjoin(FrozenPick, FrozenPick.fixture_id == Fixture.id)
                    .where(
                        Fixture.kickoff_utc <= now,
                        Fixture.status != FixtureStatus.POSTPONED,
                        FrozenPick.id.is_(None),
                    )
                    .order_by(Fixture.kickoff_utc.desc())
                )
            )
            .scalars()
            .all()
        )
        already = (await db.execute(select(func.count(FrozenPick.id)))).scalar_one()

    print(f"already frozen: {already}")
    print(f"kicked-off fixtures still unfrozen: {len(pending)}")
    if not pending:
        await engine.dispose()
        return
    if not confirm:
        print("\ndry run - re-run with --confirm to write")
        await engine.dispose()
        return

    from app.predictions.pick_freeze import _freeze

    written = 0
    for start in range(0, len(pending), batch):
        chunk = list(pending[start : start + batch])
        async with async_session_factory() as db:
            written += await _freeze(db, chunk, reason="backfill")
        print(f"  {written}/{len(pending)}")
    print(f"\nfroze {written} cards")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--batch", type=int, default=BATCH)
    asyncio.run(main(parser.parse_args().confirm, parser.parse_args().batch))
