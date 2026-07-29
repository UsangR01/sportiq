"""Unit tests for football's Big3/Top5 key player availability feature (TDD §3.3) — the
direct analogue of test_nba_key_players.py, covering Stage 1's pure select_top5 (season-total-
minutes gating instead of NBA's per-game MPG bands) and the required leakage-guard proof that
Stage 2 (shared with NBA — app/models_ml/key_player_availability.py) follows
player_injury_status, not lineup/box-score presence.
"""

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import InjurySource, InjuryStatus, PlayerInjuryStatus, Team, TeamKeyPlayer
from app.models_ml.football_key_players import select_top5
from app.models_ml.key_player_availability import get_key_player_availability
from app.sports.models import League, Sport

# ml/training/ isn't normally on backend's import path — inserted here (mirroring
# test_nba_key_players.py's own precedent) specifically to reuse
# historical_key_player_availability in the leakage-guard test below.
ML_TRAINING_DIR = Path(__file__).resolve().parents[2] / "ml" / "training"
if str(ML_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(ML_TRAINING_DIR))
from train_football import historical_key_player_availability, index_played_names  # noqa: E402

# --- Stage 1: pure functions -------------------------------------------------------------


def test_select_top5_from_high_minutes_pool():
    players = [
        {"player_id": str(i), "player_name": f"P{i}", "minutes": 1500.0, "rating": float(i)}
        for i in range(6)
    ]
    top5 = select_top5(players)
    assert len(top5) == 5
    assert [p["player_name"] for p in top5] == ["P5", "P4", "P3", "P2", "P1"]  # ranked desc


def test_select_top5_falls_back_to_mid_band_when_high_pool_short():
    high_pool = [
        {"player_id": "h1", "player_name": "High1", "minutes": 1000.0, "rating": 7.2},
        {"player_id": "h2", "player_name": "High2", "minutes": 950.0, "rating": 7.0},
    ]
    mid_pool = [
        {"player_id": "m1", "player_name": "Mid1", "minutes": 700.0, "rating": 7.8},
        {"player_id": "m2", "player_name": "Mid2", "minutes": 500.0, "rating": 6.9},
        {"player_id": "m3", "player_name": "Mid3", "minutes": 460.0, "rating": 6.5},
    ]
    low_pool = [{"player_id": "l1", "player_name": "Low1", "minutes": 200.0, "rating": 9.9}]

    top5 = select_top5(high_pool + mid_pool + low_pool)
    names = [p["player_name"] for p in top5]
    assert "Low1" not in names  # below 450 minutes — never eligible regardless of rating
    assert set(names) == {"High1", "High2", "Mid1", "Mid2", "Mid3"}
    assert names[0] == "Mid1"  # highest rating (7.8) ranks first even from the mid band


def test_select_top5_returns_fewer_than_5_if_pool_is_smaller():
    players = [{"player_id": "1", "player_name": "Solo", "minutes": 1200.0, "rating": 7.5}]
    assert len(select_top5(players)) == 1


# --- Stage 2 (shared with NBA): leakage guard ----------------------------------------------


@pytest.fixture
async def seeded_football_team_and_key_players():
    async with async_session_factory() as db:
        slug = f"test-football-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Football", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league = League(
            sport_id=sport.id, slug="test-epl", name="Test EPL", country="XX", tier=1, active=True
        )
        db.add(league)
        await db.flush()

        team = Team(
            sport_id=sport.id, league_id=league.id, name="Test FC", external_id="test-team-33"
        )
        db.add(team)
        await db.flush()

        season_year = 2025
        now = datetime.now(UTC)
        players = [("Striker One", 8.1), ("Mid Two", 7.6), ("Def Three", 7.2)]
        for rank, (name, rating) in enumerate(players, start=1):
            db.add(
                TeamKeyPlayer(
                    team_id=team.id,
                    season_year=season_year,
                    player_rank=rank,
                    player_id=f"api-football-{rank}",
                    player_name=name,
                    rank_metric=rating,
                    combined_metric=rating,
                    mpg=85.0,
                    computed_at=now,
                )
            )
        await db.commit()
        await db.refresh(team)

    yield sport, team, season_year

    async with async_session_factory() as db:
        await db.execute(delete(PlayerInjuryStatus).where(PlayerInjuryStatus.sport_id == sport.id))
        await db.execute(delete(TeamKeyPlayer).where(TeamKeyPlayer.team_id == team.id))
        await db.execute(delete(Team).where(Team.id == team.id))
        await db.execute(delete(League).where(League.sport_id == sport.id))
        await db.execute(delete(Sport).where(Sport.id == sport.id))
        await db.commit()


async def test_stage2_follows_injury_status_not_lineup_presence(
    seeded_football_team_and_key_players,
):
    """Acceptance criterion (football analogue of test_nba_key_players.py's own): "Striker
    One" is marked OUT in player_injury_status (API_FOOTBALL source) but DID appear with real
    minutes in a completed fixture's lineup — Stage 2 and the historical lineup-presence
    backtest label must diverge on this exact scenario."""
    sport, team, season_year = seeded_football_team_and_key_players
    now = datetime.now(UTC)

    async with async_session_factory() as db:
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id="api-football-1",
                team_id=team.id,
                player_name="Striker One",
                status=InjuryStatus.OUT,
                source=InjurySource.API_FOOTBALL,
                updated_at=now,
            )
        )
        await db.commit()

        stage2_available, stage2_combined = await get_key_player_availability(
            db, team.id, season_year
        )

    lineups = pd.DataFrame(
        [
            {
                "FIXTURE_ID": 555001,
                "TEAM_ID": "test-team-33",
                "PLAYER_NAME": "Striker One",
            }
        ]
    )
    played_names_index = index_played_names(lineups)
    key_players_by_team_season = {
        ("test-team-33", season_year): [
            {"player_name": "Striker One", "combined_metric": 8.1},
            {"player_name": "Mid Two", "combined_metric": 7.6},
            {"player_name": "Def Three", "combined_metric": 7.2},
        ]
    }
    hist_available, hist_combined = historical_key_player_availability(
        played_names_index, key_players_by_team_season, "test-team-33", season_year, 555001
    )

    # Stage 2 correctly excludes Striker One (OUT) -> only 2 of 3 available.
    assert stage2_available == 2
    assert stage2_combined == pytest.approx(7.6 + 7.2)

    # The lineup-presence-based backtest label would say Striker One WAS available (they
    # played) — if Stage 2 ever matched this, it would mean Stage 2 had regressed into reading
    # lineup/box-score data (target leakage per TDD §3.3's PITFALL).
    assert hist_available == 1
    assert hist_combined == pytest.approx(8.1)
    assert stage2_available != hist_available
