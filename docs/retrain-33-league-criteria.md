# Pre-registered criteria — the 33-league retrain

**Written 2026-09-28, before any number from this retrain existed.** Committed before the run so
git timestamps it: the point of pre-registration is that the author does not get to relitigate
after seeing the result, and that only holds if the rules are provably older than the numbers.

Incumbent, currently serving: **`football_xgb_v20260823131011`** — test accuracy 0.5082, RPS
0.2084, trained 2026-08-23.

## What is changing

Five leagues join the pool, collected 2026-09-28 and not yet served:

| league | id | fixtures | corners coverage |
|---|---|---|---|
| England League Two | 42 | 2,879 | 71% |
| Argentina Liga Profesional | 128 | 2,753 | 70% |
| Colombia Primera A | 239 | 2,506 | 64% |
| Spain LaLiga 2 | 141 | 2,416 | 84% |
| Brazil Série B | 72 | 2,198 | 72% |

12,752 fixtures, six seasons each. The split windows are unchanged: **TRAIN 2021-23 / VAL 2024 /
TEST 2025**, so these seasons do reach training — unlike the 2026-only collection that produced a
retrain whose every booster came out byte-identical.

## What CANNOT be compared, and why it is stated first

**Headline accuracy is not comparable across this change.** Adding leagues moves the TEST set, so
the new number is a score on a different exam paper. This project has made that mistake before and
recorded it: 0.4916 on nine leagues against 0.4857 on eighteen looked like a regression and was
not. The same applies to corners MAE, whose test set grew 35% on a previous run.

Two instruments are comparable, and the criteria below use only those:

1. **The gap over each run's OWN always-home baseline.** Self-normalising, so it survives a
   changed test set.
2. **Per-league metrics for the 28 leagues present in BOTH runs.** Those are like-for-like, and
   they are the strongest evidence available here.

## Procedure

Both arms run from the same code, in this order:

1. **Baseline arm** — current 28-league pool, `--no-activate`. This exists so the comparison is
   against a number produced by today's code rather than against a registry row from five weeks
   ago.
2. **Candidate arm** — 33 leagues, `--no-activate`.
3. Compare. Adopt or reject per the criteria below.
4. Only on adoption, register and activate.

`--no-activate` on both arms is not optional. `_register_model` demotes every active row before
inserting; a measurement arm that activates itself has twice caused real harm here — once
promoting an experiment's losing arm as the served tennis model, once leaving NBA with no active
model at all.

## PRIMARY criteria — these decide adopt or reject

Every one carries an explicit tolerance. The last pre-registration on this project specified none,
so a 0.05% relative move read as failure and had to be overridden after the fact; that override is
still recorded as an override.

**P1 — Pooled gap must not regress.**
The candidate's pooled gap over its own always-home baseline must be **≥ baseline arm's gap −
0.50pp**. A tolerance rather than equality because the test set genuinely changes and a small move
carries no information.

**P2 — No existing league may be badly damaged.**
Across the 28 leagues in both arms, **no league's gap over its own baseline may fall by more than
3.0pp**, and **at most 3 leagues may regress by more than 1.0pp**. This is the like-for-like
instrument and the one that would catch the pooled number hiding a localised failure.

**P3 — The count of leagues below their own baseline must not rise by more than 2.**
The last full retrain had 1 of 18 below baseline. This bounds the obvious failure mode of pooling
more heterogeneous leagues.

**P4 — Over/Under goals discrimination must not degrade.**
The **under-3.5 reliability buckets must remain monotonic** if they currently are. Monotonicity is
the property repeatedly identified as binding on this market, and it is the one that moved when
Layer 1 tuning earned its place. A bucket with n < 20 is not counted, matching the script.

**P5 — Full-scoreline log loss must not worsen by more than 0.005 nats.**
The structural instrument, and the one that cannot be gamed by a single market. Reported against
both model-free references every run.

## SECONDARY — reported, explicitly NOT gating

Recorded so the run is fully described, and so nobody can later promote one of these to a
criterion because it happened to look good:

- Test accuracy and RPS (both arms) — **not comparable**, see above
- Corners MAE — test set changes with the pool
- xG MAE, raw and calibrated
- Per-league × per-market Brier
- Flat-stake ROI — always directional here, never used to choose anything

## The five new leagues specifically

**Reported, not gating on this run.** A league cannot be judged on the first run that includes it;
its own baseline gap is the thing to watch on the NEXT retrain, once it has served fixtures.

Two carry known, named risks worth reading first rather than discovering:

- **Brazil Série B at 2.35 goals/match** is a low-scoring outlier, close to Brasileirão's 2.41 —
  which is precisely where Over/Under calibration went wrong before, when pooling only
  EPL+Brasileirão biased P(under 3.5) toward 0.79 while MLS and CSL truly sat near 0.66.
- **Colombia's xG is roughly a third missing by construction** — TheStatsAPI carries only the
  Apertura half of each year. Those rows score as missing, which XGBoost handles, but a per-league
  metric that looks poor for Colombia should be read against that before anything is concluded.

## If the criteria fail

Reject, keep the incumbent serving, and record which criterion failed and by how much. A failed
retrain is a result — the collected data stays valid and the next attempt starts from a stated
reason rather than a fresh guess.

## What this does NOT authorise

Adopting the model does **not** switch the five leagues on. They are collection-only by an explicit
decision taken 2026-09-28, and serving them is a separate step requiring `LEAGUE_IDS`,
`CALENDAR_YEAR_SEASON_LEAGUES` and `app/sports/catalog.py` entries — all recorded in
`ml/training/collect_football_data.py` so they need not be re-derived.
