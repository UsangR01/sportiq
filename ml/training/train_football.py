"""Train the first real football 1X2 model (TDD §3.2/§3.5), scoped to EPL only (see
CLAUDE.md's scope decision — the other 4 leagues are seeded/generically supported but not
part of this training run).

Two-layer stack per TDD §3.2:
  - Layer 1: two XGBoost Poisson regressors (objective="count:poisson"), one per side,
    predicting expected goals from the 16 pre-match context features
    (app/models_ml/football_features.py).
  - Layer 2: an XGBoost multiclass classifier (objective="multi:softprob") predicting
    home/draw/away from Layer 1's xG outputs plus a handful of context features not already
    summarized by xG (see app/models_ml/football.py:LAYER2_CONTEXT_FEATURES).
  - One-vs-rest isotonic calibration per class, renormalised to sum to 1 (TDD §3.3's
    calibration requirement, adapted from NBA's binary case to a 3-way market).

Known simplification, documented rather than hidden: Layer 2 is trained on Layer 1's own
IN-SAMPLE predictions for the training split (out-of-sample for val/test, since Layer 1 is
already fixed by the time Layer 2 sees them) — a proper stacked-generalization setup would use
out-of-fold Layer-1 predictions for the training split too (e.g. via k-fold), but that's a
materially larger amount of code for a first real version; flagging this rather than silently
accepting a subtly-optimistic Layer 2 training signal.

Temporal split (same 5-season window ml/training/compute_football_key_players.py uses):
  - train: 2021-22, 2022-23, 2023-24 (3 seasons)
  - validate: 2024-25
  - test: 2025-26 (the most recently completed season)
Fewer seasons than NBA's 6 (4 train + 1 val + 1 test) — a deliberate, documented scope cut
tied to the subscription's real ~1-month time limit and the added per-fixture lineup-call
cost (see collect_football_data.py); not a data-availability limitation (API-Football itself
has full coverage back to 2010).

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/train_football.py
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")  # see train_nba.py for why

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")

import joblib  # noqa: E402
import mlflow  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import xgboost as xgb  # noqa: E402
from app.models_ml.football import FootballModel  # noqa: E402
from app.models_ml.football_features import FEATURE_NAMES, assemble_from_game_log  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import accuracy_score, log_loss  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ML_DIR = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ML_DIR / "artifacts"

TRAIN_SEASONS = [2021, 2022, 2023]
VAL_SEASON = 2024
TEST_SEASON = 2025

CLASSES = FootballModel.CLASSES  # ("home", "draw", "away") — fixes the label encoding below
LABEL_BY_CLASS = {cls: i for i, cls in enumerate(CLASSES)}


def index_played_names(lineups: pd.DataFrame) -> dict[tuple[int, str], set[str]]:
    """(FIXTURE_ID, TEAM_ID) -> lowercased names who appeared with real minutes — the football
    analogue of ml/training/train_nba.py's index_played_names, built from
    collect_football_data.py's already-minutes-filtered lineup rows."""
    index: dict[tuple[int, str], set[str]] = {}
    for fixture_id, team_id, name in zip(
        lineups["FIXTURE_ID"], lineups["TEAM_ID"], lineups["PLAYER_NAME"].str.lower(), strict=False
    ):
        index.setdefault((fixture_id, team_id), set()).add(name)
    return index


def historical_key_player_availability(
    played_names_index: dict[tuple[int, str], set[str]],
    team_key_players_by_team_season: dict,
    team_id: str,
    season: int,
    fixture_id: int,
) -> tuple[int | None, float | None]:
    """BACKTEST LABEL ONLY, built from lineup presence in an *already-completed* fixture —
    mirrors ml/training/train_nba.py:historical_key_player_availability's role and warning
    exactly: must NEVER be reused for live Stage 2
    (app/models_ml/key_player_availability.py:get_key_player_availability), which is pre-game
    and reads only player_injury_status. Kept in this script, not in app/models_ml/,
    specifically so it can't be imported into the live path by accident."""
    key_players = team_key_players_by_team_season.get((team_id, season))
    if not key_players:
        return None, None

    played_names = played_names_index.get((fixture_id, team_id), set())

    available_count = 0
    combined = 0.0
    for key_player in key_players:
        if key_player["player_name"].lower() in played_names:
            available_count += 1
            combined += key_player["combined_metric"]
    return available_count, combined


async def _load_team_key_players() -> dict[tuple[str, int], list[dict]]:
    """Real team_key_players rows (Stage 1, written by
    ml/training/compute_football_key_players.py), joined to the team's own API-Football
    external_id — football teams don't have a reliable cross-provider abbreviation the way
    NBA's do, so this joins by external_id instead of short_name (see
    app/fixtures/models.py:Team)."""
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.fixtures.models import Team, TeamKeyPlayer

    by_team_season: dict[tuple[str, int], list[dict]] = {}
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(TeamKeyPlayer, Team.external_id).join(Team, Team.id == TeamKeyPlayer.team_id)
            )
        ).all()

    for key_player, external_id in rows:
        key = (external_id, key_player.season_year)
        by_team_season.setdefault(key, []).append(
            {
                "player_name": key_player.player_name,
                "combined_metric": key_player.combined_metric,
            }
        )
    return by_team_season


def ranked_probability_score(probs: np.ndarray, actual_label: int, n_classes: int = 3) -> float:
    """Real RPS for an ordinal 3-way market (home/draw/away treated as ordered from
    home-favorable to away-favorable, matching FootballModel.CLASSES's own order) — the
    metric TDD §3.3 specifies for football, unlike NBA's 2-outcome shortcut
    (train_nba.py's `rps = brier`, valid only because a 2-outcome market has just one
    cumulative-probability boundary)."""
    cum_forecast = np.cumsum(probs)
    cum_actual = np.cumsum([1.0 if i == actual_label else 0.0 for i in range(n_classes)])
    return float(np.sum((cum_forecast - cum_actual) ** 2) / (n_classes - 1))


def build_training_examples(
    games: pd.DataFrame,
    odds: pd.DataFrame,
    lineups: pd.DataFrame,
    team_key_players_by_team_season: dict,
    team_codes: dict[str, str],
) -> pd.DataFrame:
    """One row per fixture (from the home team's perspective) — features via
    assemble_from_game_log (the same function run_predictions.py's live path calls through
    assemble_from_live_db), label in {0: home win, 1: draw, 2: away win}.

    team_codes: API-Football team_id -> its own 3-letter code, used only to join against
    TheRundown's teams_normalized.abbreviation for the odds sample — a best-effort
    cross-provider match (see collect_football_data.py:_fetch_team_codes), not a guaranteed
    one; unmatched fixtures simply get moneyline_implied_prob_home=None."""
    best_odds = odds.groupby(["date", "home_short", "away_short"], as_index=False)[
        "home_odds"
    ].max()
    odds_lookup = {
        (row.date, row.home_short, row.away_short): row.home_odds for row in best_odds.itertuples()
    }
    played_names_index = index_played_names(lineups)

    rows = []
    for (fixture_id, season), group in games.groupby(["FIXTURE_ID", "SEASON"]):
        home_row_df = group[group["HOME_AWAY"] == "home"]
        away_row_df = group[group["HOME_AWAY"] == "away"]
        if home_row_df.empty or away_row_df.empty:
            continue
        home_row = home_row_df.iloc[0]
        away_row = away_row_df.iloc[0]

        game_date = home_row["GAME_DATE"]
        home_id = home_row["TEAM_ID"]
        away_id = away_row["TEAM_ID"]

        home_code = team_codes.get(home_id)
        away_code = team_codes.get(away_id)
        home_odds = (
            odds_lookup.get((str(game_date), home_code, away_code))
            if home_code and away_code
            else None
        )
        moneyline_prob = (1 / home_odds) if home_odds else None

        key_avail_home, key_per_home = historical_key_player_availability(
            played_names_index, team_key_players_by_team_season, home_id, season, fixture_id
        )
        key_avail_away, key_per_away = historical_key_player_availability(
            played_names_index, team_key_players_by_team_season, away_id, season, fixture_id
        )

        features = assemble_from_game_log(
            games,
            game_date,
            home_id,
            away_id,
            moneyline_prob,
            key_players_available_home=key_avail_home,
            key_players_available_away=key_avail_away,
            key_players_per_combined_home=key_per_home,
            key_players_per_combined_away=key_per_away,
        )
        features["label"] = LABEL_BY_CLASS[
            "home" if home_row["WDL"] == "W" else ("draw" if home_row["WDL"] == "D" else "away")
        ]
        features["season"] = season
        features["fixture_id"] = fixture_id
        features["home_goals"] = home_row["GF"]
        features["away_goals"] = home_row["GA"]
        features["home_odds"] = home_odds  # ROI metric only, not a model input
        rows.append(features)

    return pd.DataFrame(rows)


async def _register_model(artefact_path: Path, rps: float, accuracy: float, roi: float | None) -> None:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.predictions.models import ModelRegistry
    from app.sports.models import Sport

    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "football"))).scalar_one()

        existing_active = (
            (
                await db.execute(
                    select(ModelRegistry).where(
                        ModelRegistry.sport_id == sport.id, ModelRegistry.is_active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing_active:
            row.is_active = False

        version = f"football_xgb_v{datetime.now(UTC):%Y%m%d%H%M%S}"
        db.add(
            ModelRegistry(
                sport_id=sport.id,
                version=version,
                artefact_path=str(artefact_path),
                rps_score=rps,
                accuracy=accuracy,
                roi_simulation=roi,
                trained_at=datetime.now(UTC),
                is_active=True,
            )
        )
        await db.commit()
        print(f"registered models_registry row: {version} (is_active=True)")


async def main_async() -> None:
    # Everything DB-touching runs inside this one asyncio.run() call — see train_nba.py for
    # why two separate asyncio.run() calls in one process is unsafe on this platform.
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    games = pd.read_parquet(DATA_DIR / "football_game_log.parquet")
    lineups = pd.read_parquet(DATA_DIR / "football_lineups.parquet")
    team_codes = dict(
        zip(
            pd.read_parquet(DATA_DIR / "football_team_codes.parquet")["team_id"],
            pd.read_parquet(DATA_DIR / "football_team_codes.parquet")["code"],
            strict=False,
        )
    )
    odds_path = DATA_DIR / "football_odds_sample.parquet"
    odds = (
        pd.read_parquet(odds_path)
        if odds_path.exists()
        else pd.DataFrame(columns=["date", "home_short", "away_short", "home_odds"])
    )

    print("loading team_key_players (Stage 1 — run compute_football_key_players.py first)...")
    team_key_players_by_team_season = await _load_team_key_players()
    print(f"  {len(team_key_players_by_team_season)} (team, season) entries loaded")

    print("assembling training examples (leakage-safe: every stat filtered to GAME_DATE < fixture date)...")
    examples = build_training_examples(
        games, odds, lineups, team_key_players_by_team_season, team_codes
    )
    print(f"{len(examples)} examples, moneyline available for {examples['home_odds'].notna().sum()}")

    train_df = examples[examples["season"].isin(TRAIN_SEASONS)]
    val_df = examples[examples["season"] == VAL_SEASON]
    test_df = examples[examples["season"] == TEST_SEASON]
    print(f"train={len(train_df)} val={len(val_df)} test={len(test_df)}")

    feature_cols = list(FEATURE_NAMES)
    X_train = train_df[feature_cols].astype(float)
    X_val = val_df[feature_cols].astype(float)
    X_test = test_df[feature_cols].astype(float)

    # --- Layer 1: Poisson expected-goals regressors ---------------------------------------
    layer1_home_model = xgb.XGBRegressor(objective="count:poisson", n_estimators=200, max_depth=4)
    layer1_home_model.fit(X_train, train_df["home_goals"].astype(float))
    layer1_away_model = xgb.XGBRegressor(objective="count:poisson", n_estimators=200, max_depth=4)
    layer1_away_model.fit(X_train, train_df["away_goals"].astype(float))

    def add_xg(df: pd.DataFrame, X: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["xg_home"] = layer1_home_model.predict(X).clip(min=0)
        df["xg_away"] = layer1_away_model.predict(X).clip(min=0)
        return df

    train_df = add_xg(train_df, X_train)
    val_df = add_xg(val_df, X_val)
    test_df = add_xg(test_df, X_test)

    layer2_cols = ["xg_home", "xg_away", *FootballModel.LAYER2_CONTEXT_FEATURES]
    X_train_l2 = train_df[layer2_cols].astype(float)
    y_train = train_df["label"]
    X_val_l2 = val_df[layer2_cols].astype(float)
    y_val = val_df["label"]
    X_test_l2 = test_df[layer2_cols].astype(float)
    y_test = test_df["label"]

    # --- Layer 2: 1X2 classifier ------------------------------------------------------------
    layer2_model = xgb.XGBClassifier(
        objective="multi:softprob", num_class=3, n_estimators=200, max_depth=4
    )
    layer2_model.fit(X_train_l2, y_train)

    val_raw = layer2_model.predict_proba(X_val_l2)
    test_raw = layer2_model.predict_proba(X_test_l2)

    # One-vs-rest isotonic calibration per class, fit on validation predictions (TDD §3.3),
    # then renormalised to sum to 1 at inference (app/models_ml/football.py:predict).
    calibrators = {}
    for i, cls in enumerate(CLASSES):
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999)
        calibrator.fit(val_raw[:, i], (y_val == i).astype(float))
        calibrators[cls] = calibrator

    test_calibrated_raw = np.column_stack(
        [calibrators[cls].predict(test_raw[:, i]) for i, cls in enumerate(CLASSES)]
    )
    row_sums = test_calibrated_raw.sum(axis=1, keepdims=True)
    test_calibrated = np.where(row_sums > 0, test_calibrated_raw / row_sums, test_raw)

    test_pred_label = test_calibrated.argmax(axis=1)
    accuracy = accuracy_score(y_test, test_pred_label)
    rps = float(
        np.mean(
            [
                ranked_probability_score(test_calibrated[i], int(y_test.iloc[i]))
                for i in range(len(y_test))
            ]
        )
    )
    val_log_loss = log_loss(y_val, val_raw)

    baseline_accuracy = accuracy_score(y_test, [LABEL_BY_CLASS["home"]] * len(y_test))

    # Bonus ROI metric, same deliberate simplification as train_nba.py's: only covers test
    # fixtures with a real home_odds AND the model favouring home.
    home_idx = LABEL_BY_CLASS["home"]
    roi_rows = test_df[
        test_df["home_odds"].notna() & (test_calibrated[:, home_idx] == test_calibrated.max(axis=1))
    ]
    if len(roi_rows) > 0:
        stakes = len(roi_rows)
        returns = sum(
            row.home_odds if row.label == home_idx else 0.0 for row in roi_rows.itertuples()
        )
        flat_stake_roi = (returns - stakes) / stakes
    else:
        flat_stake_roi = None

    print(f"test accuracy={accuracy:.4f} (baseline={baseline_accuracy:.4f}) RPS={rps:.4f}")
    print(f"flat-stake ROI (home picks with real odds, n={len(roi_rows)}): {flat_stake_roi}")

    artefact_path = ARTIFACT_DIR / f"football_xgb_{datetime.now(UTC):%Y%m%d%H%M%S}.joblib"
    joblib.dump(
        {
            "layer1_home_model": layer1_home_model,
            "layer1_away_model": layer1_away_model,
            "layer1_feature_names": feature_cols,
            "layer2_model": layer2_model,
            "calibrators": calibrators,
        },
        artefact_path,
    )
    print(f"saved artefact to {artefact_path}")

    mlflow.set_tracking_uri(f"file:{ML_DIR / 'mlruns'}")
    mlflow.set_experiment("football_1x2")
    with mlflow.start_run():
        mlflow.log_param("train_seasons", ",".join(str(s) for s in TRAIN_SEASONS))
        mlflow.log_param("val_season", VAL_SEASON)
        mlflow.log_param("test_season", TEST_SEASON)
        mlflow.log_metric("test_accuracy", accuracy)
        mlflow.log_metric("test_rps", rps)
        mlflow.log_metric("baseline_accuracy", baseline_accuracy)
        mlflow.log_metric("val_log_loss", val_log_loss)
        if flat_stake_roi is not None:
            mlflow.log_metric("flat_stake_roi_home_picks", flat_stake_roi)
        mlflow.log_artifact(str(artefact_path))

    await _register_model(artefact_path, rps, accuracy, flat_stake_roi)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
