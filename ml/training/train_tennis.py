"""Train the first real tennis (ATP) win-probability model.

Temporal split, same shape as train_nba.py/train_football.py — clean season boundaries:
  - train: 2021, 2022, 2023
  - validate: 2024
  - test: 2025 (the most recently completed season)

Optuna: 50 trials, isotonic calibration fit on validation predictions, final model re-trained
on train+val with the best hyperparameters — identical recipe to train_nba.py (single binary
classifier, no draw, since tennis is also a 2-outcome sport — NOT football's two-layer stack).

No historical odds were collected (see collect_tennis_data.py) — moneyline_implied_prob_home
is None for every training example, and flat-stake ROI is therefore always None for this
model too, an honest gap, not a bug.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/train_tennis.py
"""

import argparse
import asyncio
import os
import sys
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")  # see train_nba.py for why

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
from app.models_ml.tennis_features import (  # noqa: E402
    FEATURE_NAMES,
    assemble_from_game_log,
)
from collect_tennis_data import TOUR  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ML_DIR = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ML_DIR / "artifacts"

TRAIN_SEASONS = [2021, 2022, 2023]
VAL_SEASON = 2024
TEST_SEASON = 2025

N_OPTUNA_TRIALS = 50
# Matches train_football.py's pinned seed so runs are comparable to each other; the value
# itself is arbitrary, its fixedness is not.
RANDOM_SEED = 20260811

# === PRE-REGISTERED 2026-08-19, BEFORE THE RANK-SCALE ARMS WERE RUN =========================
#
# THE DEFECT BEING FIXED, measured on the 2025 test season of the then-served model
# (tennis_xgb_20260813165827) before any code changed:
#
#     agreement with "back the higher-ranked player"   88.4%   (n=3,902)
#     model accuracy 64.20%   ranking baseline 62.22%          gap +1.98pp
#
#     rank gap          higher-ranked wins   model acc   agreement
#     0-250   n=1307          55.4%            59.4%        74%
#     250-500 n= 794          56.5%            58.4%        91%
#     500-1k  n= 640          63.4%            64.1%        96%
#     1k-2k   n= 541          68.6%            69.5%        98%
#     2k+     n= 620          77.1%            77.1%       100%   <- never deviates at all
#
# CAUSE: rank_diff is a RAW POINTS SUBTRACTION, and ranking points are wildly non-linear in
# position. Measured on the live table: dropping ten places costs 8,720 points at #1 but 119
# points at #50 -- 73x. One number therefore cannot distinguish "#3 v #5" from "#40 v #90",
# and past a ~250-point gap NO amount of form advantage flips the pick (an extreme form
# reversal moves the probability 10-18 points, never across 0.5). The form/streak/surface
# features are NOT missing at serving time -- verified live against BallDontLie -- and the
# model does respond to them (0.31 -> 0.69 with rank held equal). They are simply outvoted.
#
# THE ARMS: baseline (14 features, unchanged) against treatment (16 -- adding
# rank_position_diff, linear in ranking PLACES, and rank_log_points_ratio, a compressed
# points signal). Both seeded, both run with --no-activate, everything else identical.
#
# ADOPT ONLY IF ALL THREE HOLD:
#   PRIMARY   ranking_baseline_gap improves by >= 1.0pp. This is the metric the defect is
#             ABOUT -- a model that merely reproduces the ranking table has no edge to sell,
#             and it is what a user experiences as "it always just picks the favourite".
#   GUARD     RPS (brier) must not worsen by more than 0.0020. Deviating more is only progress
#             if the probabilities stay honest.
#   GUARD     test accuracy must not fall by more than 0.5pp.
#
# NOT A CRITERION, reported as a diagnostic only: agreement with the ranking baseline. Lower
# agreement is not by itself good -- deviating and being WRONG is worse than deferring. The
# gap already encodes "deviates AND is right", which is why it is primary and this is not.
#
# Tolerances are stated up front deliberately: the rolling-form pre-registration on this
# project specified none, so a 0.05% move read as failure and had to be overridden after the
# fact. See CLAUDE.md.
#
# --- RESULT, 2026-08-19: FAILED, REVERTED ---------------------------------------------------
#
#                          baseline (14)   treatment (16)   bar
#     ranking gap             +2.15pp         +1.77pp       >= +3.15pp   FAIL (moved BACKWARDS)
#     RPS                      0.2275          0.2302       <= 0.2295    FAIL
#     accuracy                 0.6377          0.6338       >= 0.6327    pass
#     agreement (diagnostic)    87.8%           88.2%        --          ROSE, did not fall
#
# Two of three criteria failed including the primary, so the arm was reverted by the letter --
# SPORTIQ_TENNIS_RANK_SCALE_FEATURES now defaults OFF and the served vector is 14 again.
# Baseline reproduced the incumbent exactly (0.6377 / 0.2275 / +2.15pp), so this is a real
# comparison rather than run-to-run noise. Both arms ran --no-activate and the incumbent
# tennis_xgb_v20260813023503 was never disturbed.
#
# WHAT THIS RULES OUT, which is the value of the negative result: the model's deference to
# ranking is NOT an artefact of the points scale. Given a linear-in-places signal and a
# compressed-ratio signal, it deviated slightly LESS, not more. So ranking points are not
# merely a coarse proxy the model is forced to lean on -- at wide gaps they are genuinely the
# best information available, which the reality column already said (the higher-ranked player
# really does win 77.1% of matches past a 2,000-point gap).
#
# THE DEFECT IS REAL AND REMAINS OPEN. Where the model SHOULD be able to add value is the
# 0-250 band, which is a third of all matches and where the higher-ranked player wins only
# 55.4%; the model manages 59.3% there. A future attempt should carry information ranking
# CANNOT contain rather than re-expressing what it already does -- opponent-adjusted recent
# form (beating a top-10 player counts more than beating #300, which a flat win rate ignores),
# or within-tournament fatigue (sets/minutes played this week). Neither is collected today.
# ===========================================================================================

# === PRE-REGISTERED 2026-08-19, BEFORE THE OPPONENT-ADJUSTED FORM ARM WAS RUN ================
#
# THE PROBLEM, in the user's own words: "Winning 7 games over very weak opponents currently
# counts as in form, but this could be well misleading." Correct, and it is the gap the FAILED
# rank-scale arm above pointed at. form_win_rate is a flat, opponent-blind win rate, while ATP
# ranking points are explicitly weighted by tournament tier and round reached -- so a flat win
# rate can never out-argue ranking, because ranking already knows something it does not.
#
# THE ARM adds 6 features (14 -> 20), all derivable from data already collected:
#
#   form_vs_expected_home/away      SUM(actual - expected) / 10 over the last 10 matches,
#                                   where expected = logistic(BETA * log points ratio) against
#                                   each opponent's own point-in-time ranking. Zero means
#                                   "performed exactly as the rankings predicted"; beating 7
#                                   weak opponents scores NEGATIVE. This is a RESIDUAL, which
#                                   is why it is not simply rank restated -- it measures what
#                                   ranking does not already say, by construction.
#   opponent_quality_faced_home/away  mean log ranking points of the last 10 opponents.
#   rank_momentum_home/away         log(points now) - log(points RANK_MOMENTUM_WEEKS ago):
#                                   surging versus coasting on a rank built ten months ago.
#
# BETA = 0.6460 was fitted on the TRAIN SEASONS ONLY (2021-2023, 19,594 pairs) and pinned as a
# constant, so no fitting touches validation or test. Its calibration on that split: predicted
# 0.149/0.288/0.432/0.568/0.712/0.851 against actual 0.186/0.265/0.427/0.573/0.735/0.814.
#
# ADOPT ONLY IF ALL THREE HOLD -- deliberately the SAME bar as the failed rank-scale arm, so
# the two are directly comparable:
#   PRIMARY   ranking_baseline_gap improves by >= 1.0pp (baseline +2.15pp, so >= +3.15pp).
#   GUARD     RPS must not worsen by more than 0.0020 (baseline 0.2275, so <= 0.2295).
#   GUARD     test accuracy must not fall by more than 0.5pp (baseline 0.6377, so >= 0.6327).
#
# REPORTED BUT NOT A CRITERION: the 0-250 ranking-gap band, which is a third of all matches and
# where the favourite wins only 55.4%. That is where the feature SHOULD bite, and it is worth
# seeing even on a failing arm -- but promoting it to a criterion after the fact would be
# choosing the cut that flatters the result.
#
# SERVING IS DELIBERATELY NOT WIRED YET. Feasibility was confirmed first (the last 10
# opponents' current rankings come back in ONE batched /rankings?player_ids[]=... call, under
# the 100-id cap), but two consecutive tennis arms have now failed, so the measurement runs
# before the serving work rather than after it. The toggle defaults OFF and a test pins that,
# so nothing can ship half-wired.
#
# --- RESULT, 2026-08-19: FAILED, NOT ADOPTED ------------------------------------------------
#
#                          baseline (14)   treatment (20)   bar
#     ranking gap             +2.15pp         +2.13pp       >= +3.15pp   FAIL (flat)
#     RPS                      0.2275          0.2324       <= 0.2295    FAIL (clearly worse)
#     accuracy                 0.6377          0.6362       >= 0.6327    pass
#     agreement (diagnostic)    87.8%           87.9%        --          unchanged
#
# THE ARM WAS ALREADY IN TROUBLE BEFORE TRAINING, and this is the part worth keeping. Measured
# on the assembled examples (96% coverage, so not a data gap):
#
#     corr(form_vs_expected, plain form_win_rate) = +0.843   <- NOT new information
#     corr(form_vs_expected, outcome)             = +0.080
#     corr(plain form_win_rate, outcome)          = +0.136   <- the flat version predicts BETTER
#
# So the residual is mostly a noisier restatement of the flat win rate. The intuition -- that
# beating seven qualifiers should not read as "in form" -- is sound, and the arithmetic does
# express it; what is false is the assumption that opponent strength varies enough ACROSS a
# player's last ten matches to carry signal. Draws are seeded, so a player's opponents cluster
# near their own level by construction, and the residual mostly cancels.
#
# THE ONE ENCOURAGING NUMBER, and it does NOT rescue the arm: the 0-250 ranking-gap band moved
# 0.5931 -> 0.6031, the exact band predicted. That is +1.00pp against a standard error of
# 1.36pp on n=1305 -- inside noise. It was ALSO pre-registered as "reported but not a
# criterion" precisely so it could not be promoted after the fact, and it is not being
# promoted now.
#
# WHAT IS NOW RULED OUT for tennis: re-scaling the ranking signal (the arm above), and
# re-weighting recent form by opponent strength (this one). Both tried to extract more from
# information the ranking already contains. A third attempt should use data the ranking cannot
# contain at all -- within-tournament fatigue (sets and minutes played this week), or
# serve/return match statistics, which /match_stats carries and no feature currently reads.
# ===========================================================================================



def _iso_monday(d):
    return d - timedelta(days=d.weekday())


def build_training_examples(games: pd.DataFrame, rank_points: pd.DataFrame) -> pd.DataFrame:
    """One row per match (from player1/"home"'s perspective) — features via
    assemble_from_game_log (the same function run_predictions.py's live path calls through
    assemble_from_live_db), label = 1 if the home-slot player won."""
    rank_lookup = {(row.PLAYER_ID, row.WEEK): row.RANK_POINTS for row in rank_points.itertuples()}
    # RANK_POSITION was added to the collector 2026-08-19. Tolerated as absent so an older
    # parquet still trains (the two position-scaled features simply score as missing, which
    # XGBoost handles) rather than crashing — the same _load_optional philosophy
    # train_football.py applies to a league with no corners collected yet.
    has_position = "RANK_POSITION" in rank_points.columns
    position_lookup = (
        {(row.PLAYER_ID, row.WEEK): row.RANK_POSITION for row in rank_points.itertuples()}
        if has_position
        else {}
    )
    if not has_position:
        print("  rank parquet has no RANK_POSITION column — position features will be missing")

    def rank_points_for(player_id: str, game_date) -> float | None:
        return rank_lookup.get((player_id, _iso_monday(game_date)))

    def rank_position_for(player_id: str, game_date) -> float | None:
        return position_lookup.get((player_id, _iso_monday(game_date)))

    # MOST RECENT snapshot ON OR BEFORE a date, for any player -- not an exact week hit like
    # the two lookups above. The opponent-adjusted features need each OPPONENT's ranking at the
    # time they were played, and an opponent did not necessarily play that same week, so an
    # exact-key lookup would miss most of them. Sorted arrays + bisect because this runs for
    # every one of ~17,700 examples x up to 10 prior matches x 2 players.
    _by_player: dict[str, tuple] = {}
    for pid, grp in rank_points.dropna(subset=["RANK_POINTS"]).groupby("PLAYER_ID"):
        grp = grp.sort_values("WEEK")
        _by_player[pid] = (grp["WEEK"].tolist(), grp["RANK_POINTS"].tolist())

    def rank_points_at(player_id: str, on_date) -> float | None:
        entry = _by_player.get(player_id)
        if entry is None:
            return None
        weeks, points = entry
        idx = bisect_right(weeks, _iso_monday(on_date)) - 1
        if idx < 0:
            return None
        value = points[idx]
        return float(value) if value and value > 0 else None

    rows = []
    for match_id, group in games.groupby("MATCH_ID"):
        home_rows = group[group["HOME_AWAY"] == "home"]
        away_rows = group[group["HOME_AWAY"] == "away"]
        if home_rows.empty or away_rows.empty:
            continue  # incomplete pair — skip defensively rather than guess
        home_row = home_rows.iloc[0]
        away_row = away_rows.iloc[0]

        game_date = home_row["GAME_DATE"]
        season = home_row["SEASON"]
        home_player = home_row["PLAYER_ID"]
        away_player = away_row["PLAYER_ID"]

        features = assemble_from_game_log(
            games,
            game_date,
            home_player,
            away_player,
            home_rank_points=rank_points_for(home_player, game_date),
            away_rank_points=rank_points_for(away_player, game_date),
            moneyline_implied_prob_home=None,
            home_rank_position=rank_position_for(home_player, game_date),
            away_rank_position=rank_position_for(away_player, game_date),
            rank_points_at=rank_points_at,
        )
        features["label"] = 1 if home_row["WL"] == "W" else 0
        features["season"] = season
        features["match_id"] = match_id
        # Carried for REPORTING only — neither is in FEATURE_NAMES, so neither reaches the
        # model. `surface` lets the ranking baseline below be cut per surface, and rank_diff is
        # already a feature but is read there as the baseline's own decision rule.
        features["surface"] = home_row["SURFACE"]
        rows.append(features)

    return pd.DataFrame(rows)


MIN_SURFACE_ROWS_TO_REPORT = 100


def _ranking_baseline_report(test_df, predicted_labels, model_accuracy: float) -> dict:
    """Score the model against BACK THE HIGHER-RANKED PLAYER, pooled and per surface.

    WHY THIS BASELINE AND NOT "ALWAYS PICK HOME". "Home" in tennis is the lower BallDontLie
    player id — a label-stability device, not a venue — so "always pick home" measures our own
    row ordering. Backing the higher-ranked player is the strategy a user could actually run
    without a model, which is what a baseline is supposed to represent. It is also what
    justified removing the tennis base-rate gate from app/fixtures/router.py: that gate was
    comparing picks against the id ordering, which inverted a quarter of them.

    PER SURFACE, because ranking is surface-blind and the model is not. ATP points are one
    number across all surfaces, while the model carries surface_win_rate, surface_streak and
    h2h_win_rate_surface precisely to know what the ranking does not. If those features earn
    their place, the model's edge should be LARGER on clay, where ranking is least reliable.
    Measured over 17,480 real matches the baseline itself barely moves by surface (Hard 0.6325,
    Clay 0.6202, Grass 0.6377 — a 1.75pp spread), so any per-surface difference in the GAP is
    the model's doing rather than the baseline's.

    Ties and missing ranks abstain rather than guess: a baseline that silently falls back to
    "pick home" on unranked players would smuggle the id ordering back in through the door this
    exists to close.
    """
    df = test_df.reset_index(drop=True).copy()
    df["model_correct"] = (
        pd.Series(predicted_labels).reset_index(drop=True) == df["label"]
    ).astype(int)
    rated = df[df["rank_diff"].notna() & (df["rank_diff"] != 0)].copy()
    if rated.empty:
        print("ranking baseline: no test rows carry rank points for both players — skipped")
        return {}
    # rank_diff = home_points - away_points, so > 0 means the home slot IS the higher-ranked
    # player and the baseline predicts label 1.
    rated["baseline_correct"] = ((rated["rank_diff"] > 0).astype(int) == rated["label"]).astype(int)

    pooled_base = float(rated["baseline_correct"].mean())
    pooled_model = float(rated["model_correct"].mean())
    metrics = {
        "ranking_baseline_accuracy": pooled_base,
        "ranking_baseline_n": float(len(rated)),
        "model_accuracy_on_ranked_subset": pooled_model,
        "ranking_baseline_gap": pooled_model - pooled_base,
    }
    print(
        "\nvs 'back the higher-ranked player' (the strategy a user could run without us):\n"
        f"  pooled   n={len(rated):<6} model={pooled_model:.4f} baseline={pooled_base:.4f} "
        f"gap={(pooled_model - pooled_base) * 100:+.2f}pp"
    )
    print(f"  (model accuracy over ALL test rows, including unranked: {model_accuracy:.4f})")

    # DIAGNOSTIC, EXPLICITLY NOT AN ADOPTION CRITERION (see the pre-registration block above):
    # how often the model simply reproduces the ranking table, overall and by how wide the
    # gap is. Reported because a user asked why picks "always favour the higher-ranked player"
    # and the answer turned out to be measurable -- 88.4% agreement, rising to 100% past a
    # 2,000-point gap. Lower agreement is NOT by itself an improvement: deviating and being
    # wrong is worse than deferring, which is what ranking_baseline_gap above already scores.
    rated["baseline_pick"] = (rated["rank_diff"] > 0).astype(int)
    rated["model_pick"] = pd.Series(predicted_labels).reset_index(drop=True)[rated.index]
    agreement = float((rated["model_pick"] == rated["baseline_pick"]).mean())
    metrics["ranking_baseline_agreement"] = agreement
    disagreements = rated[rated["model_pick"] != rated["baseline_pick"]]
    right_on_deviations = (
        float(disagreements["model_correct"].mean()) if len(disagreements) else float("nan")
    )
    print(
        f"  agreement with the ranking table: {agreement * 100:.1f}%  "
        f"({len(disagreements)} deviations, model right on "
        f"{right_on_deviations * 100:.1f}% of them)"
    )
    gap_bands = [(0, 250), (250, 500), (500, 1000), (1000, 2000), (2000, float("inf"))]
    print("  by ranking-points gap:")
    for lo, hi in gap_bands:
        band = rated[(rated["rank_diff"].abs() >= lo) & (rated["rank_diff"].abs() < hi)]
        if len(band) < 30:
            continue
        print(
            f"    {lo:>5}-{hi if hi != float('inf') else 'inf':<5} n={len(band):<5} "
            f"model={band['model_correct'].mean():.4f} "
            f"baseline={band['baseline_correct'].mean():.4f} "
            f"agreement={(band['model_pick'] == band['baseline_pick']).mean() * 100:.0f}%"
        )

    if "surface" in rated.columns:
        # Stripped defensively: the collected log holds both "Grass" and "Grass " and an
        # unstripped groupby splits the smaller stratum out of sight.
        rated["surface"] = rated["surface"].astype(str).str.strip()
        for surface, g in rated.groupby("surface"):
            if len(g) < MIN_SURFACE_ROWS_TO_REPORT:
                continue  # below this a surface rate is noise, not a signal
            b, m = float(g["baseline_correct"].mean()), float(g["model_correct"].mean())
            metrics[f"ranking_gap_{surface.lower()}"] = m - b
            metrics[f"ranking_baseline_{surface.lower()}"] = b
            print(
                f"  {surface:<8} n={len(g):<6} model={m:.4f} baseline={b:.4f} "
                f"gap={(m - b) * 100:+.2f}pp"
            )
    print(
        "  A gate against this baseline is pre-registered in app/fixtures/router.py at >=3pp\n"
        "  pooled AND not concentrated in one surface. Below that, no gate."
    )
    return metrics


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
    model = xgb.XGBClassifier(objective="binary:logistic", random_state=RANDOM_SEED, **params)
    model.fit(X_train, y_train)
    val_preds = model.predict_proba(X_val)[:, 1]
    return log_loss(y_val, val_preds)



# Set by --no-activate. A training run is often a MEASUREMENT rather than a promotion, and
# these scripts register-and-activate unconditionally, so the losing arm of an experiment
# silently becomes the model that serves users.
#
# Not hypothetical, and it has now happened twice. The football corners baseline left an
# inactive row (harmless). Then on 2026-08-13 a tennis form-window experiment at n=5 scored
# WORSE than the n=10 model on both accuracy (0.6312 vs 0.6377) and RPS (0.2298 vs 0.2275) and
# activated itself anyway. Worse than a bad number: LAST_N_FORM defaults back to 10 at serving
# time, so a model trained on 5-match form would have been fed 10-match form -- a silent
# train/serve mismatch introduced by the measurement itself. Caught before any prediction was
# generated with it, by luck of timing rather than by design.
ACTIVATE_ON_REGISTER = True

async def _register_model(artefact_path: Path, rps: float, accuracy: float) -> None:
    from app.core.database import async_session_factory
    from app.predictions.models import ModelRegistry
    from app.sports.models import Sport
    from sqlalchemy import select

    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "tennis"))).scalar_one()

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
        # Only demote the incumbent when something is actually replacing it. Demoting
        # unconditionally under --no-activate leaves the sport with NO active model at all, so
        # run_predictions has nothing to resolve and every prediction for that sport silently
        # stops -- which is exactly what happened to NBA on 2026-08-13 when three measurement
        # arms ran back to back and the third left the registry empty.
        if ACTIVATE_ON_REGISTER:
            for row in existing_active:
                row.is_active = False

        version = f"tennis_xgb_v{datetime.now(UTC):%Y%m%d%H%M%S}"
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
                roi_simulation=None,  # no historical odds collected — see module docstring
                trained_at=datetime.now(UTC),
                is_active=ACTIVATE_ON_REGISTER,
            )
        )
        await db.commit()
        print(
            f"registered models_registry row: {version} "
            f"(is_active={ACTIVATE_ON_REGISTER})"
        )


async def main_async() -> None:
    # Everything DB-touching (_register_model) runs inside this one asyncio.run() call — see
    # train_nba.py's own comment for why two separate asyncio.run() calls in one process
    # crash on Windows.
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    games = pd.read_parquet(DATA_DIR / f"tennis_game_log_{TOUR}.parquet")
    # Applied on load as well as at collection, because the already-collected parquet still
    # holds the unstripped values and re-collecting costs 17k rate-limited API calls. "Grass"
    # (4,296 rows) and "Grass " (386) are the same surface; unstripped they are two, which
    # splits the surface FEATURES — surface_win_rate, surface_streak and h2h_win_rate_surface
    # all match on this string, so 8% of grass matches were compared against the wrong pool.
    if "SURFACE" in games.columns:
        games["SURFACE"] = (
            games["SURFACE"].astype(str).str.strip().replace({"": None, "None": None})
        )
    rank_points = pd.read_parquet(DATA_DIR / f"tennis_rank_points_{TOUR}.parquet")

    print("assembling training examples (this walks every match with a leakage-safe filter)...")
    examples = build_training_examples(games, rank_points)
    print(f"{len(examples)} examples")

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
    # SEEDED, because a pre-registered threshold is meaningless against a number that moves on
    # its own. app/fixtures/router.py now commits to reinstating the tennis gate only if the
    # model beats the ranking baseline by >=3pp; if consecutive runs of this script disagree by
    # an unknown amount, that criterion cannot be evaluated. train_football.py already pins its
    # sampler for the same reason — tennis and NBA did not, which is why the first measured gap
    # (+2.13pp) could not be compared against the previous run's accuracy.
    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(seed=RANDOM_SEED)
    )
    study.optimize(
        lambda trial: _optuna_objective(trial, X_train, y_train, X_val, y_val),
        n_trials=N_OPTUNA_TRIALS,
    )
    print(f"best val log_loss={study.best_value:.4f}, params={study.best_params}")

    X_trainval = pd.concat([X_train, X_val])
    y_trainval = pd.concat([y_train, y_val])
    # random_state pinned as well as the sampler: the search space includes subsample and
    # colsample_bytree, so the FIT is stochastic even once the params are fixed.
    final_model = xgb.XGBClassifier(
        objective="binary:logistic", random_state=RANDOM_SEED, **study.best_params
    )
    final_model.fit(X_trainval, y_trainval)

    val_raw = final_model.predict_proba(X_val)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999)
    calibrator.fit(val_raw, y_val)

    test_raw = final_model.predict_proba(X_test)[:, 1]
    test_calibrated = calibrator.predict(test_raw)
    test_pred_label = (test_calibrated > 0.5).astype(int)

    accuracy = accuracy_score(y_test, test_pred_label)
    brier = brier_score_loss(y_test, test_calibrated)
    # 2-outcome market (no draw) -> RPS reduces exactly to Brier score, same documented
    # equivalence as train_nba.py.
    rps = brier

    baseline_accuracy = accuracy_score(y_test, [1] * len(y_test))  # "always pick player1"

    print(f"test accuracy={accuracy:.4f} (baseline={baseline_accuracy:.4f}) brier/rps={brier:.4f}")

    ranking_metrics = _ranking_baseline_report(test_df, test_pred_label, accuracy)

    artefact_path = ARTIFACT_DIR / f"tennis_xgb_{datetime.now(UTC):%Y%m%d%H%M%S}.joblib"
    joblib.dump(
        {"model": final_model, "calibrator": calibrator, "feature_names": feature_cols},
        artefact_path,
    )
    print(f"saved artefact to {artefact_path}")

    mlflow.set_tracking_uri(f"file:{ML_DIR / 'mlruns'}")
    mlflow.set_experiment("tennis_win_probability")
    with mlflow.start_run():
        mlflow.log_params(study.best_params)
        mlflow.log_param("n_optuna_trials", N_OPTUNA_TRIALS)
        mlflow.log_param("train_seasons", ",".join(str(s) for s in TRAIN_SEASONS))
        mlflow.log_param("val_season", VAL_SEASON)
        mlflow.log_param("test_season", TEST_SEASON)
        mlflow.log_metric("test_accuracy", accuracy)
        mlflow.log_metric("test_brier_rps", brier)
        mlflow.log_metric("baseline_accuracy", baseline_accuracy)
        # Logged so the "does this model beat a strategy a user could run?" question accrues a
        # history rather than being asked once and forgotten — the same reason the corners
        # market's calibration is logged every run.
        for name, value in ranking_metrics.items():
            mlflow.log_metric(name, value)
        mlflow.log_metric("val_log_loss", study.best_value)
        mlflow.log_artifact(str(artefact_path))

    await _register_model(artefact_path, rps, accuracy)


def main() -> None:
    global ACTIVATE_ON_REGISTER
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Register the artefact but leave is_active False. Use for any run that is a "
        "MEASUREMENT rather than a promotion - an experiment arm must not become the served "
        "model just by finishing.",
    )
    ACTIVATE_ON_REGISTER = not parser.parse_args().no_activate

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
