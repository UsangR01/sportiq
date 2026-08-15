"""One failing fixture must not cost a whole league its predictions.

WHAT THIS IS GUARDING AGAINST, measured in production 2026-08-15: 96 completed football
fixtures across 13 of 18 leagues showed NO PICK AT ALL -- 35% of every finished card, on a
screen users open specifically to see whether their pick came in.

The mechanism was structural rather than exotic. _retrodict_league looped over completed
fixtures adding Prediction rows, with `await db.commit()` AFTER the loop and no per-fixture
guard. Any failure inside the loop -- a live fetch_lineup_presence timeout, an unparseable
season, a feature the model rejects -- discarded every prediction the league had produced, then
surfaced to _ingest_fixtures's per-league `except (httpx.HTTPError, ValueError)` as a single
warning line. The next day's run failed identically.

Production is where it bites hardest, and that is not a coincidence: DATA_DIR resolves to
/ml/data inside the image and does not exist, so every fixture takes the live lineup-fetch path
rather than reading a cached parquet.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.predictions.models import Prediction
from app.sports.models import League, Sport
from app.workers import backfill_predictions as bp


@pytest.fixture
async def league_with_three_completed_fixtures():
    async with async_session_factory() as db:
        slug = f"test-retro-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Football", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"lg-{slug}", name="L", country="XX", tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h-{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a-{slug}")
        db.add_all([home, away])
        await db.flush()

        fixtures = []
        for i, (hs, as_) in enumerate([(1, 0), (2, 2), (0, 3)]):
            fixture = Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"fx-{slug}-{i}",
                home_team_id=home.id if i % 2 == 0 else away.id,
                away_team_id=away.id if i % 2 == 0 else home.id,
                kickoff_utc=datetime(2026, 7, 20, 15, 0, tzinfo=UTC) + timedelta(days=i * 3),
                status=FixtureStatus.COMPLETED,
                season="2026",
            )
            db.add(fixture)
            await db.flush()
            db.add(
                FixtureLiveState(
                    fixture_id=fixture.id,
                    home_score=hs,
                    away_score=as_,
                    status="completed",
                    last_updated_utc=datetime.now(UTC),
                )
            )
            fixtures.append(fixture)
        await db.commit()
        for f in fixtures:
            await db.refresh(f)
        await db.refresh(league)
        await db.refresh(sport)

    yield sport, league, fixtures

    async with async_session_factory() as db:
        await db.execute(
            delete(Prediction).where(Prediction.fixture_id.in_([f.id for f in fixtures]))
        )
        await db.execute(
            delete(FixtureLiveState).where(
                FixtureLiveState.fixture_id.in_([f.id for f in fixtures])
            )
        )
        await db.execute(delete(Fixture).where(Fixture.league_id == league.id))
        await db.execute(delete(Team).where(Team.league_id == league.id))
        await db.execute(delete(League).where(League.id == league.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


class FakeResult:
    home_prob = 0.5
    draw_prob = 0.25
    away_prob = 0.25
    xg_home = 1.2
    xg_away = 1.0
    corners_xg_home = 5.0
    corners_xg_away = 4.5


class FakeModel:
    version = "test-model-v1"

    def predict(self, features):
        return FakeResult()


async def test_one_failing_fixture_does_not_cost_the_league_its_predictions(
    league_with_three_completed_fixtures, monkeypatch
):
    """THE REGRESSION. Before the fix this committed NOTHING: the exception escaped the loop,
    the session was never committed, and every prediction the league had produced was lost."""
    sport, league, fixtures = league_with_three_completed_fixtures

    async def fake_get_model(db, sport_id):
        return FakeModel()

    async def no_lineups(cached_lineups, fixture_external_id):
        # The provider boundary is mocked, per this project's convention. Without it these
        # synthetic fixture ids reach API-Football and come back "The Fixture field must
        # contain an integer" -- which is itself a neat demonstration of the failure being
        # guarded here, but not something a test should depend on.
        return {}

    monkeypatch.setattr(bp._model_runner, "get_model", fake_get_model)
    monkeypatch.setattr(bp, "_lineup_presence_for_fixture", no_lineups)

    real_one = bp._retrodict_one
    doomed = fixtures[1].external_id

    async def flaky(db, model, fixture, *args, **kwargs):
        if fixture.external_id == doomed:
            raise RuntimeError("provider timed out for this fixture")
        return await real_one(db, model, fixture, *args, **kwargs)

    monkeypatch.setattr(bp, "_retrodict_one", flaky)

    await bp._retrodict_league(sport, league)

    async with async_session_factory() as db:
        written = (
            (
                await db.execute(
                    select(Prediction.fixture_id).where(
                        Prediction.fixture_id.in_([f.id for f in fixtures])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(written) == 2, "the two healthy fixtures must survive their neighbour failing"
    assert fixtures[1].id not in written


async def test_every_fixture_is_retrodicted_when_nothing_fails(
    league_with_three_completed_fixtures, monkeypatch
):
    sport, league, fixtures = league_with_three_completed_fixtures

    async def fake_get_model(db, sport_id):
        return FakeModel()

    async def no_lineups(cached_lineups, fixture_external_id):
        # The provider boundary is mocked, per this project's convention. Without it these
        # synthetic fixture ids reach API-Football and come back "The Fixture field must
        # contain an integer" -- which is itself a neat demonstration of the failure being
        # guarded here, but not something a test should depend on.
        return {}

    monkeypatch.setattr(bp._model_runner, "get_model", fake_get_model)
    monkeypatch.setattr(bp, "_lineup_presence_for_fixture", no_lineups)

    await bp._retrodict_league(sport, league)

    async with async_session_factory() as db:
        written = (
            (
                await db.execute(
                    select(Prediction.fixture_id).where(
                        Prediction.fixture_id.in_([f.id for f in fixtures])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(written) == len(fixtures)


async def test_a_run_that_writes_nothing_is_logged_as_a_warning(
    league_with_three_completed_fixtures, monkeypatch, caplog
):
    """Silence is how this survived unnoticed. A retrodiction that produces nothing must be
    distinguishable in the logs from one that had nothing to do."""
    sport, league, fixtures = league_with_three_completed_fixtures

    async def fake_get_model(db, sport_id):
        return FakeModel()

    async def always_fails(*args, **kwargs):
        raise RuntimeError("every fixture fails")

    monkeypatch.setattr(bp._model_runner, "get_model", fake_get_model)
    monkeypatch.setattr(bp, "_retrodict_one", always_fails)

    with caplog.at_level("WARNING"):
        await bp._retrodict_league(sport, league)

    summaries = [r for r in caplog.records if "retrodiction" in r.getMessage()]
    assert summaries, "a run writing zero predictions must say so"
    assert any(r.levelname == "WARNING" for r in summaries)
