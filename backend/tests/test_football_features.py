"""app/models_ml/football_features.py — the shared training/serving feature-vector assembly.
Covers the new Elo/streak/richer-H2H additions; the pre-existing rolling-form/rest-days/
win-rate logic already has coverage elsewhere in this file's history (kept minimal here, not
re-tested for behavior that hasn't changed)."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.football_features import (
    CORNERS_FEATURE_NAMES,
    FEATURE_NAMES,
    _corners_rolling_live,
    _xg_rolling,
    assemble_from_game_log,
    merge_corners_into_game_log,
    merge_xg_into_game_log,
)
from app.sports.models import League, Sport


def _row(team, opp, home_away, game_date, gf, ga, wdl):
    return {
        "TEAM_ID": team,
        "OPPONENT_ID": opp,
        "HOME_AWAY": home_away,
        "GAME_DATE": game_date,
        "GF": gf,
        "GA": ga,
        "WDL": wdl,
    }


def test_feature_names_includes_new_features():
    assert "elo_diff" in FEATURE_NAMES
    assert "win_streak_home" in FEATURE_NAMES
    assert "win_streak_away" in FEATURE_NAMES
    assert "h2h_avg_goals_scored_home" in FEATURE_NAMES
    assert "h2h_avg_goals_allowed_home" in FEATURE_NAMES


def test_assemble_from_game_log_passes_through_elo_diff():
    games = pd.DataFrame([_row("A", "B", "home", date(2024, 1, 1), 1, 0, "W")])
    features = assemble_from_game_log(games, date(2024, 1, 8), "A", "B", elo_diff=42.5)
    assert features["elo_diff"] == 42.5


def test_assemble_from_game_log_elo_diff_none_by_default():
    games = pd.DataFrame([_row("A", "B", "home", date(2024, 1, 1), 1, 0, "W")])
    features = assemble_from_game_log(games, date(2024, 1, 8), "A", "B")
    assert features["elo_diff"] is None


def test_assemble_from_game_log_win_streak_home():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "away", date(2024, 1, 8), 2, 0, "W"),
            _row("A", "Z", "home", date(2024, 1, 15), 3, 0, "W"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 22), "A", "B")
    assert features["win_streak_home"] == 3.0


def test_assemble_from_game_log_win_streak_broken_by_loss():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "away", date(2024, 1, 8), 0, 2, "L"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 15), "A", "B")
    assert features["win_streak_home"] == 0.0


def test_assemble_from_game_log_win_streak_none_with_no_history():
    games = pd.DataFrame(
        [], columns=["TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GAME_DATE", "GF", "GA", "WDL"]
    )
    features = assemble_from_game_log(games, date(2024, 1, 22), "A", "B")
    assert features["win_streak_home"] is None
    assert features["win_streak_away"] is None


def test_assemble_from_game_log_h2h_avg_goals():
    games = pd.DataFrame(
        [
            _row("A", "B", "home", date(2023, 1, 1), 3, 1, "W"),
            _row("A", "B", "home", date(2023, 6, 1), 1, 1, "D"),
        ]
    )
    features = assemble_from_game_log(games, date(2024, 1, 1), "A", "B")
    assert features["h2h_win_rate_home"] == 0.5
    assert features["h2h_avg_goals_scored_home"] == 2.0
    assert features["h2h_avg_goals_allowed_home"] == 1.0


def test_assemble_from_game_log_h2h_none_with_no_meetings():
    games = pd.DataFrame(
        [], columns=["TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GAME_DATE", "GF", "GA", "WDL"]
    )
    features = assemble_from_game_log(games, date(2024, 1, 1), "A", "B")
    assert features["h2h_win_rate_home"] is None
    assert features["h2h_avg_goals_scored_home"] is None
    assert features["h2h_avg_goals_allowed_home"] is None


def test_assemble_from_game_log_leakage_guard_excludes_future_games():
    games = pd.DataFrame(
        [
            _row("A", "X", "home", date(2024, 1, 1), 1, 0, "W"),
            _row("A", "Y", "home", date(2024, 6, 1), 0, 5, "L"),  # strictly AFTER as_of_date
        ]
    )
    features = assemble_from_game_log(games, date(2024, 2, 1), "A", "B")
    # Only the Jan 1 win is visible as of Feb 1 — the June loss must not affect the streak.
    assert features["win_streak_home"] == 1.0


def test_corners_feature_names_extends_feature_names_only_for_corners():
    """Corners-only additions must never leak into the shared 21 Layer 1/Layer 2 use —
    see module docstring."""
    assert CORNERS_FEATURE_NAMES[: len(FEATURE_NAMES)] == FEATURE_NAMES
    new_names = CORNERS_FEATURE_NAMES[len(FEATURE_NAMES) :]
    assert new_names == (
        "corners_for_home",
        "corners_against_home",
        "corners_for_away",
        "corners_against_away",
        # The league's own corner level, added 2026-08-23. league_avg_goals had existed since
        # partial pooling while corners had no equivalent, so these regressors knew a league's
        # SCORING level and nothing about its CORNER level -- across 28k fixtures P(over 9.5)
        # runs 0.435 to 0.607, a spread the model could not see.
        "league_avg_corners",
    )
    assert not set(new_names) & set(FEATURE_NAMES)


def test_assemble_from_game_log_corners_none_when_never_merged():
    """A games_df that never had merge_corners_into_game_log applied (e.g. backfill_
    predictions.py's own retrodiction game log — a real, accepted gap, see module docstring)
    must return None for all four corners features, not raise a KeyError."""
    games = pd.DataFrame([_row("A", "B", "home", date(2024, 1, 1), 1, 0, "W")])
    features = assemble_from_game_log(games, date(2024, 1, 8), "A", "B")
    assert features["corners_for_home"] is None
    assert features["corners_against_home"] is None
    assert features["corners_for_away"] is None
    assert features["corners_against_away"] is None


def test_merge_corners_into_game_log_attaches_for_and_against():
    games = pd.DataFrame(
        [
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 2,
                "GA": 1,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "B",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 1,
                "GA": 2,
                "WDL": "L",
            },
        ]
    )
    corners = pd.DataFrame(
        [
            {"FIXTURE_ID": 1, "TEAM_ID": "A", "CORNERS": 7},
            {"FIXTURE_ID": 1, "TEAM_ID": "B", "CORNERS": 3},
        ]
    )
    merged = merge_corners_into_game_log(games, corners)

    team_a = merged[merged["TEAM_ID"] == "A"].iloc[0]
    assert team_a["CORNERS_FOR"] == 7
    assert team_a["CORNERS_AGAINST"] == 3

    team_b = merged[merged["TEAM_ID"] == "B"].iloc[0]
    assert team_b["CORNERS_FOR"] == 3
    assert team_b["CORNERS_AGAINST"] == 7


def test_merge_corners_into_game_log_leaves_nan_for_missing_fixture():
    """A fixture whose /fixtures/statistics call never returned a real corner count (a real,
    documented ~26-30% historical coverage gap) must get NaN, not a fabricated 0."""
    games = pd.DataFrame(
        [
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 2,
                "GA": 1,
                "WDL": "W",
            }
        ]
    )
    corners = pd.DataFrame(columns=["FIXTURE_ID", "TEAM_ID", "CORNERS"])
    merged = merge_corners_into_game_log(games, corners)
    assert merged.iloc[0]["CORNERS_FOR"] is None or pd.isna(merged.iloc[0]["CORNERS_FOR"])
    assert merged.iloc[0]["CORNERS_AGAINST"] is None or pd.isna(merged.iloc[0]["CORNERS_AGAINST"])


def test_assemble_from_game_log_corners_rolling_after_merge():
    games = pd.DataFrame(
        [
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "A",
                "OPPONENT_ID": "X",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 1,
                "GA": 0,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "X",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 0,
                "GA": 1,
                "WDL": "L",
            },
            {
                "FIXTURE_ID": 2,
                "TEAM_ID": "A",
                "OPPONENT_ID": "Y",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 8),
                "GF": 2,
                "GA": 1,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": 2,
                "TEAM_ID": "Y",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GAME_DATE": date(2024, 1, 8),
                "GF": 1,
                "GA": 2,
                "WDL": "L",
            },
            # Strictly AFTER as_of_date below — must not affect the rolling average (leakage guard).
            {
                "FIXTURE_ID": 3,
                "TEAM_ID": "A",
                "OPPONENT_ID": "Z",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 6, 1),
                "GF": 9,
                "GA": 9,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": 3,
                "TEAM_ID": "Z",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GAME_DATE": date(2024, 6, 1),
                "GF": 9,
                "GA": 9,
                "WDL": "L",
            },
        ]
    )
    corners = pd.DataFrame(
        [
            {"FIXTURE_ID": 1, "TEAM_ID": "A", "CORNERS": 6},
            {"FIXTURE_ID": 1, "TEAM_ID": "X", "CORNERS": 4},
            {"FIXTURE_ID": 2, "TEAM_ID": "A", "CORNERS": 8},
            {"FIXTURE_ID": 2, "TEAM_ID": "Y", "CORNERS": 2},
            {"FIXTURE_ID": 3, "TEAM_ID": "A", "CORNERS": 20},  # future — must be excluded
            {"FIXTURE_ID": 3, "TEAM_ID": "Z", "CORNERS": 20},
        ]
    )
    merged = merge_corners_into_game_log(games, corners)
    features = assemble_from_game_log(merged, date(2024, 2, 1), "A", "B")
    # A's own corners in its two prior games were 6 and 8 -> mean 7; conceded 4 and 2 -> mean 3.
    assert features["corners_for_home"] == 7.0
    assert features["corners_against_home"] == 3.0


@pytest.fixture
async def seeded_team_with_corners_history():
    """A real team with 3 completed fixtures alternating home/away, real corner counts on
    FixtureLiveState — proves _corners_rolling_live correctly picks home_corners vs
    away_corners per past fixture depending on which side the team was actually on, not just
    always reading the same column."""
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
        team = Team(sport_id=sport.id, league_id=league.id, name="Team A", external_id="a1")
        opp1 = Team(sport_id=sport.id, league_id=league.id, name="Opp 1", external_id="o1")
        opp2 = Team(sport_id=sport.id, league_id=league.id, name="Opp 2", external_id="o2")
        db.add_all([team, opp1, opp2])
        await db.flush()

        base = datetime(2026, 1, 1, tzinfo=UTC)
        fixtures = []
        # Team A is HOME with 6 corners for / 2 against.
        f1 = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-corners-1",
            home_team_id=team.id,
            away_team_id=opp1.id,
            kickoff_utc=base,
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(f1)
        await db.flush()
        db.add(
            FixtureLiveState(
                fixture_id=f1.id,
                home_score=1,
                away_score=0,
                home_corners=6,
                away_corners=2,
                status="completed",
                last_updated_utc=datetime.now(UTC),
            )
        )
        fixtures.append(f1)

        # Team A is AWAY with 4 corners for (= away_corners) / 8 against (= home_corners).
        f2 = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id="fx-corners-2",
            home_team_id=opp2.id,
            away_team_id=team.id,
            kickoff_utc=base + timedelta(days=7),
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(f2)
        await db.flush()
        db.add(
            FixtureLiveState(
                fixture_id=f2.id,
                home_score=2,
                away_score=1,
                home_corners=8,
                away_corners=4,
                status="completed",
                last_updated_utc=datetime.now(UTC),
            )
        )
        fixtures.append(f2)

        await db.commit()
        for f in fixtures:
            await db.refresh(f)
        await db.refresh(team)

    yield team, fixtures

    async with async_session_factory() as db:
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


async def test_corners_rolling_live_picks_correct_side_per_fixture(
    seeded_team_with_corners_history,
):
    team, _fixtures = seeded_team_with_corners_history
    async with async_session_factory() as db:
        corners_for, corners_against = await _corners_rolling_live(db, team.id, n=5)
    # Home game: for=6, against=2. Away game: for=4 (away_corners), against=8 (home_corners).
    assert corners_for == pytest.approx((6 + 4) / 2)
    assert corners_against == pytest.approx((2 + 8) / 2)


async def test_corners_rolling_live_none_with_no_completed_fixtures():
    async with async_session_factory() as db:
        corners_for, corners_against = await _corners_rolling_live(db, uuid.uuid4(), n=5)
    assert corners_for is None
    assert corners_against is None


def _two_team_log(rows):
    """Minimal game-log frame in collect_football_data.py's shape."""
    return pd.DataFrame(rows)


def test_merge_xg_into_game_log_attaches_for_and_against():
    """xG arrives from TheStatsAPI already resolved to API-Football FIXTURE_ID/TEAM_ID (the
    collector owns the cross-provider join), so this stays a plain two-key merge."""
    games = _two_team_log(
        [
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 2,
                "GA": 1,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "B",
                "OPPONENT_ID": "A",
                "HOME_AWAY": "away",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 1,
                "GA": 2,
                "WDL": "L",
            },
        ]
    )
    xg = pd.DataFrame(
        [
            {"FIXTURE_ID": 1, "TEAM_ID": "A", "XG_FOR": 2.41},
            {"FIXTURE_ID": 1, "TEAM_ID": "B", "XG_FOR": 0.88},
        ]
    )
    merged = merge_xg_into_game_log(games, xg)

    team_a = merged[merged["TEAM_ID"] == "A"].iloc[0]
    assert team_a["XG_FOR"] == pytest.approx(2.41)
    assert team_a["XG_AGAINST"] == pytest.approx(0.88)

    team_b = merged[merged["TEAM_ID"] == "B"].iloc[0]
    assert team_b["XG_FOR"] == pytest.approx(0.88)
    assert team_b["XG_AGAINST"] == pytest.approx(2.41)


def test_merge_xg_leaves_nan_for_a_league_with_no_xg_collected():
    """MLS/CSL/Scottish Prem have no xG collected, and EPL 2021 genuinely has none upstream
    (measured: 0/5 sampled). Those rows must come through as NaN for XGBoost to treat as
    missing — never a fabricated 0.0, which would read as 'no chances created'."""
    games = _two_team_log(
        [
            {
                "FIXTURE_ID": 99,
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2021, 8, 1),
                "GF": 1,
                "GA": 0,
                "WDL": "W",
            }
        ]
    )
    xg = pd.DataFrame(columns=["FIXTURE_ID", "TEAM_ID", "XG_FOR"])
    merged = merge_xg_into_game_log(games, xg)
    assert merged["XG_FOR"].isna().all()
    assert merged["XG_AGAINST"].isna().all()


def test_xg_rolling_is_none_when_never_merged():
    """Retrodiction (backfill_predictions.py) builds its own game log without the xG merge.
    That must degrade to None, not raise a KeyError — same contract as _corners_rolling."""
    games = _two_team_log(
        [
            {
                "FIXTURE_ID": 1,
                "TEAM_ID": "A",
                "OPPONENT_ID": "B",
                "HOME_AWAY": "home",
                "GAME_DATE": date(2024, 1, 1),
                "GF": 1,
                "GA": 0,
                "WDL": "W",
            }
        ]
    )
    assert _xg_rolling(games, date(2024, 6, 1)) == (None, None)


def test_xg_rolling_excludes_the_match_being_predicted():
    """The leakage guard: a match on as_of_date must not inform its own prediction. Without
    the strict < comparison a fixture's own xG would leak into its feature vector."""
    rows = [
        {
            "FIXTURE_ID": i,
            "TEAM_ID": "A",
            "OPPONENT_ID": "B",
            "HOME_AWAY": "home",
            "GAME_DATE": date(2024, 1, i + 1),
            "GF": 1,
            "GA": 0,
            "WDL": "W",
            "XG_FOR": 1.0,
            "XG_AGAINST": 0.5,
        }
        for i in range(3)
    ]
    # the match being predicted, carrying a wildly different xG that must NOT be counted
    rows.append(
        {
            "FIXTURE_ID": 99,
            "TEAM_ID": "A",
            "OPPONENT_ID": "B",
            "HOME_AWAY": "home",
            "GAME_DATE": date(2024, 2, 1),
            "GF": 9,
            "GA": 0,
            "WDL": "W",
            "XG_FOR": 99.0,
            "XG_AGAINST": 99.0,
        }
    )
    xg_for, xg_against = _xg_rolling(_two_team_log(rows), date(2024, 2, 1))
    assert xg_for == pytest.approx(1.0)
    assert xg_against == pytest.approx(0.5)


def test_league_baselines_distinguish_leagues():
    """The whole point: a low-scoring league and a high-scoring one must produce different
    numbers, so the model can stop applying one blended scoring level to both."""
    from app.models_ml.league_baselines import compute_league_baselines

    rows = []
    for i in range(40):
        d = date(2025, 1, 1) + timedelta(days=i)
        rows += [
            {"LEAGUE": "low", "HOME_AWAY": "home", "GAME_DATE": d, "GF": 1, "GA": 0, "WDL": "W"},
            {"LEAGUE": "low", "HOME_AWAY": "away", "GAME_DATE": d, "GF": 0, "GA": 1, "WDL": "L"},
            {"LEAGUE": "high", "HOME_AWAY": "home", "GAME_DATE": d, "GF": 3, "GA": 2, "WDL": "W"},
            {"LEAGUE": "high", "HOME_AWAY": "away", "GAME_DATE": d, "GF": 2, "GA": 3, "WDL": "L"},
        ]
    baselines = compute_league_baselines(pd.DataFrame(rows))
    low = baselines.get("low", date(2026, 1, 1))
    high = baselines.get("high", date(2026, 1, 1))

    assert low.avg_goals == pytest.approx(1.0)
    assert high.avg_goals == pytest.approx(5.0)


def test_league_baseline_is_expanding_not_full_season():
    """Leakage guard: the baseline must reflect only matches BEFORE the fixture being scored.
    A full-season average would leak the very matches being predicted."""
    from app.models_ml.league_baselines import compute_league_baselines

    rows = []
    # 40 low-scoring matches, then 40 high-scoring ones in the same league
    for i in range(40):
        d = date(2025, 1, 1) + timedelta(days=i)
        rows += [
            {"LEAGUE": "l", "HOME_AWAY": "home", "GAME_DATE": d, "GF": 0, "GA": 0, "WDL": "D"},
            {"LEAGUE": "l", "HOME_AWAY": "away", "GAME_DATE": d, "GF": 0, "GA": 0, "WDL": "D"},
        ]
    for i in range(40):
        d = date(2025, 6, 1) + timedelta(days=i)
        rows += [
            {"LEAGUE": "l", "HOME_AWAY": "home", "GAME_DATE": d, "GF": 5, "GA": 5, "WDL": "D"},
            {"LEAGUE": "l", "HOME_AWAY": "away", "GAME_DATE": d, "GF": 5, "GA": 5, "WDL": "D"},
        ]
    baselines = compute_league_baselines(pd.DataFrame(rows))

    early = baselines.get("l", date(2025, 3, 1))
    late = baselines.get("l", date(2026, 1, 1))
    assert early.avg_goals == pytest.approx(0.0), "must not see the later high-scoring run"
    assert late.avg_goals > early.avg_goals


def test_league_baseline_is_none_before_any_match():
    """No prior matches means no baseline — None, not a fabricated neutral value."""
    from app.models_ml.league_baselines import compute_league_baselines

    rows = [
        {
            "LEAGUE": "l",
            "HOME_AWAY": "home",
            "GAME_DATE": date(2025, 5, 1),
            "GF": 1,
            "GA": 1,
            "WDL": "D",
        },
        {
            "LEAGUE": "l",
            "HOME_AWAY": "away",
            "GAME_DATE": date(2025, 5, 1),
            "GF": 1,
            "GA": 1,
            "WDL": "D",
        },
    ]
    baselines = compute_league_baselines(pd.DataFrame(rows))
    assert baselines.get("l", date(2025, 1, 1)) is None
    assert baselines.get("unknown-league", date(2026, 1, 1)) is None


def test_league_baseline_needs_no_league_column():
    """Retrodiction builds its own game log without a LEAGUE column — must degrade, not raise."""
    from app.models_ml.league_baselines import compute_league_baselines

    rows = [{"HOME_AWAY": "home", "GAME_DATE": date(2025, 5, 1), "GF": 1, "GA": 1, "WDL": "D"}]
    baselines = compute_league_baselines(pd.DataFrame(rows))
    assert baselines.get("anything", date(2026, 1, 1)) is None
