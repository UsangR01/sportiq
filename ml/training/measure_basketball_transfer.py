"""Does the NBA model predict EUROPEAN basketball? A held-out transfer measurement.

THE QUESTION. Six European leagues are reachable on the API-Sports account the football key
already uses (see collect_basketball_data.py), but serving them needs a paid plan. Before paying
for a subscription and building an adapter, this measures whether the thing we would serve them
with -- the active NBA artefact, unchanged -- actually predicts them. One model serves a whole
sport here, so European leagues would be served by NBA weights exactly as the WNBA already is.

THAT WNBA BET HAS NEVER BEEN MEASURED. CLAUDE.md records it plainly: "The weights are NBA's.
WNBA pace, scoring level and home advantage differ, so calibration may well be off. Nothing here
has been measured against WNBA outcomes." This script is the instrument that was missing, and
Europe is a harsher test than the WNBA on the two axes that matter.

WHY TRANSFER IS IN DOUBT, measured before this was written:

    NBA        227.1 total points   home win 0.552
    Europe     164.8 (-27.4%)       home win 0.603 (+5.2pp, range 0.571-0.649)

Two of the sixteen features (last10_point_diff_*, net_rating_diff) are ABSOLUTE point
differentials learned on the 227-point game, and home_court_indicator is a constant whose
learned meaning is NBA home advantage. Football fixed exactly this class with
league_baselines.py -- league average goals and home-win rate as continuous features, partial
pooling, so the model is told WHAT differs rather than merely WHICH competition it is looking
at. Basketball has no equivalent at all.

=============================================================================================
PRE-REGISTERED CRITERIA -- fixed before a single number was produced, and not negotiable after
=============================================================================================

P1  PRIMARY, Brier.    The model's Brier score must be STRICTLY BETTER than the always-home
                       baseline's. Brier and not accuracy, because always-home already scores
                       ~0.60 accuracy in these leagues: accuracy alone cannot separate skill
                       from the home-advantage prior, while Brier scores the probability the
                       product actually displays.
P2  Accuracy.          Model accuracy must be at least (always-home accuracy - 1.0pp), i.e.
                       not materially worse on hard calls even if it wins on probabilities.
P3  Calibration.       Top-label ECE must be <= 0.10. A number shown to a user as a percentage
                       cannot be off by more than ten points on average.

REPORTED BUT EXPLICITLY NOT GATING: per-league cuts (n is 539-989 per league, several of them
underpowered on their own), log loss, the reliability table, and the "back whichever team has
the better last-10 win rate" heuristic. ROI is not computed at all -- no odds were collected.

DECISION RULE, also fixed in advance:
  * all three pass  -> NBA weights transfer adequately. Serving becomes purely a plan-cost
                       decision, with per-league baselines worth adding as an improvement
                       rather than as a prerequisite.
  * P1 or P3 fails  -> do NOT serve European basketball on NBA weights. Either add the
                       league-baseline features football already proved and retrain, or train a
                       dedicated European model on the history collect_basketball_data.py has
                       now banked.
  * only P2 fails   -> marginal. Report and decide with the user rather than deciding here.

HELD-OUT DESIGN. The test set is each league's LAST collected season, so every team has at
least one prior season of real history behind its form, head-to-head and net-rating features.
Feature assembly runs through the REAL app/models_ml/nba_features.py:assemble_from_game_log --
including its own GAME_DATE < as_of_date leakage guard -- rather than a reimplementation, since
a transfer test against a reimplementation measures the reimplementation.

The always-home baseline probability is taken from PRIOR seasons only, never from the test
season, because reading the test season's own home-win rate into the baseline would hand it
information the model is not given.

HISTORY IS POOLED ACROSS COMPETITIONS on purpose: a club's last-10 form genuinely includes its
Euroleague games, and the provider uses one global team-id namespace, so pooling is both
realistic and free. Per-league attribution is by the TEST GAME's own competition.
"""

import sys
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models_ml.nba import NBAModel
from app.models_ml.nba_features import assemble_from_game_log

DATA = Path(__file__).resolve().parents[1] / "data"
ARTEFACT = (
    Path(__file__).resolve().parents[1]
    / "artifacts"
    / "deployed"
    / "nba_xgb_20260728142552.joblib"
)
# The registry version whose artefact this is. The two timestamps differ by a few seconds
# because _register_model used to mint its own datetime -- documented in CLAUDE.md, corrected
# since, and named here so the measurement is attributable to a specific served model.
MODEL_VERSION = "nba_xgb_v20260728142554"

ECE_BUCKETS = 10
P3_MAX_ECE = 0.10
P2_ACCURACY_TOLERANCE = 0.010


def _brier(prob: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean((prob - actual) ** 2))


def _log_loss(prob: np.ndarray, actual: np.ndarray) -> float:
    p = np.clip(prob, 1e-12, 1 - 1e-12)
    return float(-np.mean(actual * np.log(p) + (1 - actual) * np.log(1 - p)))


def _top_label_ece(prob: np.ndarray, actual: np.ndarray) -> tuple[float, list[tuple]]:
    """Expected calibration error on the PREDICTED LABEL's confidence.

    Uses explicit bucket edges rather than np.arange, because np.arange(0, 1, 0.1) yields
    0.30000000000000004 and a probability of exactly 0.3 then misses its own bucket -- a real
    bug a test caught in evaluation.py.
    """
    confidence = np.where(prob >= 0.5, prob, 1.0 - prob)
    correct = np.where(prob >= 0.5, actual, 1.0 - actual)
    edges = [i / ECE_BUCKETS for i in range(ECE_BUCKETS + 1)]
    total, ece, table = len(prob), 0.0, []
    for lo, hi in pairwise(edges):
        mask = (
            (confidence >= lo) & (confidence < hi) if hi < 1.0 else (confidence >= lo)
        )
        n = int(mask.sum())
        if n == 0:
            continue
        mean_conf, mean_acc = float(confidence[mask].mean()), float(
            correct[mask].mean()
        )
        ece += (n / total) * abs(mean_conf - mean_acc)
        table.append((lo, hi, n, mean_conf, mean_acc))
    return ece, table


def main() -> None:
    pooled_path = DATA / "basketball_game_log_pooled.parquet"
    if not pooled_path.exists():
        # Rebuilt from the per-league frames rather than required on disk: those are committed
        # and this is a plain concat of them, so the measurement reproduces from a clean clone
        # without anyone holding an API key.
        parts = sorted(DATA.glob("basketball_game_log_*.parquet"))
        parts = [p for p in parts if p.name != pooled_path.name]
        if not parts:
            raise SystemExit(
                f"no basketball game logs in {DATA} - run collect_basketball_data.py"
            )
        pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True).to_parquet(
            pooled_path, index=False
        )
        print(f"rebuilt {pooled_path.name} from {len(parts)} per-league frames")
    if not ARTEFACT.exists():
        raise SystemExit(f"{ARTEFACT} missing")

    games = pd.read_parquet(pooled_path)
    games["GAME_DATE"] = pd.to_datetime(games["GAME_DATE"]).dt.date
    model = NBAModel(str(ARTEFACT), MODEL_VERSION)

    # Each league's newest collected season is its held-out set. Compared by start year so the
    # string "2024-2025" and the Euroleague's "2024" both order correctly.
    test_season = {
        league: max(frame["SEASON"].unique(), key=lambda s: str(s).split("-")[0])
        for league, frame in games.groupby("LEAGUE")
    }
    print("held-out season per league:")
    for league, season in sorted(test_season.items()):
        print(f"  {league:<12} {season}")

    home_rows = games[games["IS_HOME"]]
    records = []
    for row in home_rows.itertuples():
        if row.SEASON != test_season[row.LEAGUE]:
            continue
        features = assemble_from_game_log(
            games_df=games,
            as_of_date=row.GAME_DATE,
            season=row.SEASON,
            home_team=row.TEAM_ABBREVIATION,
            away_team=row.MATCHUP.rsplit(" ", 1)[-1],
        )
        result = model.predict(features)
        populated = sum(1 for v in features.values() if v is not None)
        records.append(
            {
                "league": row.LEAGUE,
                "home_prob": result.home_prob,
                "home_won": 1.0 if row.WL == "W" else 0.0,
                "completeness": populated / len(features),
                "has_form": features["last10_win_rate_home"] is not None
                and features["last10_win_rate_away"] is not None,
            }
        )

    df = pd.DataFrame(records)
    if df.empty:
        raise SystemExit("no test games assembled")

    # Baseline from PRIOR seasons only - never the test season being scored.
    prior = home_rows[
        home_rows.apply(lambda r: r["SEASON"] != test_season[r["LEAGUE"]], axis=1)
    ]
    prior_home_rate = float((prior["WL"] == "W").mean())
    print(
        f"\nalways-home baseline probability (prior seasons only): {prior_home_rate:.4f}"
    )
    print(
        f"test games: {len(df)}   with both sides' form populated: {int(df['has_form'].sum())}"
    )
    print(
        f"feature completeness: {df['completeness'].mean():.4f} (4 key-player + moneyline absent)"
    )

    prob, actual = df["home_prob"].to_numpy(), df["home_won"].to_numpy()
    base = np.full_like(prob, prior_home_rate)

    model_acc = float(((prob >= 0.5) == (actual == 1.0)).mean())
    base_acc = float((actual == 1.0).mean())  # always-home always predicts home
    model_brier, base_brier = _brier(prob, actual), _brier(base, actual)
    ece, table = _top_label_ece(prob, actual)

    form = df[df["has_form"]]
    print("\n" + "=" * 78)
    print(f"{'metric':<26}{'NBA model':>14}{'always-home':>14}{'verdict':>20}")
    print("-" * 78)
    print(
        f"{'accuracy':<26}{model_acc:>14.4f}{base_acc:>14.4f}"
        f"{('+' if model_acc >= base_acc else '') + f'{100*(model_acc-base_acc):.2f}pp':>20}"
    )
    print(
        f"{'Brier (PRIMARY)':<26}{model_brier:>14.4f}{base_brier:>14.4f}"
        f"{('better' if model_brier < base_brier else 'WORSE'):>20}"
    )
    print(
        f"{'log loss':<26}{_log_loss(prob, actual):>14.4f}{_log_loss(base, actual):>14.4f}{'':>20}"
    )
    print(f"{'top-label ECE':<26}{ece:>14.4f}{'-':>14}{'':>20}")
    print("=" * 78)

    print(
        "\nreliability (predicted-label confidence vs observed), buckets under 20 suppressed:"
    )
    for lo, hi, n, conf, acc in table:
        if n < 20:
            continue
        print(
            f"  {lo:.1f}-{hi:.1f}  n={n:>5}  claimed {conf:.3f}  delivered {acc:.3f}"
            f"  gap {acc - conf:+.3f}"
        )

    print("\nper league (REPORTED, NOT GATING - several are underpowered alone):")
    print(
        f"  {'league':<12}{'n':>5}{'acc':>8}{'home rate':>11}{'Brier':>8}{'base Brier':>12}"
    )
    for league, frame in df.groupby("league"):
        p, a = frame["home_prob"].to_numpy(), frame["home_won"].to_numpy()
        lb = np.full_like(p, prior_home_rate)
        print(
            f"  {league:<12}{len(frame):>5}{float(((p>=0.5)==(a==1.0)).mean()):>8.3f}"
            f"{float(a.mean()):>11.3f}{_brier(p,a):>8.4f}{_brier(lb,a):>12.4f}"
        )

    if len(form) != len(df):
        fp, fa = form["home_prob"].to_numpy(), form["home_won"].to_numpy()
        fb = np.full_like(fp, prior_home_rate)
        print(
            f"\nform-populated subset only: n={len(form)} "
            f"acc {float(((fp>=0.5)==(fa==1.0)).mean()):.4f} "
            f"Brier {_brier(fp,fa):.4f} vs base {_brier(fb,fa):.4f}"
        )

    p1 = model_brier < base_brier
    p2 = model_acc >= base_acc - P2_ACCURACY_TOLERANCE
    p3 = ece <= P3_MAX_ECE
    print("\nPRE-REGISTERED VERDICT")
    print(
        f"  P1 Brier beats always-home      {'PASS' if p1 else 'FAIL'}"
        f"   ({model_brier:.4f} vs {base_brier:.4f})"
    )
    print(
        f"  P2 accuracy within 1.0pp        {'PASS' if p2 else 'FAIL'}"
        f"   ({model_acc:.4f} vs {base_acc:.4f})"
    )
    print(
        f"  P3 top-label ECE <= {P3_MAX_ECE:.2f}        {'PASS' if p3 else 'FAIL'}   ({ece:.4f})"
    )
    if p1 and p2 and p3:
        print("\n  => ALL PASS: NBA weights transfer. Serving is a plan-cost decision.")
    elif not p1 or not p3:
        print(
            "\n  => DO NOT SERVE on NBA weights. Add league baselines and retrain, or train a"
            "\n     dedicated European model on the collected history."
        )
    else:
        print("\n  => MARGINAL (only P2 failed). Report and decide with the user.")


if __name__ == "__main__":
    main()
