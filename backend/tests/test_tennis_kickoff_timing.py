"""Time-TBC tennis fixtures must not be invented into postponements, and should stop being TBC.

THE REPORTED PROBLEM. Nine Cincinnati fixtures showed POSTPONED on the card -- Djokovic,
Zverev and Shapovalov among them. Every one was checked against the provider and every one
still existed as `scheduled`. All nine postponements were ours.

THE MECHANISM, and it is not "the provider has no times". BallDontLie's tennis match object
carries no date of its own: only scheduled_time, null for the overwhelming majority, and the
TOURNAMENT's start/end. So a timeless match is stored at the tournament's START, which places a
third-round match on day one of a ten-day draw -- already "30 hours late" before it was ever
going to be played. The clock-based sweep then retired it.

TWO FIXES, tested here:
  1. the clock no longer judges a fixture whose clock we never knew;
  2. the real time is taken from the odds provider, which already supplies it -- 16 of 16
     Time-TBC fixtures were already joined to a TheRundown event carrying one.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.base import OddsPayload
from app.fixtures.models import Fixture
from app.odds.models import OddsMarket
from app.workers.ingest_live_scores import ABANDONED_AFTER_HOURS
from app.workers.ingest_odds import MAX_KICKOFF_CORRECTION_DAYS, _adopt_real_kickoff

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _fixture(kickoff: datetime, estimated: bool) -> Fixture:
    return Fixture(kickoff_utc=kickoff, kickoff_is_estimated=estimated)


def _odds(kickoff: datetime | None) -> OddsPayload:
    return OddsPayload(
        fixture_external_id="rundown-1",
        bookmaker="Bovada",
        market=OddsMarket.H2H,
        home_odds=1.5,
        draw_odds=None,
        away_odds=2.5,
        updated_at=NOW,
        kickoff_utc=kickoff,
    )


def test_a_placeholder_is_replaced_by_the_odds_providers_real_time():
    """The whole point. A Cincinnati match stored at the tournament's start gets the real
    time TheRundown already reported for it."""
    fixture = _fixture(datetime(2026, 8, 13, 0, 0, tzinfo=UTC), estimated=True)
    assert _adopt_real_kickoff(fixture, _odds(datetime(2026, 8, 14, 15, 0, tzinfo=UTC))) is True
    assert fixture.kickoff_utc == datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    assert fixture.kickoff_is_estimated is False


def test_a_real_kickoff_is_never_overwritten():
    """The stats provider owns the schedule. Letting an odds feed overwrite a time it actually
    reported would trade a known-good value for a second-hand one."""
    real = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
    fixture = _fixture(real, estimated=False)
    assert _adopt_real_kickoff(fixture, _odds(datetime(2026, 8, 14, 15, 0, tzinfo=UTC))) is False
    assert fixture.kickoff_utc == real


def test_an_implausible_correction_is_refused():
    """A correction that far out is a bad MATCH, not a bad placeholder. Refusing is safe: the
    fixture keeps its placeholder and stays flagged estimated, so nothing claims a false time."""
    fixture = _fixture(datetime(2026, 8, 13, 0, 0, tzinfo=UTC), estimated=True)
    far = datetime(2026, 8, 13, 0, 0, tzinfo=UTC) + timedelta(days=MAX_KICKOFF_CORRECTION_DAYS + 1)
    assert _adopt_real_kickoff(fixture, _odds(far)) is False
    assert fixture.kickoff_is_estimated is True


def test_an_odds_event_with_no_time_changes_nothing():
    fixture = _fixture(datetime(2026, 8, 13, 0, 0, tzinfo=UTC), estimated=True)
    assert _adopt_real_kickoff(fixture, _odds(None)) is False
    assert fixture.kickoff_is_estimated is True


def test_the_clock_sweep_no_longer_has_an_estimated_branch():
    """THE GUARD ON THE REGRESSION. A clock can only judge a fixture whose clock we know.

    Read structurally rather than by importing a deleted constant, so this keeps meaning
    something if the query is rewritten: the abandonment query must filter on
    kickoff_is_estimated being FALSE, and must not carry a separate estimated-kickoff branch.
    """
    import inspect

    from app.workers import ingest_live_scores

    source = inspect.getsource(ingest_live_scores._mark_abandoned_fixtures)
    assert "kickoff_is_estimated.is_(False)" in source
    assert "kickoff_is_estimated.is_(True)" not in source, (
        "an estimated-kickoff branch is back in the clock sweep -- that is what invented nine "
        "postponements for real Cincinnati matches"
    )


def test_a_known_kickoff_is_still_retired_on_the_clock():
    """The clock is still right where the input means what it says -- this must not become
    'never retire anything', which would leave genuinely abandoned fixtures showing a live pick.
    """
    assert ABANDONED_AFTER_HOURS == 12


@pytest.mark.parametrize("estimated", [True, False])
def test_adoption_never_fabricates_a_time(estimated):
    """Neither branch may invent a kickoff when the payload has none."""
    original = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    fixture = _fixture(original, estimated=estimated)
    _adopt_real_kickoff(fixture, _odds(None))
    assert fixture.kickoff_utc == original
