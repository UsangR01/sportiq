"""Closing odds, and Closing Line Value.

The closing line is the market's final word on a fixture, and beating it consistently is the
only durable evidence that a model knows something the market does not. Win rate cannot show
this: a model can win 60% of its picks and still lose money if it keeps taking prices shorter
than the close.

WE ALREADY HAVE THE RAW MATERIAL AND NOBODY NOTICED. ingest_odds does a plain insert with no
upsert, so every run appends a new row -- 53,474 rows across 5,655 (fixture, bookmaker, market,
line) combinations, 83% of which carry more than one snapshot, up to 26 for a single
combination. Odds.__doc__ even says snapshots are kept "for line movement analysis"; nothing
had ever read them that way.

THE CLOSE IS NOT SIMPLY THE LAST SNAPSHOT. Measured over the last five days, the latest stored
price for a fixture sits at a MEDIAN of 164 minutes AFTER kickoff -- an in-play price, which
already knows how the match is going. Using it would quietly grade pre-match judgement against
a price informed by the result, the same defect that made BallDontLie's finished-match tennis
odds unusable. So the close here is strictly the last price observed BEFORE kickoff.

Taken per bookmaker rather than per run: a single ingest writes rows carrying the PROVIDER's
own updated_at, so one run's rows do not share a timestamp and "the last batch" is not
well defined. "Each bookmaker's final pre-kickoff price" is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.odds.models import Odds
from app.picks.service import best_available_odds, best_totals_odds


@dataclass(frozen=True)
class ClosingLine:
    """Best price per selection at the close, for one fixture and market."""

    market: str
    line: float | None
    home: float | None = None
    draw: float | None = None
    away: float | None = None
    over: float | None = None
    under: float | None = None

    def price_for(self, selection: str) -> float | None:
        return {
            "home": self.home,
            "draw": self.draw,
            "away": self.away,
            "1X": self.home,
            "X2": self.away,  # double chance reuses the h2h columns
            "over": self.over,
            "under": self.under,
        }.get(selection)


async def bulk_closing_lines(db, fixtures: list) -> dict[uuid.UUID, list[ClosingLine]]:
    """Each fixture's closing lines across every market, from one query.

    Only rows strictly BEFORE kickoff are eligible. A fixture with no pre-kickoff price at all
    returns an empty list rather than falling back to an in-play one -- no closing line is a
    more honest answer than a misleading one.
    """
    if not fixtures:
        return {}
    kickoff_by_id = {f.id: f.kickoff_utc for f in fixtures if f.kickoff_utc is not None}
    if not kickoff_by_id:
        return {}

    rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id.in_(kickoff_by_id.keys()))))
        .scalars()
        .all()
    )

    # (fixture, bookmaker, market, line) -> the latest row strictly before kickoff
    latest: dict[tuple, Odds] = {}
    for row in rows:
        kickoff = kickoff_by_id.get(row.fixture_id)
        updated = row.updated_at
        if kickoff is None or updated is None:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=kickoff.tzinfo)
        if updated >= kickoff:
            continue  # in-play or post-match: it has seen the game
        key = (row.fixture_id, row.bookmaker, row.market.value, row.line)
        current = latest.get(key)
        if current is None or updated > current.updated_at:
            latest[key] = row

    # Group each fixture's final per-bookmaker prices by market, then take the best per side.
    by_market: dict[tuple, list[dict]] = {}
    for (fixture_id, _bookmaker, market, line), row in latest.items():
        by_market.setdefault((fixture_id, market, line), []).append(
            {
                "home_odds": row.home_odds,
                "draw_odds": row.draw_odds,
                "away_odds": row.away_odds,
                "line": row.line,
                "over_odds": row.over_odds,
                "under_odds": row.under_odds,
            }
        )

    result: dict[uuid.UUID, list[ClosingLine]] = {f.id: [] for f in fixtures}
    for (fixture_id, market, line), market_rows in by_market.items():
        if market in ("total", "corners_total"):
            over, under = best_totals_odds(market_rows, line)
            result[fixture_id].append(ClosingLine(market=market, line=line, over=over, under=under))
        else:
            best = best_available_odds(market_rows)
            result[fixture_id].append(
                ClosingLine(
                    market=market,
                    line=line,
                    home=best["home"],
                    draw=best["draw"],
                    away=best["away"],
                )
            )
    return result


def closing_line_value(taken_odds: float | None, closing_odds: float | None) -> float | None:
    """CLV as a fraction: +0.05 means the price taken was 5% longer than the close.

    Positive is good -- the market moved TOWARD the selection after we called it, which is
    evidence the call carried information. Negative means we backed something the market then
    priced more generously, i.e. we were on the wrong side of the move.

    Returns None when either price is missing; a fixture with no closing line is not evidence
    of anything and must not be scored as zero.
    """
    if not taken_odds or not closing_odds or closing_odds <= 1.0 or taken_odds <= 1.0:
        return None
    return taken_odds / closing_odds - 1.0
