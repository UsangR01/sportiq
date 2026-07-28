"""Unit tests for the Big3/Top5 key player availability feature (TDD §3.3).

Covers both stages: Stage 1's pure ranking/scoring functions (no network/DB), and Stage 2's
DB-backed availability lookup (conftest pattern, like test_team_upsert.py). The last test is
the one the acceptance criteria specifically require: proof that Stage 2 follows
player_injury_status and diverges from what a box-score-based implementation would say.
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
from app.models_ml.nba_key_players import (
    compute_uper,
    compute_ws48_approx,
    get_key_player_availability,
    select_top5,
)
from app.sports.models import League, Sport

# ml/training/ isn't normally on backend's import path — inserted here (mirroring how the
# ml/ scripts insert backend/ onto theirs) specifically to reuse historical_key_player_
# availability in the leakage-guard test below, proving the two paths genuinely diverge.
ML_TRAINING_DIR = Path(__file__).resolve().parents[2] / "ml" / "training"
if str(ML_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(ML_TRAINING_DIR))
from train_nba import historical_key_player_availability, index_played_names  # noqa: E402

# --- Stage 1: pure functions -------------------------------------------------------------


def test_compute_uper_matches_league_average_scales_to_15():
    # If a player's raw per-48 value equals the league average, PER should land exactly at
    # PER's own defining convention: 15.0 (league-average).
    player_row = {
        "MIN": 30.0,
        "PTS": 20.0,
        "REB": 5.0,
        "AST": 5.0,
        "STL": 1.0,
        "BLK": 1.0,
        "FGA": 15.0,
        "FGM": 8.0,
        "FTA": 4.0,
        "FTM": 3.0,
        "TOV": 2.0,
        "PF": 2.0,
    }
    raw = (20 + 5 + 5 + 1 + 1) - (15 - 8) - (4 - 3) - 2 - 0.5 * 2
    league_avg_raw_per48 = raw / 30.0 * 48.0
    assert compute_uper(player_row, league_avg_raw_per48) == pytest.approx(15.0)


def test_compute_uper_zero_minutes_is_zero():
    assert compute_uper({"MIN": 0.0}, league_avg_raw_per48=10.0) == 0.0


def test_compute_ws48_approx_scales_with_pie_and_win_pct():
    low_win = compute_ws48_approx({"PIE": 0.10}, team_win_pct=0.3)
    high_win = compute_ws48_approx({"PIE": 0.10}, team_win_pct=0.8)
    assert high_win > low_win  # same PIE, better team -> higher WS/48 approximation
    assert compute_ws48_approx({"PIE": None}, team_win_pct=0.5) == 0.0


def test_select_top5_from_high_mpg_pool():
    players = [
        {"player_id": str(i), "player_name": f"P{i}", "mpg": 30.0, "ws_48": float(i)}
        for i in range(6)
    ]
    top5 = select_top5(players)
    assert len(top5) == 5
    assert [p["player_name"] for p in top5] == ["P5", "P4", "P3", "P2", "P1"]  # ranked desc


def test_select_top5_falls_back_to_mid_band_when_high_pool_short():
    high_pool = [
        {"player_id": "h1", "player_name": "High1", "mpg": 28.0, "ws_48": 0.20},
        {"player_id": "h2", "player_name": "High2", "mpg": 27.0, "ws_48": 0.18},
    ]
    mid_pool = [
        {"player_id": "m1", "player_name": "Mid1", "mpg": 22.0, "ws_48": 0.25},
        {"player_id": "m2", "player_name": "Mid2", "mpg": 20.0, "ws_48": 0.10},
        {"player_id": "m3", "player_name": "Mid3", "mpg": 19.0, "ws_48": 0.05},
    ]
    low_pool = [{"player_id": "l1", "player_name": "Low1", "mpg": 12.0, "ws_48": 0.99}]

    top5 = select_top5(high_pool + mid_pool + low_pool)
    names = [p["player_name"] for p in top5]
    assert "Low1" not in names  # below 18 MPG — never eligible regardless of ws_48
    assert set(names) == {"High1", "High2", "Mid1", "Mid2", "Mid3"}
    assert names[0] == "Mid1"  # highest ws_48 (0.25) ranks first even though it's from the mid band


def test_select_top5_returns_fewer_than_5_if_pool_is_smaller():
    players = [{"player_id": "1", "player_name": "Solo", "mpg": 30.0, "ws_48": 0.15}]
    assert len(select_top5(players)) == 1


# --- Stage 2: DB-backed availability lookup -----------------------------------------------


@pytest.fixture
async def seeded_team_and_key_players():
    async with async_session_factory() as db:
        slug = f"test-sport-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Test Sport", model_type="test", active=True)
        db.add(sport)
        await db.flush()

        league = League(
            sport_id=sport.id,
            slug="test-league",
            name="Test League",
            country="XX",
            tier=1,
            active=True,
        )
        db.add(league)
        await db.flush()

        team = Team(sport_id=sport.id, league_id=league.id, name="Test Team", short_name="TST")
        db.add(team)
        await db.flush()

        season_year = 2023
        now = datetime.now(UTC)
        players = [
            ("Star One", 20.0),
            ("Star Two", 18.0),
            ("Star Three", 15.0),
            ("Star Four", 12.0),
            ("Star Five", 10.0),
        ]
        for rank, (name, per) in enumerate(players, start=1):
            db.add(
                TeamKeyPlayer(
                    team_id=team.id,
                    season_year=season_year,
                    player_rank=rank,
                    player_id=f"nba-api-{rank}",
                    player_name=name,
                    ws_48=0.15,
                    per=per,
                    mpg=30.0,
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


async def test_all_active_counts_all_five(seeded_team_and_key_players):
    sport, team, season_year = seeded_team_and_key_players
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        for name in ("Star One", "Star Two", "Star Three", "Star Four", "Star Five"):
            db.add(
                PlayerInjuryStatus(
                    sport_id=sport.id,
                    player_id=name,
                    team_id=team.id,
                    player_name=name,
                    status=InjuryStatus.ACTIVE,
                    source=InjurySource.ROTOWIRE,
                    updated_at=now,
                )
            )
        await db.commit()

        available, per_combined = await get_key_player_availability(db, team.id, season_year)
    assert available == 5
    assert per_combined == pytest.approx(20.0 + 18.0 + 15.0 + 12.0 + 10.0)


async def test_out_players_excluded(seeded_team_and_key_players):
    sport, team, season_year = seeded_team_and_key_players
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id="Star One",
                team_id=team.id,
                player_name="Star One",
                status=InjuryStatus.OUT,
                source=InjurySource.ROTOWIRE,
                updated_at=now,
            )
        )
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id="Star Two",
                team_id=team.id,
                player_name="star two",  # case-insensitive match
                status=InjuryStatus.PROBABLE,
                source=InjurySource.ROTOWIRE,
                updated_at=now,
            )
        )
        await db.commit()

        available, per_combined = await get_key_player_availability(db, team.id, season_year)
    # Star One OUT (excluded); Star Two PROBABLE (included); Three/Four/Five have no record
    # at all -> default-available (see app/models_ml/nba_key_players.py).
    assert available == 4
    assert per_combined == pytest.approx(18.0 + 15.0 + 12.0 + 10.0)


async def test_no_injury_record_defaults_available(seeded_team_and_key_players):
    _sport, team, season_year = seeded_team_and_key_players
    async with async_session_factory() as db:
        available, per_combined = await get_key_player_availability(db, team.id, season_year)
    assert available == 5
    assert per_combined == pytest.approx(20.0 + 18.0 + 15.0 + 12.0 + 10.0)


async def test_no_team_key_players_returns_none():
    async with async_session_factory() as db:
        available, per_combined = await get_key_player_availability(db, uuid.uuid4(), 2023)
    assert available is None
    assert per_combined is None


# --- The required leakage-guard test -------------------------------------------------------


async def test_stage2_follows_injury_status_not_box_score(seeded_team_and_key_players):
    """Acceptance criterion: a test that fails if Stage 2 is accidentally computed from box-
    score/lineup data instead of player_injury_status. "Star One" is marked OUT in
    player_injury_status but DID play real minutes in a completed game's box score — the two
    functions must diverge on this exact scenario, or Stage 2 has regressed into using
    box-score presence (target leakage per TDD §3.3's PITFALL)."""
    sport, team, season_year = seeded_team_and_key_players
    now = datetime.now(UTC)

    async with async_session_factory() as db:
        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id="Star One",
                team_id=team.id,
                player_name="Star One",
                status=InjuryStatus.OUT,
                source=InjurySource.ROTOWIRE,
                updated_at=now,
            )
        )
        await db.commit()

        stage2_available, stage2_per = await get_key_player_availability(db, team.id, season_year)

    # Same underlying facts, via the training-only box-score-presence backtest label: "Star
    # One" DID appear with real minutes in this completed game.
    game_log = pd.DataFrame(
        [
            {
                "GAME_ID": "0012300001",
                "TEAM_ABBREVIATION": "TST",
                "PLAYER_NAME": "Star One",
                "MIN": 35.0,
            }
        ]
    )
    played_names_index = index_played_names(game_log)
    key_players_by_team_season = {
        ("TST", season_year): [
            {"player_name": "Star One", "per": 20.0},
            {"player_name": "Star Two", "per": 18.0},
        ]
    }
    hist_available, hist_per = historical_key_player_availability(
        played_names_index, key_players_by_team_season, "TST", season_year, "0012300001"
    )

    # Stage 2 correctly excludes Star One (OUT) -> only 4 of 5 available.
    assert stage2_available == 4
    assert stage2_per == pytest.approx(18.0 + 15.0 + 12.0 + 10.0)

    # The box-score-based backtest label would have said Star One WAS available (they
    # played) — if Stage 2 ever matched this instead of the assertion above, it would mean
    # Stage 2 had regressed into reading box-score/lineup data.
    assert hist_available == 1  # only Star One is in this tiny synthetic game_log at all
    assert hist_per == pytest.approx(20.0)
    assert stage2_available != hist_available
