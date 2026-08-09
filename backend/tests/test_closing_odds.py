"""Closing odds and CLV (app/odds/closing.py, ingest_odds.capture_closing_odds).

CLV is the only measure that separates a model with an edge from one that wins short-priced
favourites, so the two things that would quietly invalidate it are pinned here:

  * the close must never be an IN-PLAY price. Measured over five days of real data, the latest
    stored price for a fixture sits a median of 164 minutes AFTER kickoff -- a price that
    already knows how the match is going. Grading pre-match judgement against it would flatter
    the model exactly as BallDontLie's finished-match tennis odds did.
  * the capture job must cost NOTHING when no fixture is imminent. A naive 15-minute sweep is
    ~672 requests/day against a 5,000/MONTH allowance -- the arithmetic behind the original
    weeks-long odds outage.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.odds.closing import bulk_closing_lines, closing_line_value
from app.odds.models import Odds, OddsMarket
from app.sports.models import League, Sport


@pytest.fixture(autouse=True)
async def _cleanup():
    """The suite runs against the DEV database; a leftover Sport reaches the app's own
    dropdown. Mirrors tests/test_watchlist.py's teardown."""
    yield
    from sqlalchemy import delete, select

    async with async_session_factory() as db:
        sport_ids = (
            (await db.execute(select(Sport.id).where(Sport.slug.like("clv-%")))).scalars().all()
        )
        if not sport_ids:
            return
        fixture_ids = (
            (await db.execute(select(Fixture.id).where(Fixture.sport_id.in_(sport_ids))))
            .scalars()
            .all()
        )
        if fixture_ids:
            await db.execute(delete(Odds).where(Odds.fixture_id.in_(fixture_ids)))
        await db.execute(delete(Fixture).where(Fixture.sport_id.in_(sport_ids)))
        await db.execute(delete(Team).where(Team.sport_id.in_(sport_ids)))
        await db.execute(delete(League).where(League.sport_id.in_(sport_ids)))
        await db.execute(delete(Sport).where(Sport.id.in_(sport_ids)))
        await db.commit()


def test_clv_arithmetic_and_its_refusals():
    """+0.05 means the price taken was 5% longer than the close."""
    assert closing_line_value(2.10, 2.00) == pytest.approx(0.05)
    assert closing_line_value(1.90, 2.00) == pytest.approx(-0.05)
    # A missing price is not evidence of zero CLV - scoring it as 0.0 would dilute the mean
    # toward "no edge" using fixtures that were never measured at all.
    assert closing_line_value(None, 2.0) is None
    assert closing_line_value(2.0, None) is None
    assert closing_line_value(2.0, 1.0) is None


async def _seed(db, kickoff, prices):
    """prices: list of (bookmaker, minutes_relative_to_kickoff, home_odds)."""
    suffix = uuid.uuid4().hex[:8]
    sport = Sport(slug=f"clv-{suffix}", name="CLV Test", model_type="none")
    db.add(sport)
    await db.flush()
    league = League(sport_id=sport.id, slug=f"clvl-{suffix}", name="L")
    db.add(league)
    await db.flush()
    home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{suffix}")
    away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{suffix}")
    db.add_all([home, away])
    await db.flush()
    fixture = Fixture(
        sport_id=sport.id,
        league_id=league.id,
        home_team_id=home.id,
        away_team_id=away.id,
        external_id=f"clvf-{suffix}",
        kickoff_utc=kickoff,
        status=FixtureStatus.COMPLETED,
        season="2026",
    )
    db.add(fixture)
    await db.flush()
    for bookmaker, minutes, home_odds in prices:
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker=bookmaker,
                market=OddsMarket.H2H,
                home_odds=home_odds,
                draw_odds=3.4,
                away_odds=4.0,
                updated_at=kickoff + timedelta(minutes=minutes),
            )
        )
    await db.commit()
    return fixture


@pytest.mark.asyncio
async def test_in_play_prices_are_never_treated_as_the_close():
    """THE guard. A price stamped after kickoff has seen the match.

    Here the in-play price (2.00 at +90) is both the latest AND the longest, so any
    implementation that simply takes max(updated_at) or the best available price would pick it.
    The close must be 1.50 -- the last price before the whistle.
    """
    kickoff = datetime.now(UTC) - timedelta(days=1)
    async with async_session_factory() as db:
        fixture = await _seed(
            db,
            kickoff,
            [
                ("BookA", -180, 1.60),
                ("BookA", -20, 1.50),  # <- the real close
                ("BookA", +90, 2.00),  # in-play: drifted out because the home side went behind
            ],
        )
        lines = await bulk_closing_lines(db, [fixture])

    h2h = next(line for line in lines[fixture.id] if line.market == "h2h")
    assert h2h.home == 1.50


@pytest.mark.asyncio
async def test_close_is_each_bookmakers_last_pre_kickoff_price():
    """Per bookmaker, not per ingest run: one run writes rows carrying the PROVIDER's own
    updated_at, so a run's rows do not share a timestamp and 'the last batch' is undefined.

    BookA's final price is 1.55 and BookB's is 1.70, so the best available close is 1.70 --
    NOT BookB's earlier 1.80, which had already been superseded."""
    kickoff = datetime.now(UTC) - timedelta(days=1)
    async with async_session_factory() as db:
        fixture = await _seed(
            db,
            kickoff,
            [
                ("BookA", -300, 1.40),
                ("BookA", -30, 1.55),
                ("BookB", -290, 1.80),
                ("BookB", -25, 1.70),
            ],
        )
        lines = await bulk_closing_lines(db, [fixture])

    assert next(line for line in lines[fixture.id] if line.market == "h2h").home == 1.70


@pytest.mark.asyncio
async def test_a_fixture_priced_only_in_play_yields_no_closing_line():
    """No closing line is a more honest answer than an in-play one. Returning the in-play price
    would silently enter a result-aware number into the metric."""
    kickoff = datetime.now(UTC) - timedelta(days=1)
    async with async_session_factory() as db:
        fixture = await _seed(db, kickoff, [("BookA", +5, 1.90), ("BookA", +60, 2.40)])
        lines = await bulk_closing_lines(db, [fixture])

    assert lines[fixture.id] == []


@pytest.mark.asyncio
async def test_capture_spends_nothing_when_no_fixture_is_imminent():
    """The cost guard. Every 15 minutes is only affordable because the task returns without
    touching a provider unless something is actually about to start."""
    from app.workers import ingest_odds as module

    with patch.object(module, "_ingest_odds_for_league", new=AsyncMock()) as ingest:
        with patch.object(module, "async_session_factory") as factory:
            session = AsyncMock()
            result = AsyncMock()
            result.all = lambda: []  # nothing kicking off in the window
            session.execute = AsyncMock(return_value=result)
            factory.return_value.__aenter__.return_value = session
            await module._capture_closing_odds()

    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_asks_only_for_the_imminent_fixtures_own_date():
    """Requesting the whole lookahead window every 15 minutes is what the monthly cap cannot
    survive; the date list must be narrowed to the day the fixture actually kicks off on."""
    from app.workers import ingest_odds as module

    kickoff = datetime.now(UTC) + timedelta(minutes=20)

    class _League:
        id, slug, sport_id = "lg", "test-league", "sp"

    class _Sport:
        id, slug = "sp", "football"

    seen = {}

    async def capture(sport, league, adapters=None, dates=None):
        seen["dates"] = dates

    with patch.object(module, "_ingest_odds_for_league", side_effect=capture):
        with patch.object(module, "async_session_factory") as factory:
            session = AsyncMock()
            calls = {"n": 0}

            def execute(*_a, **_k):
                calls["n"] += 1
                result = AsyncMock()
                if calls["n"] == 1:
                    result.all = lambda: [("lg", kickoff)]
                else:
                    scalars = AsyncMock()
                    scalars.all = lambda: [_League()] if calls["n"] == 2 else [_Sport()]
                    result.scalars = lambda: scalars
                return result

            session.execute = AsyncMock(side_effect=execute)
            factory.return_value.__aenter__.return_value = session
            await module._capture_closing_odds()

    assert seen["dates"] == [kickoff.date()]
