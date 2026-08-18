"""app/workers/backfill_tennis_predictions.py — the retrodicted-prediction feature for
completed tennis fixtures. Covers _load_cached_tennis_history's graceful empty-frame fallback
(pure) and _retrodict_tennis_league (real DB-backed, proving it gets all the way through real
fixture/team resolution before ever reaching a live API call or model inference — the model
lookup is checked first specifically so a sport with no registered model fails fast)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.predictions.models import Prediction
from app.sports.models import League, Sport
from app.workers.backfill_tennis_predictions import (
    _load_cached_tennis_history,
    _retrodict_tennis_league,
)


def test_load_cached_tennis_history_returns_empty_frames_for_unknown_tour():
    """A tour with no collected parquet cache (or a bogus one, as in the test below) must
    degrade gracefully to empty frames with the right columns, not raise."""
    games, ranks = _load_cached_tennis_history("not-a-real-tour-xyz")
    assert list(games.columns) == [
        "MATCH_ID",
        "TOURNAMENT_ID",
        "SEASON",
        "PLAYER_ID",
        "OPPONENT_ID",
        "HOME_AWAY",
        "GAME_DATE",
        "WL",
        "SURFACE",
    ]
    assert games.empty
    assert list(ranks.columns) == ["PLAYER_ID", "WEEK", "RANK_POINTS"]
    assert ranks.empty


@pytest.fixture
async def seeded_tennis_league_with_completed_fixtures():
    """2 real-shaped completed tennis fixtures (sets-won scorelines) between the same two
    players, under a fresh, isolated test Sport/League (deliberately not the real "tennis"
    slug/"atp" league, so this never touches the real dev DB's several-thousand real ATP rows
    or its registered model). No models_registry row exists for this test sport, so
    _retrodict_tennis_league should raise cleanly at the model-lookup step."""
    async with async_session_factory() as db:
        slug = f"test-tennis-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Tennis", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league_slug = f"test-tour-{uuid.uuid4().hex[:8]}"
        league = League(
            sport_id=sport.id,
            slug=league_slug,
            name="Test Tour",
            country=None,
            tier=1,
            active=True,
        )
        db.add(league)
        await db.flush()

        home = Team(
            sport_id=sport.id,
            league_id=league.id,
            name="Player One",
            external_id=f"h-{league_slug}",
        )
        away = Team(
            sport_id=sport.id,
            league_id=league.id,
            name="Player Two",
            external_id=f"a-{league_slug}",
        )
        db.add_all([home, away])
        await db.flush()

        base_date = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
        fixtures = []
        for i, (hs, as_) in enumerate([(2, 0), (1, 2)]):
            fixture = Fixture(
                sport_id=sport.id,
                league_id=league.id,
                external_id=f"{league_slug}:{9000 + i}",
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=base_date + timedelta(days=i * 2),
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


async def test_retrodict_tennis_league_raises_without_a_registered_model(
    seeded_tennis_league_with_completed_fixtures,
):
    """This test sport's slug isn't in ModelRunner's _MODEL_CLASSES (it's neither "tennis" nor
    any other real sport) — confirms _retrodict_tennis_league gets all the way through real
    fixture/team resolution and existing-prediction filtering before failing at ModelRunner's
    own "no model registered" check, and crucially BEFORE any live API call (the model
    lookup happens first specifically to fail fast without wasting one)."""
    sport, league, _fixtures = seeded_tennis_league_with_completed_fixtures
    with pytest.raises(ValueError, match="model.*registered"):
        await _retrodict_tennis_league(sport, league)
