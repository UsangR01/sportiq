"""Train the first real NBA win-probability model (TDD §3.3/§3.5).

Temporal split (adapted from TDD §3.5's "train on N-3..N-1, validate on last 10% of N-1, test
on N" — using clean season boundaries instead of an intra-season slice, simpler and avoids
any ambiguity about what "last 10%" means chronologically):
  - train: 2020-21, 2021-22, 2022-23, 2023-24 (4 seasons)
  - validate: 2024-25
  - test: 2025-26 (the most recently completed season)

Optuna: 50 trials (TDD §3.5 says 100 — reduced to keep a laptop run in seconds-to-minutes,
not because 50 was found insufficient). Isotonic calibration is fit on validation
predictions, exactly as TDD §3.5 specifies. Final model is re-trained on train+val with the
best hyperparameters (TDD §3.5: "Best trial is re-trained on train+val for final artefact").

Usage (from repo root):
    PYTHONPATH=backend python ml/training/train_nba.py
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# mlflow 3.x deprecated the plain filesystem tracking backend by default (raises unless this
# is set) — TDD §3.5 just says "MLflow logs all experiments", no backend specified; opting
# into the file store is the simplest choice for a one-off local training run, not a
# multi-user tracking server.
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is needed explicitly

import joblib  # noqa: E402
import mlflow  # noqa: E402
import optuna  # noqa: E402
import pandas as pd  # noqa: E402
import xgboost as xgb  # noqa: E402
from app.models_ml.nba_features import FEATURE_NAMES, assemble_from_game_log  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ML_DIR = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ML_DIR / "artifacts"

TRAIN_SEASONS = ["2020-21", "2021-22", "2022-23", "2023-24"]
VAL_SEASON = "2024-25"
TEST_SEASON = "2025-26"

N_OPTUNA_TRIALS = 50


def build_training_examples(games: pd.DataFrame, odds: pd.DataFrame) -> pd.DataFrame:
    """One row per game (from the home team's perspective) — features via
    assemble_from_game_log (the same function run_predictions.py's live path calls through
    assemble_from_live_db), label = 1 if home team won."""
    best_odds = odds.groupby(["date", "home_short", "away_short"], as_index=False)[
        "home_odds"
    ].max()
    odds_lookup = {
        (row.date, row.home_short, row.away_short): row.home_odds for row in best_odds.itertuples()
    }

    rows = []
    for game_id, group in games.groupby("GAME_ID"):
        if len(group) != 2:
            continue  # incomplete pair — skip defensively rather than guess
        home_row_df = group[group["MATCHUP"].str.contains("vs.", regex=False)]
        away_row_df = group[group["MATCHUP"].str.contains("@", regex=False)]
        if home_row_df.empty or away_row_df.empty:
            continue
        home_row = home_row_df.iloc[0]
        away_row = away_row_df.iloc[0]

        game_date = home_row["GAME_DATE"]
        season = home_row["SEASON"]
        home_abbr = home_row["TEAM_ABBREVIATION"]
        away_abbr = away_row["TEAM_ABBREVIATION"]

        home_odds = odds_lookup.get((str(game_date), home_abbr, away_abbr))
        moneyline_prob = (1 / home_odds) if home_odds else None

        features = assemble_from_game_log(
            games, game_date, season, home_abbr, away_abbr, moneyline_prob
        )
        features["label"] = 1 if home_row["WL"] == "W" else 0
        features["season"] = season
        features["game_id"] = game_id
        features["home_odds"] = (
            home_odds  # kept alongside for the ROI metric only, not a model input
        )
        rows.append(features)

    return pd.DataFrame(rows)


def _optuna_objective(trial, X_train, y_train, X_val, y_val) -> float:
    params = {
        "n_estimators": trial.suggest_int("n_estimators", 50, 400),
        "max_depth": trial.suggest_int("max_depth", 2, 6),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "subsample": trial.suggest_float("subsample", 0.6, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
        "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
    }
    model = xgb.XGBClassifier(objective="binary:logistic", **params)
    model.fit(X_train, y_train)
    val_preds = model.predict_proba(X_val)[:, 1]
    return log_loss(y_val, val_preds)


async def _register_model(artefact_path: Path, rps: float, accuracy: float) -> None:
    from app.core.database import async_session_factory
    from app.predictions.models import ModelRegistry
    from app.sports.models import Sport
    from sqlalchemy import select

    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "nba"))).scalar_one()

        # TDD's "single active model per sport" invariant — deactivate any prior active row.
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

        version = f"nba_xgb_v{datetime.now(UTC):%Y%m%d%H%M%S}"
        db.add(
            ModelRegistry(
                sport_id=sport.id,
                version=version,
                artefact_path=str(artefact_path),
                rps_score=rps,
                accuracy=accuracy,
                trained_at=datetime.now(UTC),
                is_active=True,
            )
        )
        await db.commit()
        print(f"registered models_registry row: {version} (is_active=True)")


def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    games = pd.read_parquet(DATA_DIR / "nba_game_log.parquet")
    odds_path = DATA_DIR / "nba_odds_sample.parquet"
    odds = (
        pd.read_parquet(odds_path)
        if odds_path.exists()
        else pd.DataFrame(columns=["date", "home_short", "away_short", "home_odds"])
    )

    print("assembling training examples (this walks every game with a leakage-safe filter)...")
    examples = build_training_examples(games, odds)
    print(
        f"{len(examples)} examples, moneyline available for {examples['home_odds'].notna().sum()}"
    )

    train_df = examples[examples["season"].isin(TRAIN_SEASONS)]
    val_df = examples[examples["season"] == VAL_SEASON]
    test_df = examples[examples["season"] == TEST_SEASON]
    print(f"train={len(train_df)} val={len(val_df)} test={len(test_df)}")

    feature_cols = list(FEATURE_NAMES)
    X_train = train_df[feature_cols].astype(float)
    y_train = train_df["label"]
    X_val = val_df[feature_cols].astype(float)
    y_val = val_df["label"]
    X_test = test_df[feature_cols].astype(float)
    y_test = test_df["label"]

    print(f"running Optuna ({N_OPTUNA_TRIALS} trials)...")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="minimize")
    study.optimize(
        lambda trial: _optuna_objective(trial, X_train, y_train, X_val, y_val),
        n_trials=N_OPTUNA_TRIALS,
    )
    print(f"best val log_loss={study.best_value:.4f}, params={study.best_params}")

    # Re-train on train+val with the winning hyperparameters (TDD §3.5).
    X_trainval = pd.concat([X_train, X_val])
    y_trainval = pd.concat([y_train, y_val])
    final_model = xgb.XGBClassifier(objective="binary:logistic", **study.best_params)
    final_model.fit(X_trainval, y_trainval)

    # Isotonic calibration on validation predictions (TDD §3.5).
    val_raw = final_model.predict_proba(X_val)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999)
    calibrator.fit(val_raw, y_val)

    test_raw = final_model.predict_proba(X_test)[:, 1]
    test_calibrated = calibrator.predict(test_raw)
    test_pred_label = (test_calibrated > 0.5).astype(int)

    accuracy = accuracy_score(y_test, test_pred_label)
    brier = brier_score_loss(y_test, test_calibrated)
    # RPS for a 2-outcome market (no draw) reduces exactly to the Brier score — there's only
    # one cumulative-probability boundary. TDD's RPS is really aimed at 3-way (football)
    # markets; documenting the equivalence rather than re-deriving RPS for a 2-way case.
    rps = brier

    baseline_accuracy = accuracy_score(y_test, [1] * len(y_test))  # "always pick home" baseline

    # Bonus ROI metric, deliberately simplified: only covers test games where we have a real
    # home_odds AND the model favours home — away-side picks aren't included since the
    # collected odds sample only captured home_odds, not away_odds (see collect_nba_data.py).
    roi_rows = test_df[test_df["home_odds"].notna() & (test_calibrated > 0.5)]
    if len(roi_rows) > 0:
        stakes = len(roi_rows)
        returns = sum(row.home_odds if row.label == 1 else 0.0 for row in roi_rows.itertuples())
        flat_stake_roi = (returns - stakes) / stakes
    else:
        flat_stake_roi = None

    print(f"test accuracy={accuracy:.4f} (baseline={baseline_accuracy:.4f}) brier/rps={brier:.4f}")
    print(f"flat-stake ROI (home picks with real odds, n={len(roi_rows)}): {flat_stake_roi}")

    artefact_path = ARTIFACT_DIR / f"nba_xgb_{datetime.now(UTC):%Y%m%d%H%M%S}.joblib"
    joblib.dump(
        {"model": final_model, "calibrator": calibrator, "feature_names": feature_cols},
        artefact_path,
    )
    print(f"saved artefact to {artefact_path}")

    mlflow.set_tracking_uri(f"file:{ML_DIR / 'mlruns'}")
    mlflow.set_experiment("nba_win_probability")
    with mlflow.start_run():
        mlflow.log_params(study.best_params)
        mlflow.log_param("n_optuna_trials", N_OPTUNA_TRIALS)
        mlflow.log_param("train_seasons", ",".join(TRAIN_SEASONS))
        mlflow.log_param("val_season", VAL_SEASON)
        mlflow.log_param("test_season", TEST_SEASON)
        mlflow.log_metric("test_accuracy", accuracy)
        mlflow.log_metric("test_brier_rps", brier)
        mlflow.log_metric("baseline_accuracy", baseline_accuracy)
        mlflow.log_metric("val_log_loss", study.best_value)
        if flat_stake_roi is not None:
            mlflow.log_metric("flat_stake_roi_home_picks", flat_stake_roi)
        mlflow.log_artifact(str(artefact_path))

    asyncio.run(_register_model(artefact_path, rps, accuracy))


if __name__ == "__main__":
    main()
