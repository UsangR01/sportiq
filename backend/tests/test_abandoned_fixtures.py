"""Fixtures that were scheduled, never played, and quietly vanished from the provider.

NEITHER PROVIDER EMITS A CANCELLED STATUS FOR THESE — the row simply disappears. Verified
against both live APIs: an ATP match cancelled on 2026-08-05 returns HTTP 404 from
BallDontLie's /matches/{id}, and a Liga I fixture that never happened on 2026-08-10 is absent
from API-Football's list for that date, which still returns the two that did. Ingest only
updates fixtures it can still see, so a vanished one kept SCHEDULED forever and its card went
on showing an active pick, with a probability and a price, days after the match never happened.

The trap this has to avoid is the OPPOSITE error. Tennis fixtures whose provider gives no start
time are stored at midnight with kickoff_is_estimated, so "the kickoff has passed" is true from
the first second of the day for a match that may not start until late evening. Sweeping those
on the same clock would retire most of a live tournament every morning.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.ingest_live_scores import (
    ABANDONED_AFTER_HOURS,
    ABANDONED_AFTER_HOURS_ESTIMATED,
    _mark_abandoned_fixtures,
)


@pytest.fixture
async def world():
    """A sport with four fixtures, one per case the sweep has to tell apart."""
    suffix = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        sport = Sport(slug=f"aband-{suffix}", name="Ab", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"abl-{suffix}", name="L")
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{suffix}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{suffix}")
        db.add_all([home, away])
        await db.flush()

        def make(tag, hours_ago, estimated):
            return Fixture(
                sport_id=sport.id,
                league_id=league.id,
                home_team_id=home.id,
                away_team_id=away.id,
                external_id=f"{tag}-{suffix}",
                kickoff_utc=now - timedelta(hours=hours_ago),
                kickoff_is_estimated=estimated,
                status=FixtureStatus.SCHEDULED,
                season="2026",
            )

        vanished = make("vanished", ABANDONED_AFTER_HOURS + 2, False)
        # Past its own threshold but NOT past the estimated one: a Time-TBC fixture that may
        # genuinely still be played later today.
        tbc = make("tbc", ABANDONED_AFTER_HOURS + 2, True)
        played = make("played", ABANDONED_AFTER_HOURS + 2, False)
        upcoming = make("upcoming", -6, False)  # kicks off in 6 hours
        db.add_all([vanished, tbc, played, upcoming])
        await db.flush()
        # `played` was observed underway, so it must never be retired even though we have no
        # settled outcome for it yet.
        db.add(
            FixtureLiveState(
                fixture_id=played.id,
                home_score=1,
                away_score=0,
                status=FixtureStatus.LIVE,
                last_updated_utc=now,
            )
        )
        await db.commit()
        ids = {
            "sport": sport.id,
            "vanished": vanished.id,
            "tbc": tbc.id,
            "played": played.id,
            "upcoming": upcoming.id,
        }
    yield ids
    async with async_session_factory() as db:
        await db.execute(
            delete(FixtureLiveState).where(FixtureLiveState.fixture_id == ids["played"])
        )
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids["sport"]))
        await db.execute(delete(Team).where(Team.sport_id == ids["sport"]))
        await db.execute(delete(League).where(League.sport_id == ids["sport"]))
        await db.execute(delete(Sport).where(Sport.id == ids["sport"]))
        await db.commit()


async def _status(fixture_id) -> FixtureStatus:
    async with async_session_factory() as db:
        return (
            await db.execute(select(Fixture.status).where(Fixture.id == fixture_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_vanished_fixture_is_retired(world):
    """The reported case: days later the card still showed an active pick on a match that was
    never played. POSTPONED already suppresses best_pick and renders a neutral badge, and
    FixtureStatus documents it as the shared bucket for cancelled/abandoned — so no new status
    and no migration are needed to get the behaviour asked for."""
    await _mark_abandoned_fixtures()
    assert await _status(world["vanished"]) is FixtureStatus.POSTPONED


@pytest.mark.asyncio
async def test_a_time_tbc_fixture_is_left_alone(world):
    """THE property that stops this being worse than the bug it fixes.

    A placeholder midnight kickoff is always "in the past", so one clock for both would retire
    most of a live tournament every morning — removing real picks users could act on.
    """
    await _mark_abandoned_fixtures()
    assert await _status(world["tbc"]) is FixtureStatus.SCHEDULED
    assert ABANDONED_AFTER_HOURS_ESTIMATED > ABANDONED_AFTER_HOURS


@pytest.mark.asyncio
async def test_a_match_seen_underway_is_never_retired(world):
    """A FixtureLiveState row is proof the match happened, even with no settled outcome yet —
    a live game stuck mid-settlement must not be labelled as never played."""
    await _mark_abandoned_fixtures()
    assert await _status(world["played"]) is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_an_upcoming_fixture_is_never_retired(world):
    await _mark_abandoned_fixtures()
    assert await _status(world["upcoming"]) is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_the_sweep_is_idempotent(world):
    """It runs every 5 minutes on the live-scores beat, so it re-examines the same rows
    constantly. Already-POSTPONED fixtures are outside its SCHEDULED filter and cost nothing."""
    await _mark_abandoned_fixtures()
    await _mark_abandoned_fixtures()
    assert await _status(world["vanished"]) is FixtureStatus.POSTPONED
    assert await _status(world["upcoming"]) is FixtureStatus.SCHEDULED
