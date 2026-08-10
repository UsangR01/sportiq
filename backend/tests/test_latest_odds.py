"""Best odds must be a price someone can still take, not a high-water mark.

Odds rows are append-only snapshots by design, so a fixture accumulates 8.7 rows per bookmaker
on average and up to 47. Feeding all of them to best_available_odds made "best odds" the
maximum ever seen rather than the maximum currently offered.

Measured over 119 real fixtures with two or more price rows: 28% overstated, by 5.89% on
average and 55% at worst. That number is not cosmetic -- it is the price on the card, the odds
in the expected-value calculation, and the value the min_odds filter tests, so a fixture could
be surfaced on a price that had already gone.
"""

from datetime import UTC, datetime, timedelta

from app.picks.service import best_available_odds, latest_price_per_bookmaker

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _row(bookmaker, home, when, line=None):
    return {
        "bookmaker": bookmaker,
        "updated_at": when,
        "home_odds": home,
        "draw_odds": None,
        "away_odds": 2.0,
        "line": line,
        "over_odds": None,
        "under_odds": None,
    }


def test_a_price_that_has_since_shortened_is_not_offered_as_best():
    """THE case. One book drifted 2.50 -> 1.80; the old behaviour kept quoting 2.50 forever."""
    rows = [
        _row("BookA", 2.50, NOW - timedelta(hours=6)),
        _row("BookA", 1.80, NOW),
        _row("BookB", 1.90, NOW),
    ]
    assert best_available_odds(latest_price_per_bookmaker(rows))["home"] == 1.90


def test_names_differing_only_by_case_are_one_bookmaker():
    """TheRundown writes "draftkings", API-Football "Draftkings". Counted separately, one book
    contributed twice to the consensus median that decides which rows are inverted."""
    rows = [
        _row("draftkings", 2.10, NOW - timedelta(hours=2)),
        _row("Draftkings", 1.95, NOW),
    ]
    assert len(latest_price_per_bookmaker(rows)) == 1
    assert latest_price_per_bookmaker(rows)[0]["home_odds"] == 1.95


def test_different_lines_from_one_book_are_kept_apart():
    """A totals book quotes several lines at once; collapsing them to one would discard real,
    simultaneously-available prices rather than stale ones."""
    rows = [_row("BookA", None, NOW, line=2.5), _row("BookA", None, NOW, line=3.5)]
    assert len(latest_price_per_bookmaker(rows)) == 2


def test_an_untimed_row_is_kept_rather_than_dropped():
    """A price with no timestamp is still a real quote. Dropping it would silently shrink
    coverage for a sport or provider that does not stamp its rows."""
    assert len(latest_price_per_bookmaker([_row("BookA", 2.0, None)])) == 1
