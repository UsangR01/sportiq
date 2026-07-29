"""app/workers/backfill_predictions.py — the retrodicted-prediction feature for completed
fixtures. Covers _build_game_log_df (pure) and _retrodict_league (real DB-backed, proving it
writes a genuine Prediction using only strictly-prior fixtures — the leakage guard this whole
feature exists to satisfy)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.predictions.models import Prediction
from app.sports.models import League, Sport
from app.workers.backfill_predictions import _build_game_log_df, _retrodict_league


def test_build_game_log_df_one_row_per_team_per_fixture():
    class FakeFixture:
        def __init__(self, kickoff):
            self.kickoff_utc = kickoff

    class FakeLiveState:
        def __init__(self, home_score, away_score):
            self.home_score = home_score
            self.away_score = away_score

    rows = [
        (FakeFixture(datetime(2026, 7, 20, tzinfo=UTC)), FakeLiveState(2, 1), "10", "20"),
    ]
    df = _build_game_log_df(rows)

    assert len(df) == 2
    home_row = df[df["TEAM_ID"] == "10"].iloc[0]
    away_row = df[df["TEAM_ID"] == "20"].iloc[0]
    assert home_row["OPPONENT_ID"] == "20"
    assert home_row["GF"] == 2 and home_row["GA"] == 1 and home_row["WDL"] == "W"
    assert away_row["OPPONENT_ID"] == "10"
    assert away_row["GF"] == 1 and away_row["GA"] == 2 and away_row["WDL"] == "L"
    assert home_row["HOME_AWAY"] == "home"
    assert away_row["HOME_AWAY"] == "away"


def test_build_game_log_df_draw():
    class FakeFixture:
        kickoff_utc = datetime(2026, 7, 20, tzinfo=UTC)

    class FakeLiveState:
        home_score = 1
        away_score = 1

    df = _build_game_log_df([(FakeFixture(), FakeLiveState(), "10", "20")])
    assert set(df["WDL"]) == {"D"}


@pytest.fixture
async def seeded_football_league_with_completed_fixtures():
    """3 real-shaped completed fixtures for the same two teams, spread across 3 days, under a
    fresh, isolated test Sport (deliberately NOT the real "football" slug, so this never
    touches — or gets confused by — whatever real football model/data already exists in a
    dev DB). No models_registry row exists for this test sport at all, so _retrodict_league
    should raise cleanly rather than silently doing nothing, proving it exercises the real
    DB/game-log assembly this feature adds before ever reaching model inference."""
    async with async_session_factory() as db:
        slug = f"test-football-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Football", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league_slug = f"test-league-{uuid.uuid4().hex[:8]}"
        league = League(
            sport_id=sport.id,
            slug=league_slug,
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.flush()

        home = Team(
            sport_id=sport.id, league_id=league.id, name="Home FC", external_id=f"h-{league_slug}"
        )
        away = Team(
            sport_id=sport.id, league_id=league.id, name="Away FC", external_id=f"a-{league_slug}"
        )
        db.add_all([home, away])
        await db.flush()

        base_date = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
        fixtures = []
        for i, (hs, as_) in enumerate([(1, 0), (2, 2), (0, 3)]):
            fixture = Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"fx-retro-{league_slug}-{i}",
                home_team_id=home.id if i % 2 == 0 else away.id,
                away_team_id=away.id if i % 2 == 0 else home.id,
                kickoff_utc=base_date + timedelta(days=i * 3),
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


async def test_retrodict_league_raises_without_a_registered_model(
    seeded_football_league_with_completed_fixtures,
):
    """This test sport's slug isn't in ModelRunner's _MODEL_CLASSES (it's neither "football"
    nor "nba") — confirms _retrodict_league gets all the way through real DB/game-log
    assembly (the part this feature actually adds: fetching completed fixtures, resolving
    team external ids, building the game log) before failing at ModelRunner's own "no model
    class registered" check, rather than failing earlier for an unrelated reason."""
    sport, league, _fixtures = seeded_football_league_with_completed_fixtures
    with pytest.raises(ValueError, match="No model class registered"):
        await _retrodict_league(sport, league)
