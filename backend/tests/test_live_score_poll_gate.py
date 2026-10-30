"""Live-score polling must skip leagues with nothing in play — and must NOT skip the hard cases.

WHY THIS GATE EXISTS. `_ingest_live_scores` fans out one provider call per league per run at a
5-minute cadence: 288 runs a day. Football alone is 34 leagues, so ungated that is 9,792
API-Football calls/day against a 7,500/day Pro allowance — the whole day's quota spent by one
task before odds, injuries, the daily ingest or retrodiction have had any. Measured on a real
matchday, gating costs 3,319/day instead.

The cadence was sized when the plan was Ultra (75,000/day). Nothing in the comment said so,
the league count grew 9 -> 34, and the allowance shrank: exactly the failure CLAUDE.md records
as "a constraint recorded as an engineering decision should name the budget it assumed".

The SKIPPING is the easy half. These tests exist mostly for the two exemptions, because
getting either wrong silently stops a feed rather than erroring.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.ingest_live_scores import (
    LIVE_POLL_AFTER_KICKOFF_HOURS,
    _leagues_with_play_in_progress,
)


async def _league_with_fixture(*, kickoff, estimated=False, status=FixtureStatus.SCHEDULED):
    async with async_session_factory() as db:
        slug = f"gate-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Gate Test", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"l-{slug}", name="L", country=None, tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{slug}")
        db.add_all([home, away])
        await db.flush()
        db.add(
            Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"fx-{slug}",
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=kickoff,
                kickoff_is_estimated=estimated,
                status=status,
                season="2026",
            )
        )
        await db.commit()
        return league.id


async def _gate(league_id):
    async with async_session_factory() as db:
        return league_id in await _leagues_with_play_in_progress(db, [league_id])


@pytest.mark.asyncio
async def test_a_league_kicking_off_now_is_polled():
    lid = await _league_with_fixture(kickoff=datetime.now(UTC) + timedelta(minutes=20))
    assert await _gate(lid)


@pytest.mark.asyncio
async def test_a_league_with_nothing_for_days_is_skipped():
    """Where the entire saving comes from: most of 34 leagues are idle at any given tick."""
    lid = await _league_with_fixture(kickoff=datetime.now(UTC) + timedelta(days=3))
    assert not await _gate(lid)


@pytest.mark.asyncio
async def test_a_match_already_live_is_polled_however_long_it_has_run():
    """A clock must never stop us polling something we have OBSERVED to be underway.

    If it did, a match that overran its window would stop being updated and could never
    settle — turning a cost fix into a correctness bug.
    """
    long_ago = datetime.now(UTC) - timedelta(hours=LIVE_POLL_AFTER_KICKOFF_HOURS + 12)
    lid = await _league_with_fixture(kickoff=long_ago, status=FixtureStatus.LIVE)
    assert await _gate(lid)


@pytest.mark.asyncio
async def test_a_placeholder_midnight_kickoff_is_not_judged_by_the_clock():
    """THE REGRESSION GUARD, and the reason this file is careful.

    A tennis match with no `scheduled_time` is stored at MIDNIGHT of its match day; that is a
    DATE, not a start time. Judged by a time window it would be pollable only around midnight,
    starving a whole tournament for the rest of the day. This repo has been bitten four times
    by a clock rule applied to a placeholder — most recently inventing nine POSTPONED matches
    that were never postponed — so placeholders are exempt from the clock rather than tuned
    against it.

    Set to midnight of TODAY and evaluated now, which for most of the day is well outside the
    post-kickoff window.
    """
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    lid = await _league_with_fixture(kickoff=midnight, estimated=True)
    assert await _gate(lid)


@pytest.mark.asyncio
async def test_a_completed_fixture_alone_does_not_keep_its_league_polled():
    """Settled means there is nothing left to learn, so it must not hold the league open."""
    lid = await _league_with_fixture(
        kickoff=datetime.now(UTC) - timedelta(minutes=30), status=FixtureStatus.COMPLETED
    )
    assert not await _gate(lid)
