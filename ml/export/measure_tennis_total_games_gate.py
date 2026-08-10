"""THE GATE from TENNIS_TOTAL_GAMES_PLAN step 3, run for real. Verdict: FAILED -- do not build.

Reproduces the decision not to ship a tennis Over/Under total-games market. Run it to check
the work rather than take the summary on trust.

    backend/.venv/Scripts/python ml/export/measure_tennis_total_games_gate.py

Needs ml/data/tennis_game_counts_atp.parquet, tennis_match_stats_atp.parquet and
tennis_rank_points_atp.parquet (all gitignored -- request them alongside this file).

Headline: serve features are 2.5x stronger than the model's existing inputs and STILL carry
no tradeable edge. At the real market lines the model performs at or below "always over",
because its predictions are compressed toward the mean (sd 2.12 against reality's 7.87).

Fair test, not a strawman: three learners, every free feature plus surface and rank, and
leakage controlled by shift(1) before every rolling window.

The first pass used Ridge on rolling serve means and got held-out R^2 = +0.006. Failing a
market on that alone would be unfair -- a linear model on averages is a weak learner, and two
known-real signals were missing entirely (surface, which spans 4.5 games between grass and
clay, and rank, the model's existing strength feature).

So this adds surface and rank, swaps in gradient boosting, and reports the SAME held-out
metrics. If the market is real, this is where it shows up. If it still explains ~nothing, the
verdict is about the sport rather than about the model.

Reported as MAE against predicting the mean, because that is what "is this worth pricing"
actually reduces to -- R^2 near zero is easy to argue about, a MAE that does not move is not.
"""

from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\User\IdeaProjects\SportIQ")
DATA = REPO / "ml" / "data"

counts = pd.read_parquet(DATA / "tennis_game_counts_atp.parquet")
stats = pd.read_parquet(DATA / "tennis_match_stats_atp.parquet")
stats = stats[stats.SET_NUMBER == 0].copy()
counts["GAME_DATE"] = pd.to_datetime(counts["GAME_DATE"])
for f in (counts, stats):
    f["MATCH_ID"] = f["MATCH_ID"].astype(str)

SERVE = [
    "ACES",
    "DOUBLE_FAULTS",
    "FIRST_SERVE_PCT",
    "FIRST_SERVE_POINTS_WON_PCT",
    "SECOND_SERVE_POINTS_WON_PCT",
    "BREAK_POINTS_SAVED_PCT",
    "BREAK_POINTS_CONVERTED_PCT",
    "SERVE_RATING",
    "RETURN_RATING",
    "TOTAL_POINTS_WON_PCT",
]

dated = stats.merge(
    counts[["MATCH_ID", "GAME_DATE", "total_games", "SURFACE"]], on="MATCH_ID", how="inner"
).sort_values(["PLAYER_ID", "GAME_DATE"])

ROLL = 10
for col in SERVE + ["total_games"]:
    dated[f"R_{col}"] = dated.groupby("PLAYER_ID")[col].transform(
        lambda s: s.shift(1).rolling(ROLL, min_periods=3).mean()
    )
# Surface-specific match length for that player -- the plan's own step-1 feature, and the one
# most likely to carry real style information beyond raw serve power.
dated["SURF"] = dated.SURFACE.astype(str).str.strip().str.casefold()
dated["R_SURF_GAMES"] = dated.groupby(["PLAYER_ID", "SURF"])["total_games"].transform(
    lambda s: s.shift(1).rolling(ROLL, min_periods=2).mean()
)

feat = [f"R_{c}" for c in SERVE] + ["R_total_games", "R_SURF_GAMES"]
dated["slot"] = dated.groupby("MATCH_ID").cumcount()
wide = dated[dated.slot < 2].pivot(index="MATCH_ID", columns="slot", values=feat)
wide.columns = [f"{c}_{s}" for c, s in wide.columns]
# Rank points live in their own weekly parquet; take each player's median as a stable
# level (the notebook did the same) rather than a point-in-time lookup, which would need a
# week join and is not what this measurement turns on.
rk = pd.read_parquet(DATA / "tennis_rank_points_atp.parquet")
rk = rk.groupby(rk.PLAYER_ID.astype(str)).RANK_POINTS.median()
base = counts.set_index("MATCH_ID")[["total_games", "GAME_DATE", "SURFACE", "HOME_ID", "AWAY_ID"]]
base["HOME_RANK_POINTS"] = base.HOME_ID.astype(str).map(rk)
base["AWAY_RANK_POINTS"] = base.AWAY_ID.astype(str).map(rk)
wide = wide.join(base.drop(columns=["HOME_ID", "AWAY_ID"]), how="inner")

# Surface as explicit one-hots, plus rank level and gap.
wide["SURF"] = wide.SURFACE.astype(str).str.strip().str.casefold()
for s in ("hard", "clay", "grass"):
    wide[f"IS_{s}"] = (wide.SURF == s).astype(int)
wide["RANK_GAP"] = (wide.HOME_RANK_POINTS - wide.AWAY_RANK_POINTS).abs()
wide["RANK_AVG"] = (wide.HOME_RANK_POINTS + wide.AWAY_RANK_POINTS) / 2

X_COLS = [c for c in wide.columns if c.startswith("R_")] + [
    "IS_hard",
    "IS_clay",
    "IS_grass",
    "RANK_GAP",
    "RANK_AVG",
]
wide = wide.dropna(subset=X_COLS + ["total_games"]).sort_values("GAME_DATE")
print(f"  {len(wide):,} matches with a complete feature vector")

X, y = wide[X_COLS], wide.total_games
cut = int(len(wide) * 0.8)
Xtr, Xte, ytr, yte = X.iloc[:cut], X.iloc[cut:], y.iloc[:cut], y.iloc[cut:]
print(f"  train {len(Xtr):,} / test {len(Xte):,}  ({len(X_COLS)} features)\n")

import xgboost as xgb  # noqa: E402
from sklearn.metrics import r2_score  # noqa: E402

baseline_mae = np.abs(yte - ytr.mean()).mean()
print(f"  {'model':<34}{'R^2':>9}{'MAE':>9}{'vs baseline':>14}")
print(f"  {'predict the mean (baseline)':<34}{0.0:>9.4f}{baseline_mae:>9.3f}{'--':>14}")

for label, model in [
    ("Ridge (linear)", __import__("sklearn.linear_model", fromlist=["Ridge"]).Ridge(alpha=1.0)),
    (
        "XGBoost (depth 4, 400 trees)",
        xgb.XGBRegressor(
            max_depth=4,
            n_estimators=400,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=2.0,
            objective="reg:squarederror",
            n_jobs=4,
            random_state=0,
        ),
    ),
    (
        "XGBoost Poisson (count model)",
        xgb.XGBRegressor(
            max_depth=4,
            n_estimators=400,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=2.0,
            objective="count:poisson",
            n_jobs=4,
            random_state=0,
        ),
    ),
]:
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    mae = np.abs(yte - pred).mean()
    print(f"  {label:<34}{r2_score(yte, pred):>9.4f}{mae:>9.3f}" f"{baseline_mae - mae:>+13.3f}g")

# The market question is not "predict the count" but "beat the line", so test that directly.
best = xgb.XGBRegressor(
    max_depth=4,
    n_estimators=400,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_lambda=2.0,
    objective="count:poisson",
    n_jobs=4,
    random_state=0,
).fit(Xtr, ytr)
pred = best.predict(Xte)
print("\n  === as an Over/Under classifier at the real market lines ===")
for line in (21.5, 22.5, 23.5):
    actual_over = (yte > line).astype(int)
    called_over = (pred > line).astype(int)
    base = max(actual_over.mean(), 1 - actual_over.mean())
    acc = (actual_over == called_over).mean()
    always = "over" if actual_over.mean() > 0.5 else "under"
    print(
        f"  line {line}: accuracy {acc:.4f}  vs always-{always} {base:.4f}"
        f"   edge {acc - base:+.4f}"
        f"   (n={len(yte):,}, called over {called_over.mean():.1%})"
    )

print("\n  variance context:")
print(f"    sd(total_games) = {y.std():.2f} games; a 1-game MAE gain would be a real result.")
print(f"    correlation(pred, actual) on test = {np.corrcoef(pred, yte)[0,1]:+.4f}")
