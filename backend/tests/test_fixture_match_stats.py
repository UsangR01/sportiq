"""The Match Stats panel: what actually happened in a match that has been played.

Asked for so a settled prediction can be read against the result rather than taken on trust --
the card already shows a tick or a cross, and a corners pick that settled at 9 renders exactly
like one that settled at 14.

The guards here are the ones where being wrong is invisible: a panel that quietly disagrees
with the score printed above it, or one that goes stale in a way nobody notices.
"""

import pytest

from app.fixtures.match_stats_cache import match_stats_cache_key
from app.fixtures.models import FixtureStatus
from app.fixtures.router import _fetch_match_stats, _football_stat_rows


class FakeLiveState:
    def __init__(self, home_score=None, away_score=None, home_corners=None, away_corners=None):
        self.home_score = home_score
        self.away_score = away_score
        self.home_corners = home_corners
        self.away_corners = away_corners


class FakeFixture:
    def __init__(self, status=FixtureStatus.COMPLETED, external_id="123"):
        self.status = status
        self.external_id = external_id


PROVIDER_HOME = {"corners": 2, "shots": 20, "shots_on_goal": 7, "possession_pct": 40.0}
PROVIDER_AWAY = {"corners": 4, "shots": 13, "shots_on_goal": 4, "possession_pct": 60.0}


def rows_by_label(rows):
    return {r.label: (r.home, r.away) for r in rows}


def test_goals_come_from_our_own_settled_score_not_the_statistics_call():
    """The score is printed directly above this panel. A Goals row sourced from anywhere else
    could disagree with it, and a panel that contradicts the scoreline is worse than no panel."""
    rows = rows_by_label(_football_stat_rows(PROVIDER_HOME, PROVIDER_AWAY, FakeLiveState(4, 1)))

    assert rows["Goals"] == (4.0, 1.0)


def test_stored_corners_win_over_the_providers_own_number():
    """fixture_live_state's counts are what GRADED the corners pick, and for whole leagues they
    are the only counts that exist -- Veikkausliiga has 0% API-Football corner coverage, so its
    values are backfilled from TheStatsAPI. The live call fills a gap; it never overrides a
    settled fact, or the panel would justify a verdict with a different number than produced
    it."""
    rows = rows_by_label(
        _football_stat_rows(
            PROVIDER_HOME, PROVIDER_AWAY, FakeLiveState(1, 0, home_corners=9, away_corners=3)
        )
    )

    assert rows["Corners"] == (9.0, 3.0)


def test_the_provider_fills_corners_only_when_we_have_none_stored():
    rows = rows_by_label(_football_stat_rows(PROVIDER_HOME, PROVIDER_AWAY, FakeLiveState(1, 0)))

    assert rows["Corners"] == (2.0, 4.0)


def test_a_measure_missing_on_both_sides_is_dropped_rather_than_shown_as_dashes():
    """Never a fabricated row. A competition that publishes no possession simply has no
    possession row, rather than a pair of em-dashes implying the fact was looked for and
    found to be zero."""
    no_possession = {**PROVIDER_HOME, "possession_pct": None}
    rows = rows_by_label(
        _football_stat_rows(
            no_possession, {**PROVIDER_AWAY, "possession_pct": None}, FakeLiveState(1, 0)
        )
    )

    assert "Possession" not in rows
    assert "Total shots" in rows


def test_one_sided_data_still_produces_a_row():
    """Only BOTH sides missing drops a row. One side having a real value is information."""
    rows = rows_by_label(
        _football_stat_rows(PROVIDER_HOME, {**PROVIDER_AWAY, "shots": None}, FakeLiveState(1, 0))
    )

    assert rows["Total shots"] == (20.0, None)


def test_no_provider_response_at_all_still_shows_what_we_know():
    """A fixture the provider has no statistics for keeps its goals and its stored corners --
    the rows that came from our own database."""
    rows = rows_by_label(
        _football_stat_rows(None, None, FakeLiveState(2, 2, home_corners=6, away_corners=5))
    )

    assert rows == {"Goals": (2.0, 2.0), "Corners": (6.0, 5.0)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [FixtureStatus.SCHEDULED, FixtureStatus.LIVE, FixtureStatus.POSTPONED]
)
async def test_only_a_completed_fixture_gets_a_panel(status):
    """LIVE is the one that matters. The panel is cached for 30 days on the grounds that a
    played match's statistics are immutable -- which is false mid-match, so caching a live
    fixture would freeze a score. db is None deliberately: nothing may be queried before this
    gate."""
    assert await _fetch_match_stats(None, "football", "epl", FakeFixture(status=status), None) == []


@pytest.mark.asyncio
async def test_basketball_has_no_panel_and_spends_no_call():
    """BallDontLie's /stats is 401 on this plan, so the final score -- already shown above the
    panel -- is the only real per-match number. Returning before the cache also keeps the
    keyspace to the sports that use it. db is None: reaching a query would raise."""
    assert await _fetch_match_stats(None, "nba", "wnba", FakeFixture(), None) == []


@pytest.mark.asyncio
async def test_a_fixture_with_no_external_id_is_not_looked_up():
    assert (
        await _fetch_match_stats(None, "football", "epl", FakeFixture(external_id=None), None) == []
    )


def test_the_cache_key_is_scoped_by_sport():
    """Providers number fixtures independently and both use bare integers, so an API-Football
    id and a BallDontLie id can genuinely collide. Without the sport in the key, one sport's
    payload would be served for the other's fixture -- the same hazard the wnba: prefix exists
    to prevent."""
    assert match_stats_cache_key("football", "123") != match_stats_cache_key("tennis", "123")
