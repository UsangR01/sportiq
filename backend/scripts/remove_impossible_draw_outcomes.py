"""Remove settled Outcome rows recording a draw in a sport that cannot draw.

Tennis matches always have a winner, retirements included: 6-1, 6-7, 0-2 ret. is 1-1 in
COMPLETED sets and still has a real result. _maybe_settle_outcome derived the result from the
score alone and fell through to MatchResult.DRAW whenever the two sides were level, so twelve
impossible rows accumulated.

WHY DELETE RATHER THAN CORRECT: the winner is not recoverable. BallDontLie exposes no
retirement marker, and its habit of listing the winner as player1 was measured and does not
hold where it would be needed -- 100% on settled 2022/2025 data via the list endpoint, but
68% on the current season and 48% via /matches/{id}. Assigning winners from a coin flip would
turn twelve visibly-wrong rows into twelve invisibly-wrong ones, which is strictly worse.

Deletion only sticks because ingest_fixtures.SPORTS_WITHOUT_DRAWS now refuses to settle a tied
score for these sports. Without that guard the next ingest would recreate every row, since
_maybe_settle_outcome treats an absent Outcome as "not yet settled". Do not run this against a
deployment that has not taken that change.

The fixtures themselves are untouched: FixtureLiveState keeps the real score and result_type,
so the feed still renders them (as VOID where result_type says so). Only the false claim about
who won is removed.

Elo is knowingly NOT reverted, matching repair_tennis_retirement_scores.py's own reasoning:
these applied a draw-shaped update that should never have happened, and undoing it properly
would mean replaying every player's Elo history in order. It is inert in practice because
tennis's feature set uses rank_diff, not Elo (app/models_ml/tennis_features.py:FEATURE_NAMES).

Usage (from backend/):
    PYTHONPATH=. python scripts/remove_impossible_draw_outcomes.py            # dry run
    PYTHONPATH=. python scripts/remove_impossible_draw_outcomes.py --confirm
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlalchemy import delete, select  # noqa: E402

from app.core.database import async_session_factory, engine  # noqa: E402
from app.fixtures.models import Fixture, FixtureLiveState  # noqa: E402
from app.history.models import MatchResult, Outcome  # noqa: E402
from app.sports.models import Sport  # noqa: E402
from app.workers.ingest_fixtures import SPORTS_WITHOUT_DRAWS  # noqa: E402


async def main(confirm: bool) -> None:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Outcome, Sport.slug, FixtureLiveState.result_type)
                .join(Fixture, Fixture.id == Outcome.fixture_id)
                .join(Sport, Sport.id == Fixture.sport_id)
                .outerjoin(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(Sport.slug.in_(SPORTS_WITHOUT_DRAWS), Outcome.result == MatchResult.DRAW)
            )
        ).all()

        print(f"  impossible drawn outcomes: {len(rows)}")
        for outcome, slug, result_type in rows:
            print(
                f"     {slug} {outcome.home_score}-{outcome.away_score} "
                f"result_type={result_type or '(none)'}"
            )

        if not rows:
            print("  nothing to do")
            return
        if not confirm:
            print("\n  dry run — re-run with --confirm to delete")
            return

        result = await db.execute(
            delete(Outcome).where(Outcome.fixture_id.in_([o.fixture_id for o, _, _ in rows]))
        )
        await db.commit()
        print(f"\n  deleted {result.rowcount} impossible outcome rows")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually delete")
    asyncio.run(main(parser.parse_args().confirm))
