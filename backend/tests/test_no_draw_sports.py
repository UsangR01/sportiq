"""A sport that cannot draw must never have a drawn Outcome settled.

_maybe_settle_outcome derived the result from the score alone and fell through to
MatchResult.DRAW whenever the two sides were level. That is right for football and wrong for
tennis, where a tie in COMPLETED sets is a retirement (6-1, 6-7, 0-2 ret. is 1-1) and there is
still a real winner. Twelve impossible rows accumulated before the guard existed.

Not settling is deliberate. An absent Outcome is the honest state and stays settleable if a
real winner signal ever appears; a wrong one would have to be found and corrected first.

There is no usable winner signal today, and that was measured rather than assumed. BallDontLie
exposes no retirement marker, and its habit of listing the winner as player1 holds 100% on
settled 2022/2025 data via the list endpoint but only 68% on the current season and 48% via
/matches/{id} -- so it fails exactly where it would be needed.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.base import FixturePayload
from app.workers.ingest_fixtures import SPORTS_WITHOUT_DRAWS, _maybe_settle_outcome


def _payload(home_score, away_score):
    return FixturePayload(
        external_id="atp:123",
        league_external_id="atp",
        home_team_external_id="atp:1",
        away_team_external_id="atp:2",
        season="2026",
        home_team_name="A",
        away_team_name="B",
        home_team_short_name="A",
        away_team_short_name="B",
        kickoff_utc=datetime.now(UTC),
        status="completed",
        home_score=home_score,
        away_score=away_score,
    )


def _team():
    # external_id is None so the football path's corner-stat fetch short-circuits before making
    # a real API call — this test is about settlement, not about corners.
    return SimpleNamespace(elo_rating=1500.0, external_id=None)


class _Db:
    """Records what would have been written, without touching a database."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *_args, **_kwargs):
        result = AsyncMock()
        result.scalar_one_or_none = lambda: None  # no existing Outcome
        return result


@pytest.mark.asyncio
async def test_a_tied_tennis_score_settles_nothing():
    """THE guard. 1-1 in completed sets is a retirement, not a draw."""
    db, home, away = _Db(), _team(), _team()
    await _maybe_settle_outcome(db, "fix-1", _payload(1, 1), home, away, "tennis")
    assert db.added == []


@pytest.mark.asyncio
async def test_a_tied_tennis_score_also_leaves_elo_alone():
    """A draw-shaped Elo update for a match that had a winner is wrong in its own right, and
    Elo is applied in the same function -- so the early return has to cover both."""
    db, home, away = _Db(), _team(), _team()
    await _maybe_settle_outcome(db, "fix-2", _payload(0, 0), home, away, "tennis")
    assert (home.elo_rating, away.elo_rating) == (1500.0, 1500.0)


@pytest.mark.asyncio
async def test_a_decisive_tennis_score_still_settles_normally():
    """The guard must be narrow: only ties are unresolvable, and tennis settlement otherwise
    works exactly as before."""
    db, home, away = _Db(), _team(), _team()
    await _maybe_settle_outcome(db, "fix-3", _payload(2, 0), home, away, "tennis")
    assert len(db.added) == 1
    assert db.added[0].result.value == "home_win"
    assert home.elo_rating > 1500.0 > away.elo_rating


@pytest.mark.asyncio
async def test_football_can_still_draw():
    """Football draws are real and common -- roughly a quarter of matches. Suppressing them
    would silently discard a whole outcome class."""
    db, home, away = _Db(), _team(), _team()
    await _maybe_settle_outcome(db, "fix-4", _payload(1, 1), home, away, "football")
    assert len(db.added) == 1
    assert db.added[0].result.value == "draw"


def test_the_no_draw_set_contains_the_sports_that_cannot_draw():
    assert "tennis" in SPORTS_WITHOUT_DRAWS
    assert "nba" in SPORTS_WITHOUT_DRAWS  # goes to overtime rather than ending level
    assert "football" not in SPORTS_WITHOUT_DRAWS
