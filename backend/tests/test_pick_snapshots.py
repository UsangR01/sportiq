"""Capturing the pick as it was SHOWN (workers/snapshot_picks.py).

Product performance was unmeasurable without this: best_pick is computed per request and never
stored, so grading it after the result meant recomputing against today's odds and today's
guards — a different product than users saw. See docs/history-metrics-spec.md §2b.

Two properties are pinned here because breaking either produces a metric that looks fine and
means nothing.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.predictions.models import ConfidenceTier, PickSnapshot, Prediction, PredictionKind
from app.sports.models import League, Sport
from app.workers import snapshot_picks
from app.workers.snapshot_picks import (
    SNAPSHOT_WINDOW_END_HOURS,
    SNAPSHOT_WINDOW_START_HOURS,
    _snapshot_shown_picks,
)


def test_the_capture_window_leaves_room_for_the_market_to_move():
    """THE property that makes CLV mean anything.

    CLV is taken/closing - 1. capture_closing_odds records the closing price 10-45 MINUTES
    before kickoff, so snapshotting the taken price in that same window would make the two
    nearly identical and drive CLV to ~0 regardless of whether the model has an edge — a
    reassuringly neutral number measuring nothing at all.

    The snapshot therefore sits hours out, where a user would realistically act, and the gap
    between the two IS the measurement.
    """
    closing_window_hours = 45 / 60
    assert SNAPSHOT_WINDOW_START_HOURS > closing_window_hours * 4
    assert SNAPSHOT_WINDOW_END_HOURS > SNAPSHOT_WINDOW_START_HOURS


@pytest.fixture
async def fixture_in_window():
    """A scheduled fixture sitting inside the capture window, with a real prediction."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_factory() as db:
        sport = Sport(slug=f"snap-{suffix}", name="Snap", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"snapl-{suffix}", name="L")
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{suffix}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{suffix}")
        db.add_all([home, away])
        await db.flush()
        kickoff = datetime.now(UTC) + timedelta(
            hours=(SNAPSHOT_WINDOW_START_HOURS + SNAPSHOT_WINDOW_END_HOURS) / 2
        )
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_id=f"snapf-{suffix}",
            kickoff_utc=kickoff,
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="snap-test-v1",
                home_prob=0.62,
                draw_prob=0.20,
                away_prob=0.18,
                confidence_tier=ConfidenceTier.HIGH,
                feature_completeness=0.71,
                kind=PredictionKind.PRE_MATCH,
            )
        )
        await db.commit()
        ids = (sport.id, fixture.id)
    yield ids
    async with async_session_factory() as db:
        await db.execute(delete(PickSnapshot).where(PickSnapshot.fixture_id == ids[1]))
        await db.execute(delete(Prediction).where(Prediction.fixture_id == ids[1]))
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids[0]))
        await db.execute(delete(Team).where(Team.sport_id == ids[0]))
        await db.execute(delete(League).where(League.sport_id == ids[0]))
        await db.execute(delete(Sport).where(Sport.id == ids[0]))
        await db.commit()


@pytest.mark.asyncio
async def test_running_twice_captures_one_row(fixture_in_window):
    """Idempotency, and it is not cosmetic: an hourly task against a four-hour window sees the
    same fixture repeatedly, and a duplicate row would inflate the ROI denominator and
    double-count one pick's result."""
    _sport_id, fixture_id = fixture_in_window

    await _snapshot_shown_picks()
    await _snapshot_shown_picks()
    await _snapshot_shown_picks()

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(PickSnapshot).where(PickSnapshot.fixture_id == fixture_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_the_snapshot_records_what_was_shown_and_when(fixture_in_window):
    """Model version is stored so a later retrain cannot be mistaken for the model that made
    the call, and hours_before_kickoff so a pick taken near the close is distinguishable from
    one taken early — the two are different bets."""
    _sport_id, fixture_id = fixture_in_window

    await _snapshot_shown_picks()

    async with async_session_factory() as db:
        row = (
            await db.execute(select(PickSnapshot).where(PickSnapshot.fixture_id == fixture_id))
        ).scalar_one()

    assert row.model_version == "snap-test-v1"
    assert row.market and row.selection
    assert 0.0 < row.probability <= 1.0
    assert SNAPSHOT_WINDOW_START_HOURS <= row.hours_before_kickoff <= SNAPSHOT_WINDOW_END_HOURS


@pytest.mark.asyncio
async def test_a_fixture_outside_the_window_is_not_captured(fixture_in_window):
    """Cost control and correctness together: the task must do nothing when no fixture is due,
    and must not capture a pick days early that bears no relation to what closes."""
    _sport_id, fixture_id = fixture_in_window

    async with async_session_factory() as db:
        fixture = (await db.execute(select(Fixture).where(Fixture.id == fixture_id))).scalar_one()
        fixture.kickoff_utc = datetime.now(UTC) + timedelta(days=6)
        await db.commit()

    await _snapshot_shown_picks()

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(PickSnapshot).where(PickSnapshot.fixture_id == fixture_id)))
            .scalars()
            .all()
        )
    assert rows == []


def test_the_snapshot_reuses_the_feeds_own_selection():
    """The whole point. Reimplementing pick selection here would reintroduce exactly the problem
    this table exists to solve — measuring something other than what users were shown."""
    source = snapshot_picks.__file__
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "_bulk_best_picks" in body
    assert "_pick_best" not in body  # not reimplemented, delegated


@pytest.mark.asyncio
async def test_a_window_with_fixtures_but_no_captures_warns(fixture_in_window, caplog):
    """A run that SEES fixtures and captures none must be distinguishable from a quiet day.

    pick_snapshots is the only permanent record of what was displayed -- best_pick is recomputed
    per request, so a gap here can never be backfilled. Both cases currently log at info and
    look identical, which is how a real gap would stay invisible until someone counted rows
    months later.

    Diagnosed 2026-08-13 from a real question: 59 snapshots existed, all tennis, and zero of the
    six football fixtures since 2026-08-10 had been captured. That turned out NOT to be a bug --
    every one of those windows closed before the job's first run, the last by nine minutes -- but
    nothing in the logs could have said so either way. Four of those six also carried
    feature_completeness 0.16-0.26, under the 0.35 floor, so they would have produced no shown
    pick regardless; correct behaviour, and exactly the case worth being able to see.
    """
    sport_id, fixture_id = fixture_in_window
    async with async_session_factory() as db:
        # Strip the prediction so the feed has nothing to show, reproducing a guard-suppressed
        # fixture without depending on which guard did the suppressing.
        await db.execute(delete(Prediction).where(Prediction.fixture_id == fixture_id))
        await db.commit()

    with caplog.at_level(logging.WARNING, logger="app.workers.snapshot_picks"):
        await _snapshot_shown_picks()

    assert any(
        "captured NONE" in record.message for record in caplog.records
    ), "a window holding fixtures that yielded no picks must warn, not pass silently"
