"""How often does a fixture's headline pick CHANGE in the hours before kickoff?

WHY THIS EXISTS. Reported by a user twice in one day: a WNBA pick moved 59% -> 66% overnight,
and a La Liga card that had shown "over 1.5 goals" days earlier showed a double chance instead.
Both are the same mechanism -- best_pick is recomputed on every request against whatever
prediction and odds exist at that moment, and nothing is stored -- and the proposed remedy was
to freeze the pick near kickoff.

Freezing costs something real: books price late, and the market feeds the model. So before
building a freeze, measure how often the thing it prevents actually happens. If the headline
market flips in 2% of cards inside two hours, a freeze is ceremony; if it flips in 20%, it is
worth the staleness it buys.

WHAT IS MEASURED. For each settled fixture, the pick is RECONSTRUCTED at kickoff minus each
requested window and compared against its final pre-kickoff state:

    prediction   the latest row created at or before that moment (predictions are append-only,
                 so the series is genuinely recoverable)
    odds         every price posted at or before that moment, then latest-per-bookmaker --
                 the same collapse the live path applies

Three different changes are counted separately, because they matter differently to a user:

    MARKET FLIP      the recommended BET changed (over 1.5 -> 1X). The disruptive one: a user
                     who acted on the earlier card now holds a different bet than the app shows.
    PROBABILITY MOVE the same bet, a different number. Ordinary, and usually the market
                     arriving rather than noise.
    THRESHOLD CROSS  the probability crossed the 0.60 default, so the card appeared in or
                     vanished from a filtered feed. This is what the reported WNBA case actually
                     was, and a market freeze would not touch it.

It reuses the router's own _all_market_candidates/_pick_best rather than reimplementing the
ranking, so the measurement cannot drift away from what the product actually shows.

MEMORY. Fixtures are processed in batches, and only that batch's odds and predictions are held
at once. The first version loaded everything up front and was OOM-KILLED on production -- the
shell blanked and reconnected in a loop. Odds dominate: one tennis fixture can carry 40+ price
rows across 18 bookmakers.

    PYTHONPATH=. python scripts/measure_pick_flips.py
    PYTHONPATH=. python scripts/measure_pick_flips.py --hours 2 6 12 24 --limit 3000
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory, engine  # noqa: E402
from app.fixtures.corners_availability import offers_corners  # noqa: E402
from app.fixtures.goals_availability import offers_goals  # noqa: E402
from app.fixtures.league_availability import suppressed_markets_for  # noqa: E402
from app.fixtures.models import Fixture, FixtureStatus  # noqa: E402
from app.fixtures.router import (  # noqa: E402
    NO_DEMONSTRATED_SIGNAL_MARKETS,
    _all_market_candidates,
    _pick_best,
    _prediction_precedence,
)
from app.models_ml.corners_reference import bulk_corners_reference  # noqa: E402
from app.odds.models import Odds  # noqa: E402
from app.picks.service import latest_price_per_bookmaker  # noqa: E402
from app.predictions.models import Prediction  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402

DEFAULT_HOURS = (2, 6, 12)
DB_MARKETS = ("h2h", "double_chance", "total", "corners_total")
# The mobile default min_probability. A pick crossing it is what the user reported for the WNBA
# card ("at 59% it did not show; by morning it was 66%") -- NOT a market flip.
THRESHOLD = 0.60


def _pick_as_of(prediction, odds_rows, as_of, sport_slug, league_slug, corners_reference):
    """The pick the feed WOULD have shown at `as_of`, or None if it would have shown nothing."""
    if prediction is None:
        return None
    by_market = defaultdict(list)
    for o in odds_rows:
        if o.updated_at is not None and o.updated_at <= as_of:
            by_market[o.market.value].append(
                {
                    "bookmaker": o.bookmaker,
                    "updated_at": o.updated_at,
                    "home_odds": o.home_odds,
                    "draw_odds": o.draw_odds,
                    "away_odds": o.away_odds,
                    "line": o.line,
                    "over_odds": o.over_odds,
                    "under_odds": o.under_odds,
                }
            )
    odds_by_market = {m: latest_price_per_bookmaker(by_market.get(m, [])) for m in DB_MARKETS}

    candidates = _all_market_candidates(prediction, odds_by_market, corners_reference)
    if not offers_goals(league_slug):
        candidates = [c for c in candidates if c.market not in NO_DEMONSTRATED_SIGNAL_MARKETS]
    if not offers_corners(league_slug):
        candidates = [c for c in candidates if c.market != "corners_total"]
    operator_suppressed = suppressed_markets_for(league_slug)
    if operator_suppressed:
        candidates = [c for c in candidates if c.market not in operator_suppressed]

    # is_settled=False deliberately: we are reconstructing what an UPCOMING card showed, which
    # is the population a freeze would govern. Judging it by the settled-review rules would
    # measure a different product.
    return _pick_best(candidates, min_probability=None, sport_slug=sport_slug, is_settled=False)


def _identity(pick):
    return None if pick is None else (pick.market, pick.selection, pick.line)


def _score_batch(batch, odds_by_fixture, preds_by_fixture, corners, meta, hours_list, acc):
    stats, deltas, examples = acc
    sport_by, league_by = meta
    for fixture in batch:
        preds = preds_by_fixture.get(fixture.id) or []
        odds_rows = odds_by_fixture.get(fixture.id) or []
        kickoff = fixture.kickoff_utc
        pre_kickoff = [p for p in preds if p.created_at and p.created_at <= kickoff]
        if not pre_kickoff:
            continue

        def latest_before(moment, pool=pre_kickoff):
            eligible = [p for p in pool if p.created_at <= moment]
            return max(eligible, key=_prediction_precedence) if eligible else None

        sport_slug = sport_by[fixture.id]
        league_slug = league_by[fixture.id]
        reference = corners.get(fixture.id)
        final = _pick_as_of(
            latest_before(kickoff), odds_rows, kickoff, sport_slug, league_slug, reference
        )
        for hours in hours_list:
            moment = kickoff - timedelta(hours=hours)
            earlier = _pick_as_of(
                latest_before(moment), odds_rows, moment, sport_slug, league_slug, reference
            )
            if earlier is None and final is None:
                continue
            s = stats[hours]
            s["n"] += 1
            if earlier is None:
                s["appeared"] += 1
                continue
            if final is None:
                s["vanished"] += 1
                continue
            if _identity(earlier) != _identity(final):
                s["flip"] += 1
                if len(examples[hours]) < 6:
                    examples[hours].append(
                        f"{sport_slug}/{league_slug}: "
                        f"{earlier.market}:{earlier.selection} {earlier.probability:.2f}"
                        f" -> {final.market}:{final.selection} {final.probability:.2f}"
                    )
            elif abs(earlier.probability - final.probability) > 1e-9:
                s["moved"] += 1
                deltas[hours].append(abs(earlier.probability - final.probability))
            if (earlier.probability >= THRESHOLD) != (final.probability >= THRESHOLD):
                s["crossed"] += 1


def _report(hours_list, stats, deltas, examples) -> None:
    print("\nHOW OFTEN THE HEADLINE PICK CHANGES BEFORE KICKOFF")
    print("(reconstructed from the append-only prediction series and timestamped odds)\n")
    print(
        f"  {'window':>8}  {'n':>5}  {'market flip':>13}  {'same bet moved':>15}"
        f"  {'crossed 0.60':>14}"
    )
    for hours in hours_list:
        s = stats[hours]
        if not s["n"]:
            print(f"  T-{hours:<6}      no reconstructable cards")
            continue
        d = deltas[hours]
        print(
            f"  T-{hours:<6}  {s['n']:5}  "
            f"{s['flip']:4} ({s['flip'] / s['n'] * 100:5.1f}%)  "
            f"{s['moved']:5} ({s['moved'] / s['n'] * 100:5.1f}%)  "
            f"{s['crossed']:5} ({s['crossed'] / s['n'] * 100:5.1f}%)   "
            f"mean move {(sum(d) / len(d) * 100 if d else 0):.2f}pp"
        )
        if s["appeared"] or s["vanished"]:
            print(
                f"           plus {s['appeared']} card(s) that did not exist yet and "
                f"{s['vanished']} that later lost their pick entirely"
            )
    for hours in hours_list:
        if examples[hours]:
            print(f"\n  example flips inside T-{hours}:")
            for line in examples[hours]:
                print(f"    {line}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default=None, help="restrict to one sport slug")
    parser.add_argument("--hours", type=int, nargs="+", default=list(DEFAULT_HOURS))
    # 1500 rather than everything: see the MEMORY note in the module docstring.
    parser.add_argument("--limit", type=int, default=1500)
    parser.add_argument("--batch", type=int, default=250)
    args = parser.parse_args()

    async with async_session_factory() as db:
        stmt = (
            select(Fixture, Sport.slug, League.slug)
            .join(Sport, Sport.id == Fixture.sport_id)
            .join(League, League.id == Fixture.league_id)
            .where(Fixture.status == FixtureStatus.COMPLETED)
            .order_by(Fixture.kickoff_utc.desc())
            .limit(args.limit)
        )
        if args.sport:
            stmt = stmt.where(Sport.slug == args.sport)
        rows = (await db.execute(stmt)).all()

    fixtures = [r[0] for r in rows]
    meta = ({f.id: s for f, s, _ in rows}, {f.id: le for f, _, le in rows})
    if not fixtures:
        print("no settled fixtures matched")
        await engine.dispose()
        return

    stats = {
        h: {"n": 0, "flip": 0, "moved": 0, "appeared": 0, "vanished": 0, "crossed": 0}
        for h in args.hours
    }
    acc = (stats, defaultdict(list), defaultdict(list))

    batches = [fixtures[i : i + args.batch] for i in range(0, len(fixtures), args.batch)]
    print(f"reconstructing {len(fixtures)} settled cards in {len(batches)} batch(es)...")
    for number, batch in enumerate(batches, start=1):
        batch_ids = [f.id for f in batch]
        async with async_session_factory() as db:
            odds_by_fixture = defaultdict(list)
            for o in (
                await db.execute(select(Odds).where(Odds.fixture_id.in_(batch_ids)))
            ).scalars():
                odds_by_fixture[o.fixture_id].append(o)
            preds_by_fixture = defaultdict(list)
            for p in (
                await db.execute(select(Prediction).where(Prediction.fixture_id.in_(batch_ids)))
            ).scalars():
                preds_by_fixture[p.fixture_id].append(p)
            corners = await bulk_corners_reference(db, batch)
        _score_batch(batch, odds_by_fixture, preds_by_fixture, corners, meta, args.hours, acc)
        print(f"  batch {number}/{len(batches)} done")

    _report(args.hours, *acc)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
