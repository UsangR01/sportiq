"""A Time-TBC fixture follows the day forward — but not forever.

A tennis match with no scheduled_time inherits its TOURNAMENT'S START date, so every timeless
match in a ten-day draw is stamped day one and strands further into the past each day. Rolling
it forward is right for a match still to be played.

IT WAS UNBOUNDED, AND THAT CARRIED PHANTOMS. Toby Samuel v J.J. Wolf, stamped 11 August, was
still on the feed on the 24th: BallDontLie keeps reporting it `scheduled` and never withdraws
it, so _reconcile_vanished_fixtures cannot see it (the id is still in the payload) and the
clock sweep exempts placeholders by design. This sweep was the only thing still touching it,
and it was moving it onto each new day.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.ingest_live_scores import (
    MAX_PLACEHOLDER_ROLL_DAYS,
    _roll_forward_stale_placeholders,
)


async def _seed(kickoff, *, estimated=True, tournament_end=None, status=FixtureStatus.SCHEDULED):
    async with async_session_factory() as db:
        tag = uuid.uuid4().hex[:8]
        sport = Sport(slug=f"t-{tag}", name="Tennis", model_type="t", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"atp-{tag}", name="ATP", tier=1, active=True)
        db.add(league)
        await db.flush()
        home = Team(
            sport_id=sport.id, league_id=league.id, name="A", short_name="A", external_id=f"h{tag}"
        )
        away = Team(
            sport_id=sport.id, league_id=league.id, name="B", short_name="B", external_id=f"a{tag}"
        )
        db.add_all([home, away])
        await db.flush()
        fx = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=kickoff,
            status=status,
            season="2026",
            external_id=f"f{tag}",
            kickoff_is_estimated=estimated,
            tournament_end_utc=tournament_end,
        )
        db.add(fx)
        await db.commit()
        return sport.id, fx.id


async def _read(fixture_id):
    async with async_session_factory() as db:
        return (await db.execute(select(Fixture).where(Fixture.id == fixture_id))).scalar_one()


async def _cleanup(sport_id):
    async with async_session_factory() as db:
        await db.execute(delete(Fixture).where(Fixture.sport_id == sport_id))
        await db.execute(delete(Team).where(Team.sport_id == sport_id))
        await db.execute(delete(League).where(League.sport_id == sport_id))
        await db.execute(delete(Sport).where(Sport.id == sport_id))
        await db.commit()


async def test_a_playable_placeholder_still_rolls_forward():
    """The original behaviour must survive: nine Cincinnati fixtures, Djokovic among them, sat
    under "Yesterday" showing an actionable pick because the date was ours, not the provider's."""
    now = datetime.now(UTC)
    sport_id, fid = await _seed(now - timedelta(days=2), tournament_end=now + timedelta(days=5))
    try:
        await _roll_forward_stale_placeholders()
        fx = await _read(fid)
        assert fx.kickoff_utc.date() == now.date()
        assert fx.kickoff_is_estimated is True, "it must still say Time TBC"
        assert fx.status is FixtureStatus.SCHEDULED
    finally:
        await _cleanup(sport_id)


async def test_a_fixture_is_not_rolled_past_its_own_tournament():
    """THE EXACT BOUND. A match cannot be played after the competition has finished, and the
    provider embeds that date in every match response."""
    now = datetime.now(UTC)
    sport_id, fid = await _seed(now - timedelta(days=3), tournament_end=now - timedelta(days=1))
    try:
        await _roll_forward_stale_placeholders()
        fx = await _read(fid)
        assert fx.kickoff_utc.date() != now.date(), "a finished tournament must not roll forward"
    finally:
        await _cleanup(sport_id)


async def test_a_phantom_past_the_duration_bound_is_retired_and_hidden():
    """The Toby Samuel case, for rows with no known tournament end. 16 days is derived: of 64
    ATP events the longest runs exactly 14 (the Slams), plus two for a delayed finish."""
    now = datetime.now(UTC)
    sport_id, fid = await _seed(now - timedelta(days=MAX_PLACEHOLDER_ROLL_DAYS + 2))
    try:
        await _roll_forward_stale_placeholders()
        fx = await _read(fid)
        assert fx.status is FixtureStatus.POSTPONED
        assert fx.withdrawn is True, "it was never a real scheduled match, so the feed hides it"
    finally:
        await _cleanup(sport_id)


async def test_a_real_kickoff_is_never_touched():
    """The provider owns the schedule for a match it has actually timed; a genuinely missed one
    is the clock sweep's business, not this."""
    now = datetime.now(UTC)
    original = now - timedelta(days=30)
    sport_id, fid = await _seed(original, estimated=False)
    try:
        await _roll_forward_stale_placeholders()
        fx = await _read(fid)
        assert fx.kickoff_utc.date() == original.date()
        assert fx.status is FixtureStatus.SCHEDULED
    finally:
        await _cleanup(sport_id)
