"""Train the football 1X2 model (TDD §3.2/§3.5), now pooling real historical data across EPL
AND Brasileirão (see CLAUDE.md — the other 3 European leagues are still seeded/generically
supported but have no historical training data collected). Brasileirão was added specifically
to give the retrodiction feature (app/workers/backfill_predictions.py) and the model itself
more real data to learn from — both leagues' fixtures/lineups feed the same pooled train/val/
test split below rather than two separate models, since features are goal-rate/point-based
(normalized), not raw-goal-count-based, so pooling across leagues with different scoring
levels is not expected to introduce a systematic bias.

Adopted from the user's own prior NBA notebook's feature-engineering ideas
(feature_engineering.ipynb / "Running - NBA Games Prediction Project.ipynb", both added to the
repo root) — see app/models_ml/football_features.py's module docstring for the full feature
list: real iterative Elo ratings (elo_diff), win streaks (win_streak_home/away), and richer
H2H (avg goals scored/allowed vs the specific opponent, not just win rate).

New in this pass: the corners-Poisson-regressors (Over/Under corners market) now train on 4
corners-specific rolling features (CORNERS_FEATURE_NAMES) instead of reusing Layer 1's goals-
shaped feature vector — each team's own rolling average corners won/conceded, merged in via
merge_corners_into_game_log. Layer 1's goals regressors and Layer 2's 1X2 classifier are
completely unaffected (still exactly FEATURE_NAMES/LAYER2_CONTEXT_FEATURES) — this only
changes what the corners regressors themselves see.

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

Temporal split (same 5-season window ml/training/compute_football_key_players.py uses, applied
identically to both leagues since Brasileirão's season is labeled by the same integer year
convention as EPL's start-year, just on a different calendar — see
app/adapters/api_football.py:CALENDAR_YEAR_SEASON_LEAGUES):
  - train: 2021, 2022, 2023 (3 seasons)
  - validate: 2024
  - test: 2025 (the most recently completed season)
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
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import accuracy_score, log_loss  # noqa: E402
from sklearn.model_selection import KFold  # noqa: E402

from app.models_ml.elo import compute_elo_history  # noqa: E402
from app.models_ml.football import FootballModel  # noqa: E402
from app.models_ml.league_baselines import compute_league_baselines  # noqa: E402
from app.models_ml.football_features import (  # noqa: E402
    CORNERS_FEATURE_NAMES,
    FEATURE_NAMES,
    assemble_from_game_log,
    merge_corners_into_game_log,
    merge_xg_into_game_log,
)
from app.models_ml.historical_key_players import (  # noqa: E402
    historical_key_player_availability,
    index_played_names,
    load_team_key_players_by_team_season,
)
from app.models_ml.markets import GOALS_LINES, over_under_probs  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ML_DIR = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ML_DIR / "artifacts"

# EPL has real TheRundown odds coverage; Brasileirão doesn't (confirmed live, see CLAUDE.md) —
# only EPL contributes to build_training_examples' moneyline_implied_prob_home/ROI metric.
# Pooled across every league with a collected game log. MLS/CSL/Scottish Premiership were
# added to fix a MEASURED problem: they are served by this same model but were absent from its
# training data, and their Over/Under-goals probabilities were overconfident as a result.
# The real cause turned out NOT to be "MLS scores more than EPL" (it does not - both average
# ~2.93 goals/match); it is that Brasileirao is a genuine low-scoring outlier (2.41), so
# pooling only EPL+Brasileirao biased the model toward P(under 3.5)~0.79 when MLS/CSL truly
# sit at ~0.66. Adding them rebalances the pool toward the real distribution.
#
# The four European leagues below join for a different reason: not to correct a measured
# distribution problem, but because they are seeded, ingest fixtures and odds today, and their
# seasons open mid-to-late August 2026 — at which point this model starts serving leagues it
# has never seen. They are safe to pool precisely because they are NOT outliers (~2.5-3.2
# goals/match against EPL's 2.93), which matters given league-identity features were built,
# measured, and regressed: pooling means one shared prior, so what joins it has to fit it.
LEAGUES = [
    "epl",
    "brasileirao",
    "mls",
    "csl",
    "scottish_prem",
    "bundesliga",
    "seriea",
    "laliga",
    "ligue1",
]

# 5-fold out-of-fold Layer 1 predictions for Layer 2 training (see oof_xg). Deterministic
# fold order; 5 is the standard choice and keeps each fold at ~1,000 training fixtures.
OOF_FOLDS = 5

TRAIN_SEASONS = [2021, 2022, 2023]
VAL_SEASON = 2024
TEST_SEASON = 2025

CLASSES = (
    FootballModel.CLASSES
)  # ("home", "draw", "away") — fixes the label encoding below
LABEL_BY_CLASS = {cls: i for i, cls in enumerate(CLASSES)}


async def _load_team_key_players() -> dict[tuple[str, int], list[dict]]:
    """Thin wrapper around the shared app/models_ml/historical_key_players.py loader — needs
    its own async_session_factory() context since that module's function takes an already-open
    db session (backfill_predictions.py already has one open when it calls it directly).
    """
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        return await load_team_key_players_by_team_season(db)


def ranked_probability_score(
    probs: np.ndarray, actual_label: int, n_classes: int = 3
) -> float:
    """Real RPS for an ordinal 3-way market (home/draw/away treated as ordered from
    home-favorable to away-favorable, matching FootballModel.CLASSES's own order) — the
    metric TDD §3.3 specifies for football, unlike NBA's 2-outcome shortcut
    (train_nba.py's `rps = brier`, valid only because a 2-outcome market has just one
    cumulative-probability boundary)."""
    cum_forecast = np.cumsum(probs)
    cum_actual = np.cumsum(
        [1.0 if i == actual_label else 0.0 for i in range(n_classes)]
    )
    return float(np.sum((cum_forecast - cum_actual) ** 2) / (n_classes - 1))


def build_training_examples(
    games: pd.DataFrame,
    odds: pd.DataFrame,
    lineups: pd.DataFrame,
    corners: pd.DataFrame,
    team_key_players_by_team_season: dict,
    team_codes: dict[str, str],
) -> pd.DataFrame:
    """One row per fixture (from the home team's perspective) — features via
    assemble_from_game_log (the same function run_predictions.py's live path calls through
    assemble_from_live_db), label in {0: home win, 1: draw, 2: away win}.

    team_codes: API-Football team_id -> its own 3-letter code, used only to join against
    TheRundown's teams_normalized.abbreviation for the odds sample — a best-effort
    cross-provider match (see collect_football_data.py:_fetch_team_codes), not a guaranteed
    one; unmatched fixtures simply get moneyline_implied_prob_home=None.

    games may pool multiple leagues (see LEAGUES) — elo_history is computed once here, up
    front, over the FULL pooled game log (see app/models_ml/elo.py: this must be a single
    chronological walk, not re-derived per row); safe to pool across leagues since team IDs
    never collide between them (no team plays in both), so each team's Elo state only ever
    evolves from its own league's matches."""
    # A real, non-masked-sentinel data-quality issue found while adding this pooled retrain:
    # a small number of rows in the collected odds sample carry an implausibly extreme decimal
    # price (e.g. 100.0, matching an American +9900 line) — not the known masked-book 0.0001
    # sentinel (_american_to_decimal already filters that), just a bad/stale real quote from one
    # of the 3 unlocked affiliates. Left in, .max() picks these as "best odds" and both the ROI
    # metric and moneyline_implied_prob_home training feature get a nonsensical near-zero
    # implied probability. 15.0 decimal (~6.25% implied) is a generous cap — no real EPL/
    # Brasileirão moneyline is plausibly longer than that.
    PLAUSIBLE_MAX_DECIMAL_ODDS = 15.0
    odds = odds[odds["home_odds"] <= PLAUSIBLE_MAX_DECIMAL_ODDS]
    best_odds = odds.groupby(["date", "home_short", "away_short"], as_index=False)[
        "home_odds"
    ].max()
    odds_lookup = {
        (row.date, row.home_short, row.away_short): row.home_odds
        for row in best_odds.itertuples()
    }
    played_names_index = index_played_names(lineups)
    elo_history = compute_elo_history(games)
    # Same "walk the pooled log once" contract as Elo — a running per-league state that no
    # individual fixture can re-derive without rescanning the whole log.
    league_baselines = compute_league_baselines(games)
    # {(FIXTURE_ID, TEAM_ID): corner count} — training TARGET only for the new corners-
    # Poisson-regressors (app/models_ml/football.py); never a live feature (see
    # collect_football_data.py:collect_corner_stats's own docstring for why).
    corners_by_fixture_team = {
        (row.FIXTURE_ID, row.TEAM_ID): row.CORNERS for row in corners.itertuples()
    }

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
            played_names_index,
            team_key_players_by_team_season,
            home_id,
            season,
            fixture_id,
        )
        key_avail_away, key_per_away = historical_key_player_availability(
            played_names_index,
            team_key_players_by_team_season,
            away_id,
            season,
            fixture_id,
        )

        # home_row, not a bare `row` — this loop iterates groupby groups, not itertuples.
        baseline = league_baselines.get(home_row.get("LEAGUE"), game_date)
        elo_home = elo_history.get((fixture_id, home_id))
        elo_away = elo_history.get((fixture_id, away_id))
        elo_diff = (
            (elo_home - elo_away)
            if elo_home is not None and elo_away is not None
            else None
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
            elo_diff=elo_diff,
            league_avg_goals=(baseline.avg_goals if baseline else None),
            league_home_win_rate=(baseline.home_win_rate if baseline else None),
        )
        features["label"] = LABEL_BY_CLASS[
            (
                "home"
                if home_row["WDL"] == "W"
                else ("draw" if home_row["WDL"] == "D" else "away")
            )
        ]
        features["season"] = season
        features["fixture_id"] = fixture_id
        features["home_goals"] = home_row["GF"]
        features["away_goals"] = home_row["GA"]
        features["home_odds"] = home_odds  # ROI metric only, not a model input
        features["home_corners"] = corners_by_fixture_team.get((fixture_id, home_id))
        features["away_corners"] = corners_by_fixture_team.get((fixture_id, away_id))
        rows.append(features)

    return pd.DataFrame(rows)


async def _register_model(
    artefact_path: Path, rps: float, accuracy: float, roi: float | None
) -> None:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.predictions.models import ModelRegistry
    from app.sports.models import Sport

    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one()

        existing_active = (
            (
                await db.execute(
                    select(ModelRegistry).where(
                        ModelRegistry.sport_id == sport.id,
                        ModelRegistry.is_active.is_(True),
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
                # Filename only, never a full path: the row must resolve on a dev laptop
                # AND in a Linux container, since promotion is a DB update rather than a
                # redeploy (TDD 3.1). app/models_ml/base.py resolves it against MODELS_DIR.
                artefact_path=artefact_path.name,
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

    # A league's GAME LOG is required (it's the actual training signal); its lineups and
    # corners are optional, and a league missing them still contributes.
    #
    # This is a deliberate consequence of collect_football_data.py's staged collection: a game
    # log costs ~1 API call per league-season, while lineups and corners cost 1 call PER
    # FIXTURE (~9,800 for the three leagues added to fix the measured out-of-distribution
    # calibration problem — past API-Football's 7,500/day ceiling). Requiring all three would
    # mean the goal-distribution fix, which is the whole point, waits days on data that only
    # feeds secondary features. A league with no lineups simply gets None key-player features
    # and contributes nothing to the corners regressors' training target — both already handled
    # (XGBoost's own missing-value handling; corners_by_fixture_team.get() returning None).
    # LEAGUE is stamped on during the concat: the per-league parquets carry no league column
    # of their own, so pooling them previously destroyed league identity entirely — which is
    # precisely why the model could not tell a 2.41-goals-per-match league from a 2.93 one.
    games = pd.concat(
        [
            pd.read_parquet(DATA_DIR / f"football_game_log_{league}.parquet").assign(
                LEAGUE=league
            )
            for league in LEAGUES
        ],
        ignore_index=True,
    )

    def _load_optional(kind: str, columns: list[str]) -> pd.DataFrame:
        frames = []
        for league in LEAGUES:
            path = DATA_DIR / f"football_{kind}_{league}.parquet"
            if path.exists():
                frames.append(pd.read_parquet(path))
            else:
                print(f"  no {kind} collected for {league} yet — training without them")
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)

    lineups = _load_optional("lineups", ["FIXTURE_ID", "TEAM_ID", "PLAYER_NAME"])
    corners = _load_optional("corners", ["FIXTURE_ID", "TEAM_ID", "CORNERS"])
    # Attaches CORNERS_FOR/CORNERS_AGAINST onto `games` for the new corners-rolling features
    # (app/models_ml/football_features.py:_corners_rolling) — `corners` itself stays the raw
    # FIXTURE_ID/TEAM_ID/CORNERS frame build_training_examples still needs separately for the
    # corners regressors' own TARGET (home_corners/away_corners), an unrelated use of the same
    # data.
    games = merge_corners_into_game_log(games, corners)
    # Rolling xG (TheStatsAPI — API-Football supplies none at any tier). Unlike corners, these
    # four features ARE part of FEATURE_NAMES, so they reach Layer 1's goals regressors: that
    # is the entire point, since rolling xG measured r=+0.129 against actual total goals where
    # rolling goals managed +0.062 and couldn't be told apart from noise. Coverage is genuinely
    # partial for now (EPL from 22/23, Brasileirão collecting; MLS/CSL/Scottish Prem none yet),
    # and the left join leaves those as NaN for XGBoost to treat as missing rather than
    # excluding the leagues.
    xg = _load_optional("xg", ["FIXTURE_ID", "TEAM_ID", "XG_FOR"])
    games = merge_xg_into_game_log(games, xg)
    if not xg.empty:
        covered = games["XG_FOR"].notna().sum()
        print(f"  xG merged: {covered}/{len(games)} game-log rows carry a real xG value")
    team_codes: dict[str, str] = {}
    for league in LEAGUES:
        codes_path = DATA_DIR / f"football_team_codes_{league}.parquet"
        if codes_path.exists():
            codes_df = pd.read_parquet(codes_path)
            team_codes.update(
                dict(zip(codes_df["team_id"], codes_df["code"], strict=False))
            )

    # Odds: EPL only (see module docstring) — a league with no *_odds_sample.parquet at all
    # (Brasileirão) simply contributes nothing here, not an error.
    odds_frames = []
    for league in LEAGUES:
        odds_path = DATA_DIR / f"football_odds_sample_{league}.parquet"
        if odds_path.exists():
            odds_frames.append(pd.read_parquet(odds_path))
    odds = (
        pd.concat(odds_frames, ignore_index=True)
        if odds_frames
        else pd.DataFrame(columns=["date", "home_short", "away_short", "home_odds"])
    )

    print(
        "loading team_key_players (Stage 1 — run compute_football_key_players.py first)..."
    )
    team_key_players_by_team_season = await _load_team_key_players()
    print(f"  {len(team_key_players_by_team_season)} (team, season) entries loaded")

    print(
        "assembling training examples (leakage-safe: every stat filtered to GAME_DATE < fixture date)..."
    )
    examples = build_training_examples(
        games, odds, lineups, corners, team_key_players_by_team_season, team_codes
    )
    print(
        f"{len(examples)} examples, moneyline available for {examples['home_odds'].notna().sum()}"
    )

    train_df = examples[examples["season"].isin(TRAIN_SEASONS)]
    val_df = examples[examples["season"] == VAL_SEASON]
    test_df = examples[examples["season"] == TEST_SEASON]
    print(f"train={len(train_df)} val={len(val_df)} test={len(test_df)}")

    feature_cols = list(FEATURE_NAMES)
    X_train = train_df[feature_cols].astype(float)
    X_val = val_df[feature_cols].astype(float)
    X_test = test_df[feature_cols].astype(float)

    # --- Layer 1: Poisson expected-goals regressors ---------------------------------------
    layer1_home_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=200, max_depth=4
    )
    layer1_home_model.fit(X_train, train_df["home_goals"].astype(float))
    layer1_away_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=200, max_depth=4
    )
    layer1_away_model.fit(X_train, train_df["away_goals"].astype(float))

    # --- Corners-Poisson-regressors (Over/Under corners market, app/models_ml/markets.py) ---
    # Now trained on Layer 1's 21 features PLUS 4 corners-specific rolling ones
    # (CORNERS_FEATURE_NAMES — each team's own rolling average corners won/conceded, from our
    # own accumulating fixture_live_state at serving time, see football_features.py's module
    # docstring for why this replaces the earlier "just reuse Layer 1's vector" simplification).
    # Rows missing a real corner count (not every historical fixture's /fixtures/statistics
    # call returned one) are excluded from fitting, not zero-filled.
    corners_feature_cols = list(CORNERS_FEATURE_NAMES)
    X_train_corners = train_df[corners_feature_cols].astype(float)
    X_val_corners = val_df[corners_feature_cols].astype(float)
    X_test_corners = test_df[corners_feature_cols].astype(float)

    corners_train_mask = (
        train_df["home_corners"].notna() & train_df["away_corners"].notna()
    )
    corners_val_mask = val_df["home_corners"].notna() & val_df["away_corners"].notna()
    corners_test_mask = (
        test_df["home_corners"].notna() & test_df["away_corners"].notna()
    )
    print(
        f"corners training rows: {corners_train_mask.sum()}/{len(train_df)} "
        f"(test rows with real corner counts: {corners_test_mask.sum()}/{len(test_df)})"
    )
    corners_home_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=200, max_depth=4
    )
    corners_home_model.fit(
        X_train_corners[corners_train_mask],
        train_df.loc[corners_train_mask, "home_corners"].astype(float),
    )
    corners_away_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=200, max_depth=4
    )
    corners_away_model.fit(
        X_train_corners[corners_train_mask],
        train_df.loc[corners_train_mask, "away_corners"].astype(float),
    )
    if corners_test_mask.sum() > 0:
        corners_home_pred = corners_home_model.predict(
            X_test_corners[corners_test_mask]
        )
        corners_away_pred = corners_away_model.predict(
            X_test_corners[corners_test_mask]
        )
        corners_mae = (
            float(
                np.mean(
                    np.abs(
                        corners_home_pred
                        - test_df.loc[corners_test_mask, "home_corners"]
                    )
                )
                + np.mean(
                    np.abs(
                        corners_away_pred
                        - test_df.loc[corners_test_mask, "away_corners"]
                    )
                )
            )
            / 2
        )
        print(
            f"corners regressors test MAE (avg of home/away, corners): {corners_mae:.3f}"
        )
    else:
        corners_mae = None

    def add_xg(df: pd.DataFrame, X: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["xg_home"] = layer1_home_model.predict(X).clip(min=0)
        df["xg_away"] = layer1_away_model.predict(X).clip(min=0)
        return df

    def oof_xg(X: pd.DataFrame, y_home, y_away) -> tuple[np.ndarray, np.ndarray]:
        """Out-of-fold Layer 1 predictions for the TRAINING split.

        Layer 2 previously trained on Layer 1's own in-sample predictions, which was a
        documented simplification in this module's header. The cost turned out to be
        substantial rather than cosmetic: XGBoost fits its training data closely, so Layer 2
        learned to trust unrealistically clean xG and then met noisier values in production.

        Measured on the full 8,718 examples, changing only this:
            1X2 accuracy  0.4273 -> 0.4656   (+3.8 pts)
            RPS           0.2869 -> 0.2286   (-20%)

        Note this is the opposite direction to the external review that prompted the test,
        which predicted OOF would DEFLATE an inflated edge. It raises it, because the bias was
        Layer 2 over-trusting its input rather than the score being flattered.

        Fold order is deterministic (shuffle=False) so training stays reproducible bit-for-bit
        — which is what makes a single run a sufficient measurement here."""
        home_oof = np.zeros(len(X))
        away_oof = np.zeros(len(X))
        for fit_idx, held_idx in KFold(n_splits=OOF_FOLDS, shuffle=False).split(X):
            fold_home = xgb.XGBRegressor(
                objective="count:poisson", n_estimators=200, max_depth=4
            )
            fold_away = xgb.XGBRegressor(
                objective="count:poisson", n_estimators=200, max_depth=4
            )
            fold_home.fit(X.iloc[fit_idx], y_home.iloc[fit_idx])
            fold_away.fit(X.iloc[fit_idx], y_away.iloc[fit_idx])
            home_oof[held_idx] = fold_home.predict(X.iloc[held_idx]).clip(min=0)
            away_oof[held_idx] = fold_away.predict(X.iloc[held_idx]).clip(min=0)
        return home_oof, away_oof

    # Validation and test use the full-fit Layer 1, exactly as production serving does; only
    # the TRAINING split needs out-of-fold values, since that is where the optimism entered.
    train_df = train_df.copy()
    train_df["xg_home"], train_df["xg_away"] = oof_xg(
        X_train, train_df["home_goals"].astype(float), train_df["away_goals"].astype(float)
    )
    val_df = add_xg(val_df, X_val)
    test_df = add_xg(test_df, X_test)

    # --- Regression calibration for xG/corners (Over/Under markets) -----------------------
    # A real, found-in-production issue, not a theoretical one: Over/Under goals/corners
    # (app/models_ml/markets.py) apply a raw Poisson CDF straight to Layer 1's own
    # uncalibrated output, and for fixtures whose feature distribution this EPL/Brasileirão
    # training data barely covers (Scottish Premiership/MLS/CSL fixtures reusing this same
    # model, see CLAUDE.md), that produced implausibly extreme (99-100%) Over/Under
    # probabilities even with real, populated TeamFeatures. IsotonicRegression works here for
    # the same reason it works for the 1X2 classes above — it's just calibrating a different
    # monotonic output (a predicted RATE, not a class probability) against its own real,
    # empirically-observed value on validation-set fixtures. y_min=0.0 only (goals/corners
    # can't be negative) — no y_max, unlike the 1X2 calibrators' [0.001, 0.999] probability
    # bound, since a rate isn't bounded to [0, 1].
    xg_home_calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0)
    xg_home_calibrator.fit(val_df["xg_home"], val_df["home_goals"].astype(float))
    xg_away_calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0)
    xg_away_calibrator.fit(val_df["xg_away"], val_df["away_goals"].astype(float))

    corners_val_pred_home = corners_home_model.predict(X_val_corners[corners_val_mask])
    corners_val_pred_away = corners_away_model.predict(X_val_corners[corners_val_mask])
    corners_home_calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0)
    corners_home_calibrator.fit(
        corners_val_pred_home,
        val_df.loc[corners_val_mask, "home_corners"].astype(float),
    )
    corners_away_calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0)
    corners_away_calibrator.fit(
        corners_val_pred_away,
        val_df.loc[corners_val_mask, "away_corners"].astype(float),
    )

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
        test_df["home_odds"].notna()
        & (test_calibrated[:, home_idx] == test_calibrated.max(axis=1))
    ]
    if len(roi_rows) > 0:
        stakes = len(roi_rows)
        returns = sum(
            row.home_odds if row.label == home_idx else 0.0
            for row in roi_rows.itertuples()
        )
        flat_stake_roi = (returns - stakes) / stakes
    else:
        flat_stake_roi = None

    print(
        f"test accuracy={accuracy:.4f} (baseline={baseline_accuracy:.4f}) RPS={rps:.4f}"
    )
    print(
        f"flat-stake ROI (home picks with real odds, n={len(roi_rows)}): {flat_stake_roi}"
    )

    # Honest before/after check on the real test set — same "report it straight, not spun
    # positively" convention as the corners-rolling-features MAE result already in CLAUDE.md.
    xg_home_raw_mae = float(np.mean(np.abs(test_df["xg_home"] - test_df["home_goals"])))
    xg_home_calibrated_mae = float(
        np.mean(
            np.abs(
                xg_home_calibrator.predict(test_df["xg_home"]) - test_df["home_goals"]
            )
        )
    )
    xg_away_raw_mae = float(np.mean(np.abs(test_df["xg_away"] - test_df["away_goals"])))
    xg_away_calibrated_mae = float(
        np.mean(
            np.abs(
                xg_away_calibrator.predict(test_df["xg_away"]) - test_df["away_goals"]
            )
        )
    )
    print(
        f"xg_home MAE: raw={xg_home_raw_mae:.4f} calibrated={xg_home_calibrated_mae:.4f} "
        f"| xg_away MAE: raw={xg_away_raw_mae:.4f} calibrated={xg_away_calibrated_mae:.4f}"
    )

    # Over/Under goals evaluation — this market had NO held-out evaluation of any kind until
    # now, which is the honest root cause of it shipping visibly overconfident: 1X2 has had
    # accuracy/RPS from the start, but nobody had ever measured whether a stated "85% chance of
    # under 3.5" was right. A one-off query against completed fixtures showed the 0.8-0.9 band
    # delivering ~0.72. Measuring it here, every run, is what stops that recurring.
    #
    # Brier score (lower is better) plus a reliability table: predicted-probability bucket vs
    # the frequency the event ACTUALLY occurred. A well-calibrated model has actual ≈ bucket
    # midpoint; a systematic positive gap is overconfidence. Reported per line, since 1.5/2.5/
    # 3.5 have very different base rates and an aggregate number would hide a bad one.
    actual_totals = (test_df["home_goals"] + test_df["away_goals"]).to_numpy()
    xg_totals = (
        xg_home_calibrator.predict(test_df["xg_home"])
        + xg_away_calibrator.predict(test_df["xg_away"])
    )
    over_under_metrics: dict[str, float] = {}
    print("Over/Under goals — held-out calibration (predicted vs actually observed):")
    for line in GOALS_LINES:
        predicted_under = np.array(
            [over_under_probs(float(t), (line,))[line][0] for t in xg_totals]
        )
        actual_under = (actual_totals < line).astype(float)
        brier = float(np.mean((predicted_under - actual_under) ** 2))
        over_under_metrics[f"ou_{line}_brier"] = brier
        # Mean predicted vs mean actual across the whole test set: the single clearest
        # overconfidence signal, and directly comparable to the live measurement above.
        mean_gap = float(predicted_under.mean() - actual_under.mean())
        over_under_metrics[f"ou_{line}_mean_gap"] = mean_gap
        print(
            f"  under {line}: brier={brier:.4f} "
            f"mean predicted={predicted_under.mean():.3f} actual={actual_under.mean():.3f} "
            f"gap={mean_gap:+.3f}"
        )
        for lo in (0.5, 0.6, 0.7, 0.8, 0.9):
            in_bucket = (predicted_under >= lo) & (predicted_under < lo + 0.1)
            n = int(in_bucket.sum())
            if n >= 20:  # below this a bucket rate is noise, not a signal
                print(
                    f"      predicted {lo:.1f}-{lo + 0.1:.1f}: n={n:4d} "
                    f"actual={actual_under[in_bucket].mean():.3f}"
                )

    artefact_path = (
        ARTIFACT_DIR / f"football_xgb_{datetime.now(UTC):%Y%m%d%H%M%S}.joblib"
    )
    joblib.dump(
        {
            "layer1_home_model": layer1_home_model,
            "layer1_away_model": layer1_away_model,
            "layer1_feature_names": feature_cols,
            "layer2_model": layer2_model,
            "calibrators": calibrators,
            "corners_home_model": corners_home_model,
            "corners_away_model": corners_away_model,
            "corners_feature_names": corners_feature_cols,
            "xg_home_calibrator": xg_home_calibrator,
            "xg_away_calibrator": xg_away_calibrator,
            "corners_home_calibrator": corners_home_calibrator,
            "corners_away_calibrator": corners_away_calibrator,
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
        if corners_mae is not None:
            mlflow.log_metric("corners_test_mae", corners_mae)
        mlflow.log_metric("xg_home_raw_mae", xg_home_raw_mae)
        mlflow.log_metric("xg_home_calibrated_mae", xg_home_calibrated_mae)
        mlflow.log_metric("xg_away_raw_mae", xg_away_raw_mae)
        mlflow.log_metric("xg_away_calibrated_mae", xg_away_calibrated_mae)
        for metric_name, metric_value in over_under_metrics.items():
            mlflow.log_metric(metric_name, metric_value)
        mlflow.log_artifact(str(artefact_path))

    await _register_model(artefact_path, rps, accuracy, flat_stake_roi)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
