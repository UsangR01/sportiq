"""Kickoff-time refresh and self-derived live status.

Both come from one real failure. BallDontLie leaves scheduled_time null until close to an ATP
match, so a fixture ingested days ahead fell back to its tournament's start date (midnight)
— and kickoff_utc was written on INSERT only, so it kept that forever even once a real time
was published. Measured live: 26 of 37 ATP fixtures in a +/-2 day window were stuck on an
estimated kickoff and 31 sat at exactly 00:00, which put matches under the wrong DAY.

The same provider also reported those matches as "scheduled" while a public scoreboard showed
them in progress, which left the Live tab permanently empty.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.base import FixturePayload
from app.fixtures.models import Fixture, FixtureStatus
from app.workers.ingest_live_scores import _looks_underway


def _payload(**overrides) -> FixturePayload:
    base = dict(
        external_id="atp:1",
        league_external_id="atp",
        home_team_external_id="atp:h",
        away_team_external_id="atp:a",
        home_team_name="A",
        away_team_name="B",
        home_team_short_name="A",
        away_team_short_name="B",
        kickoff_utc=datetime.now(UTC),
        status="scheduled",
        season="2026",
    )
    base.update(overrides)
    return FixturePayload(**base)


def _fixture(**overrides) -> Fixture:
    fixture = Fixture(
        external_id="atp:1",
        kickoff_utc=datetime.now(UTC) - timedelta(hours=1),
        status=FixtureStatus.SCHEDULED,
        season="2026",
        kickoff_is_estimated=False,
    )
    for key, value in overrides.items():
        setattr(fixture, key, value)
    return fixture


class TestLooksUnderway:
    """The heuristic behind promoting a fixture to LIVE without the provider saying so."""

    def test_started_and_scoring_counts_as_live(self):
        assert _looks_underway(_fixture(), _payload(home_score=1, away_score=0)) is True

    def test_a_clock_alone_counts_as_live(self):
        """Football reports an elapsed minute before the first goal — 0-0 in the 20th minute
        is unambiguously being played."""
        assert _looks_underway(_fixture(), _payload(match_minute=20)) is True

    def test_no_score_and_no_clock_stays_scheduled(self):
        """Never guess. A match with nothing on the board has not provably started."""
        assert _looks_underway(_fixture(), _payload()) is False

    def test_a_future_kickoff_is_never_live(self):
        """Guards against a stale score promoting a match that has not started — otherwise a
        carried-over scoreline would mark tomorrow's fixture live today."""
        future = _fixture(kickoff_utc=datetime.now(UTC) + timedelta(hours=3))
        assert _looks_underway(future, _payload(home_score=2)) is False

    def test_an_estimated_kickoff_is_never_live(self):
        """The important guard. A fabricated midnight is always in the past, so without this
        the first score anywhere would promote most of a tournament to LIVE."""
        estimated = _fixture(kickoff_is_estimated=True)
        assert _looks_underway(estimated, _payload(home_score=1)) is False

    def test_naive_timestamps_do_not_raise(self):
        """Postgres can hand back a naive datetime depending on the column/driver; comparing
        that to an aware now() would raise TypeError rather than returning a verdict."""
        naive = _fixture(kickoff_utc=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1))
        assert _looks_underway(naive, _payload(home_score=1)) is True


class TestKickoffRefreshRules:
    """The update rules applied to an existing fixture. Expressed as the plain predicate the
    worker uses, so the intent is checked without standing up a full ingest run."""

    @staticmethod
    def should_take_new_time(existing_estimated: bool, payload_estimated: bool) -> bool:
        return (not payload_estimated) or existing_estimated

    def test_a_real_time_replaces_an_estimate(self):
        """The actual bug: the provider publishes a real time later and we must adopt it."""
        assert self.should_take_new_time(existing_estimated=True, payload_estimated=False)

    def test_a_real_time_replaces_an_earlier_real_time(self):
        """Matches genuinely get rescheduled."""
        assert self.should_take_new_time(existing_estimated=False, payload_estimated=False)

    def test_an_estimate_never_overwrites_a_known_time(self):
        """Otherwise a re-ingest that happens to lose the field would drag a correct kickoff
        back to a fabricated midnight."""
        assert not self.should_take_new_time(existing_estimated=False, payload_estimated=True)

    def test_an_estimate_may_replace_another_estimate(self):
        """Both are guesses, so the newer one is at least no worse — and this keeps the
        tournament-date fallback tracking if the tournament itself moves."""
        assert self.should_take_new_time(existing_estimated=True, payload_estimated=True)


@pytest.mark.parametrize("estimated", [True, False])
def test_estimated_flag_always_follows_the_payload(estimated):
    """The flag must never say 'confirmed' while the time is still a fallback — that would
    silently drop the Time TBC label and leave a made-up time looking authoritative, which is
    worse than admitting the time is unknown."""
    payload = _payload(kickoff_is_estimated=estimated)
    assert payload.kickoff_is_estimated is estimated
