"""Held-out evaluation instruments, shared by training and testable in CI.

WHY THIS MODULE EXISTS RATHER THAN LIVING IN train_football.py: that script imports xgboost,
mlflow and optuna at module scope, so anything defined there is unreachable from the backend
test suite and therefore unpinned. Every metric this project has been embarrassed by was one
that existed only as a printed number. Same reasoning as app/predictions/market_signal.py,
which train_football.py already imports rather than reimplementing.

Nothing here trains or serves anything -- these are pure functions over probabilities and
outcomes, and they deliberately RETURN their output instead of printing it, so a caller owns
presentation and a test can assert on numbers without capturing stdout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss

# Below this a bucket rate is noise, not a signal. Same bar the Over/Under reliability tables
# in train_football.py already use, kept identical so two tables in one run's output can be
# read against each other.
MIN_BUCKET_ROWS_TO_REPORT = 20

ECE_BINS = 10


def multiclass_calibration(
    y_true: np.ndarray,
    probs: np.ndarray,
    raw_probs: np.ndarray,
    classes: tuple[str, ...],
) -> tuple[dict[str, float], list[str]]:
    """Log loss, multiclass Brier, top-label ECE and per-class reliability.

    WHY THIS EXISTS: Over/Under goals and then corners each got a reliability table only AFTER
    shipping a market nobody had checked -- goals visibly overconfident, corners judged by MAE
    alone while the product sold "P(under 9.5 corners)". 1X2 is the flagship market and never
    got one at all. RPS is reported and is a proper scoring rule, but it is a single number: it
    cannot say whether the fixtures the model calls 55% home actually win 55% of the time. That
    question had never been asked of the market the whole product is built on.

    Three instruments, because one can look fine while another does not:
      - log loss and multiclass Brier -- overall probabilistic quality, comparable across runs.
        Reported calibrated AND uncalibrated, because isotonic calibration is a step that is
        supposed to earn its place and nothing had ever measured whether it does.
      - ECE -- how far top-label confidence sits from accuracy at that confidence. This is the
        number a user effectively reads off a card showing "HOME 61%".
      - per-class reliability buckets -- WHERE the miscalibration lives. A model can post a
        near-zero ECE while being systematically overconfident on draws and underconfident on
        home, because top-label ECE only ever sees the winning class.
    """
    y_true = np.asarray(y_true)
    labels = list(range(len(classes)))
    onehot = np.zeros_like(probs)
    onehot[np.arange(len(y_true)), y_true] = 1.0

    metrics: dict[str, float] = {
        "test_log_loss": float(log_loss(y_true, probs, labels=labels)),
        "test_log_loss_uncalibrated": float(log_loss(y_true, raw_probs, labels=labels)),
        # SUM over classes of squared error, averaged over fixtures -- the standard multiclass
        # form, so it ranges [0, 2]. Explicitly NOT comparable to train_nba.py's two-outcome
        # Brier without halving, which is why the name carries `_multiclass`.
        "test_brier_multiclass": float(np.mean(np.sum((probs - onehot) ** 2, axis=1))),
        "test_brier_multiclass_uncalibrated": float(
            np.mean(np.sum((raw_probs - onehot) ** 2, axis=1))
        ),
        "test_ece": expected_calibration_error(y_true, probs),
    }

    lines = [
        "1X2 — held-out probabilistic quality (calibrated vs uncalibrated):",
        f"  log loss           {metrics['test_log_loss']:.4f}"
        f"   (uncalibrated {metrics['test_log_loss_uncalibrated']:.4f})",
        f"  Brier (multiclass) {metrics['test_brier_multiclass']:.4f}"
        f"   (uncalibrated {metrics['test_brier_multiclass_uncalibrated']:.4f})",
        f"  ECE (top label)    {metrics['test_ece']:.4f}",
        "1X2 — per-class reliability (predicted bucket vs actually observed):",
    ]
    for i, cls in enumerate(classes):
        actual = onehot[:, i]
        # The whole [0, 1] range is swept per class rather than a shared narrow band: a draw
        # probability lives around 0.25 while a home probability can reach 0.8, so any fixed
        # window would blank one of them out entirely.
        bucket_lines = []
        # Edges from integer division, NOT np.arange(0.0, 1.0, 0.1) -- that accumulates float
        # error and yields 0.30000000000000004, so a probability of exactly 0.3 misses its own
        # bucket and lands in the one below. Caught by a test, not by reading it.
        for step in range(10):
            lo = step / 10
            in_bucket = (probs[:, i] >= lo) & (probs[:, i] < lo + 0.1)
            n = int(in_bucket.sum())
            if n >= MIN_BUCKET_ROWS_TO_REPORT:
                observed = float(actual[in_bucket].mean())
                bucket_lines.append(
                    f"      predicted {lo:.1f}-{lo + 0.1:.1f}: n={n:5d} actual={observed:.3f}"
                )
                metrics[f"reliability_{cls}_{lo:.1f}"] = observed
        lines.append(f"  {cls}:")
        lines.extend(bucket_lines or ["      (no bucket reaches the reporting minimum)"])
    return metrics, lines


def expected_calibration_error(
    y_true: np.ndarray, probs: np.ndarray, bins: int = ECE_BINS
) -> float:
    """Top-label ECE: sum over bins of (n_bin/N) * |accuracy(bin) - mean confidence(bin)|.

    Top-label because that is the claim the product actually makes -- a card shows one
    selection and one percentage. Per-class miscalibration that never surfaces as the argmax
    is invisible here by construction, which is why multiclass_calibration reports the
    per-class reliability buckets alongside rather than instead.
    """
    y_true = np.asarray(y_true)
    confidence = probs.max(axis=1)
    correct = (probs.argmax(axis=1) == y_true).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        # The final bin closes on the right, so a probability of exactly 1.0 is weighted
        # rather than silently dropped out of the expectation.
        in_bin = (confidence >= lo) & (confidence <= hi if i == bins - 1 else confidence < hi)
        n = int(in_bin.sum())
        if n:
            ece += (n / len(y_true)) * abs(
                float(correct[in_bin].mean() - confidence[in_bin].mean())
            )
    return float(ece)


# The four Layer 1 xG features, named explicitly so saved rows can be cut by whether real xG
# was available. Season 2021 carries NO xG in any of the 18 leagues and 2022 only 57%, so the
# training split is ~49% covered against a test split at 98% -- a train/test mismatch in
# feature AVAILABILITY, not merely a coverage gap. Saving the flag makes that cut a groupby
# instead of a re-run.
XG_FEATURE_COLUMNS = ("xg_for_home", "xg_against_home", "xg_for_away", "xg_against_away")


def build_test_prediction_rows(
    test_df: pd.DataFrame,
    feature_cols: list[str],
    probs: np.ndarray,
    raw_probs: np.ndarray,
    xg_home: np.ndarray,
    xg_away: np.ndarray,
    classes: tuple[str, ...],
    model_version: str,
) -> pd.DataFrame:
    """One row per test fixture: what the model said, and what actually happened.

    WHY THIS EXISTS: every model comparison in this project has rested on printed headline
    numbers plus hashing the boosters to prove what changed. That is enough to prove SCOPE and
    useless for attribution -- it cannot answer "which fixtures moved", and it is what forced
    the standing caveat that corners MAE 2.167 -> 2.157 is not an improvement because the test
    set grew 35% underneath it. With per-fixture rows saved, that comparison becomes a join on
    fixture_id over the shared subset rather than a warning in a comment.

    Deliberately carries the UNCALIBRATED probabilities alongside the served ones, and the
    availability flags alongside the predictions: the questions worth asking later ("is
    calibration earning its place", "does the model do worse where xG is missing") are exactly
    the ones a headline metric throws the columns away for.
    """
    present = test_df[feature_cols].notna()
    corners_home = test_df["home_corners"].astype(float)
    rows = pd.DataFrame(
        {
            "model_version": model_version,
            "fixture_id": test_df["fixture_id"].to_numpy(),
            "game_date": pd.to_datetime(test_df["game_date"]).to_numpy(),
            "season": test_df["season"].to_numpy(),
            "league": test_df["LEAGUE"].to_numpy(),
            "label": test_df["label"].to_numpy(),
            "pred_label": probs.argmax(axis=1),
            "home_goals": test_df["home_goals"].to_numpy(),
            "away_goals": test_df["away_goals"].to_numpy(),
            "home_corners": corners_home.to_numpy(),
            "away_corners": test_df["away_corners"].astype(float).to_numpy(),
            "xg_home_pred": xg_home,
            "xg_away_pred": xg_away,
            # Fraction of the model's own feature vector that was real for this fixture -- the
            # training-side counterpart of predictions.feature_completeness, so a live
            # completeness band can be compared against a held-out one.
            "feature_completeness": present.mean(axis=1).to_numpy(),
            "has_xg_features": present[list(XG_FEATURE_COLUMNS)].all(axis=1).to_numpy(),
            "has_corners": corners_home.notna().to_numpy(),
        }
    )
    for i, cls in enumerate(classes):
        rows[f"p_{cls}"] = probs[:, i]
        rows[f"p_{cls}_uncalibrated"] = raw_probs[:, i]
    rows["correct"] = rows["pred_label"] == rows["label"]
    return rows


def per_league_market_metrics(
    rows: pd.DataFrame, goals_lines: tuple[float, ...], corners_lines: tuple[float, ...]
) -> pd.DataFrame:
    """Brier per league PER MARKET, from the saved per-fixture test rows.

    THE LAST GAP FROM THE MEASUREMENT CRITIQUE. Per-league existed for 1X2 only and per-market
    existed pooled only, so the one cut that can show "this league's corners are fine but its
    goals are noise" had never been produced. A pooled market number can be carried by a few
    leagues exactly as a pooled headline can.

    Computed from the same rows that get written to ml/evaluation/, so it costs one groupby
    rather than a retrain, and any run's table can be regenerated from its own saved file.

    Brier rather than accuracy because these markets sell a PROBABILITY. Leagues under
    MIN_LEAGUE_ROWS_FOR_MARKET are dropped rather than printed: a Brier on 40 fixtures moves
    more with which fixtures landed in the split than with the model.
    """
    from app.models_ml.markets import over_under_probs

    out = []
    for league, group in rows.groupby("league"):
        if len(group) < MIN_LEAGUE_ROWS_FOR_MARKET:
            continue
        goal_totals = (group["home_goals"] + group["away_goals"]).to_numpy()
        xg_totals = (group["xg_home_pred"] + group["xg_away_pred"]).to_numpy()
        row = {"league": str(league), "n": len(group)}
        for line in goals_lines:
            predicted = np.array([over_under_probs(float(t), (line,))[line][0] for t in xg_totals])
            row[f"goals_u{line}_brier"] = float(np.mean((predicted - (goal_totals < line)) ** 2))

        corners = group[group["has_corners"]]
        row["n_corners"] = len(corners)
        if len(corners) >= MIN_LEAGUE_ROWS_FOR_MARKET:
            actual = (corners["home_corners"] + corners["away_corners"]).to_numpy()
            totals = (corners["corners_home_pred"] + corners["corners_away_pred"]).to_numpy()
            for line in corners_lines:
                predicted = np.array([over_under_probs(float(t), (line,))[line][0] for t in totals])
                row[f"corners_u{line}_brier"] = float(np.mean((predicted - (actual < line)) ** 2))
        out.append(row)
    return pd.DataFrame(out)


# A Brier on a handful of fixtures moves more with which fixtures landed in the split than with
# the model. Higher than MIN_BUCKET_ROWS_TO_REPORT because this cut slices twice (league AND
# market) and the corners subset is thinner again.
MIN_LEAGUE_ROWS_FOR_MARKET = 100
