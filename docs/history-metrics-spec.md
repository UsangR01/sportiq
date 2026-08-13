# `GET /history` — metrics specification

**Status:** §1-8 written before the endpoint was touched; **§9 pre-registered and SETTLED
2026-08-11**, before the data it judges exists. Neither was shaped by seeing which cut of the
numbers looked best.

**Date:** 2026-08-10

---

## 1. The question, and the one it does not answer

This endpoint exists to answer: **do the predictions actually work?**

It does **not** exist to make the product look credible. Those two goals conflict the moment a
number is disappointing, and the resolution is fixed here in advance: the endpoint reports what
is true, including "not enough data to say", which is the honest answer today.

As of writing there is **no demonstrated edge** — football flat-stake ROI −6.4% (n=38),
CLV −3.58% (n=27), tennis +1.6pp over "always back the higher-ranked player". Nothing about
this endpoint is expected to change that. It is instrumentation for finding out.

---

## 2. Two different things are being measured — do not conflate them

This is the central design constraint, and it comes from what is actually stored.

### 2a. Model skill — measurable today

Are the model's probabilities good? Answered from `predictions` rows, which store
`home_prob` / `draw_prob` / `away_prob`, `xg_*`, `confidence_tier`, `feature_completeness`.

Metrics: **calibration, Brier, RPS, reliability buckets.** These need no odds and no record of
what was displayed, so they work retrospectively on existing data.

### 2b. Product skill — NOT measurable retrospectively

Were the picks we actually showed worth backing? This needs the pick that was **displayed**,
and that is **not recorded anywhere**. `best_pick` is computed per request from the prediction
plus whichever odds existed at that moment, filtered by `MIN_EDGE_OVER_BASE_RATE`,
`MAX_EDGE_OVER_MARKET`, `MIN_FEATURE_COMPLETENESS` and the caller's `min_probability` /
`min_odds`. None of that is persisted.

Recomputing it later would measure **a different product than users saw**: odds move, the
guards have changed twice this month, and the model version may differ.

> **Consequence:** hit rate, ROI and CLV over *shown picks* cannot be reported for any fixture
> settled before pick-snapshotting exists. Reporting them anyway — by regrading with today's
> odds — would be the single most misleading thing this endpoint could do.

**Required follow-up (own work item):** persist the pick at the moment it is shown —
selection, market, line, odds, probability, model version, and which guards it passed.
Everything in §2b starts accumulating from that change, not before.

---

## 3. Population

Default population, all conditions required:

| Filter | Value | Why |
|---|---|---|
| `predictions.kind` | `pre_match` **only** | Retrodictions are produced after the result is known. Legitimate for the feed, never evidence of skill. `unknown` is excluded too — see below. |
| `fixtures.status` | `completed` | Nothing else has a result. |
| `outcomes` | row must exist | The graded truth. |
| `fixture_live_state.result_type` | `NULL` | Excludes voids: a tennis retirement has no bettable result and most books void the bet. |

`unknown` rows are excluded from every metric and **reported as a count**, not hidden. They are
the rows whose provenance was destroyed by regeneration (90 football, 453 tennis). Silently
dropping them would misstate coverage; silently including them would inflate the result.

Retrodictions are available behind an explicit `kind=retrodiction` filter, **never in the
headline**, and every response states which population it used.

---

## 4. Metrics

Each is defined exactly, because "accuracy" alone is ambiguous across a 3-way market.

### Model skill (§2a)

- **Brier score** — mean squared error of the probability assigned to the outcome that
  occurred. Lower is better. Reported per market.
- **RPS (Ranked Probability Score)** — the ordered 3-way analogue for 1X2. Preferred over
  accuracy for football because it rewards being *close*, and a home/draw miss is not the same
  error as a home/away miss.
- **Calibration** — mean predicted vs actual frequency, plus reliability buckets in 0.1 bands.
  Buckets under `MIN_BUCKET_N` are suppressed rather than printed as if meaningful.
- **Discrimination (trend z)** — Cochran-Armitage across reliability buckets. Distinct from
  calibration and the metric that has repeatedly mattered: a model can be perfectly calibrated
  and still useless if every fixture gets the base rate.

### Product skill (§2b — only for fixtures with a stored pick snapshot)

- **Hit rate vs baseline** — always with the baseline stated (e.g. "always back home"). A raw
  hit rate is meaningless without it.
- **Flat-stake ROI** — 1 unit per pick at the odds *recorded when shown*, never at
  today's odds. `PLAUSIBLE_MAX_DECIMAL_ODDS` applies.
- **CLV** — `taken / closing − 1`, closing being the last price strictly before kickoff. The
  sharpest available signal of edge, and currently the most damning.

---

## 5. Slices

`sport`, `league`, `market`, `line`, `confidence_tier`, `feature_completeness` band,
`model_version`.

**Multiple-comparisons discipline.** Slicing by league × market × tier produces dozens of
cells; some will look significant by chance alone. Therefore:

- The **primary** metric and population are declared in advance (P2 pre-registration).
- Every other slice is **exploratory** and labelled as such in the response.
- A finding in an exploratory slice is a hypothesis to test on future data, never a conclusion.

This is the same discipline that made the Tier-1 league and key-player decisions trustworthy.

---

## 6. Statistical honesty — required, not optional

Every metric in every response carries:

1. **`n`** — the sample it was computed from.
2. **A confidence interval** — Wilson for proportions.
3. **`detectable_effect`** — the minimum effect this `n` could detect at 80% power.

And metrics computed on fewer than `MIN_REPORTABLE_N` are returned as `null` with
`"reason": "insufficient sample"` rather than as a number.

The reason is concrete and current:

```
football   n=139   detects 10.5pp   believed edge 4.1pp   need ~891    6x short
tennis     n= 79   detects 13.6pp   believed edge 1.6pp   need ~5,406  68x short
```

**Both sports are underpowered for the edge we believe exists.** An early accuracy figure is
noise, in either direction. Without `n` and `detectable_effect` displayed beside it, a 55%
on n=40 will be read as a track record — which is the exact failure this endpoint exists to
prevent.

---

## 7. Response shape (sketch)

```jsonc
{
  "population": {
    "kind": "pre_match",
    "excluded": { "unknown_provenance": 90, "void_results": 12, "no_outcome": 4 }
  },
  "primary": {
    "metric": "rps", "value": 0.2151, "n": 139,
    "ci95": [0.198, 0.232], "detectable_effect": 0.105,
    "sufficient": false, "reason": "insufficient sample"
  },
  "model_skill": { "brier": {...}, "calibration": {...}, "discrimination": {...} },
  "product_skill": {
    "available": false,
    "reason": "shown picks were not recorded before 2026-08-10; see spec §2b"
  },
  "exploratory": { "by_league": [...], "by_market": [...] }
}
```

---

## 8. What would make this endpoint misleading

Stated explicitly so it can be checked against:

- Regrading with **today's** odds or **today's** guards (§2b).
- Including retrodictions in the headline (§3).
- Reporting a metric without `n` (§6).
- Quoting an exploratory slice as a conclusion (§5).
- Reporting hit rate without its baseline (§4).
- Treating "not enough data" as a failure of the endpoint rather than its correct output.

---

## 9. Pre-registration — SETTLED 2026-08-11

Fixed here **before** the numbers they will judge exist. Every threshold is anchored to an
**external** reference — bookmaker margin, standard statistical power, interval width — and
**none** is derived from a result this project has measured. That distinction is the whole
value of the section: I had already seen football at 56.1%, tennis at 63.6%, and CLV at −3.47%
when writing it, so any bar reverse-engineered from those would be worthless.

### 9.1 Primary metric: **CLV on snapshotted picks**

Not accuracy, and the reason is sample size rather than preference:

```
detect +3pp of 1X2 accuracy over baseline   n ≈ 1,650
detect +1.0% mean CLV (per-pick SD 6-8%)    n ≈ 223-396
```

CLV needs roughly **a fifth** of the sample to detect an effect worth having, because it scores
every pick against a sharp reference price instead of waiting for a binary result. It is also a
leading indicator: it needs prices, not outcomes.

**RPS is the primary MODEL-skill metric** in the meantime, since it is computable today from
stored probabilities and does not depend on snapshots.

Accuracy is reported but is **never** primary. It discards the probability and the price, which
are the two things a betting product actually sells.

### 9.2 What counts as an edge

| Metric | Edge | No edge | Anchor |
|---|---|---|---|
| **CLV** (primary) | mean > 0, 95% CI excluding 0 | CI wholly ≤ 0 | Beating the closing line is the standard professional test of whether a bettor has information the market lacks. |
| **Flat-stake ROI** | CI excluding 0 | CI wholly ≤ 0 | Break-even is 0 by construction; the bookmaker margin is already inside the prices. |
| **1X2 accuracy** | ≥ +3pp over that population's own baseline | < +3pp | A modest but genuine improvement; below this the pick ordering is not worth acting on. |

**A CI spanning zero is "not yet known", not "no edge".** Reporting an inconclusive result as a
negative is the same error as reporting it as a positive.

### 9.3 If there is no edge

Pre-committed, so the response is not negotiated after seeing the answer:

1. **Stop presenting picks as advice.** The feed becomes predictions-with-context, not
   recommendations, and the copy says so.
2. **Do not retrain to chase it.** Three retrains in two days moved the headline by noise; a
   fourth after a bad read would be fitting to the test set.
3. **Do not add leagues or markets.** Both widen coverage of something unproven.
4. Retain the measurement pipeline. A negative result that is *known* is worth more than the
   positive one that was assumed.

### 9.4 Reporting thresholds

**`MIN_REPORTABLE_N = 93`**, raised from 30. Anchored to interval width, not to any result: 93
is the smallest n whose 95% Wilson interval is narrower than 20 percentage points at p=0.5.
Below it, a percentage says almost nothing and should render as "not enough data".

```
interval width ≤ 20%  →  n ≥  93     ← chosen
interval width ≤ 15%  →  n ≥ 167
interval width ≤ 10%  →  n ≥ 381
```

At this bar, football (n=139) is reportable today and tennis (n=77) is not. That the threshold
excludes real current data is the point — it was set by the rule, not to fit what exists.

**`MIN_BUCKET_N = 30`** for reliability buckets. Lower deliberately: a bucket is one point in a
trend, read together with its neighbours, not a standalone claim.

### 9.5 `sufficient_sample` — split in two

The single flag was ambiguous, and ambiguous in the dangerous direction: it read as "this number
is trustworthy" when it only meant "big enough to print".

- **`sufficient_sample`** — `n >= MIN_REPORTABLE_N`. Safe to display.
- **`conclusive`** — `detectable_effect <= the §9.2 threshold for this metric`. Safe to *act*
  on.

A metric may be reportable and inconclusive at once. That is the normal state today and the UI
must be able to say so.

### 9.6 `MIN_FEATURE_COMPLETENESS` stays at 0.25

Correctness evidence points to 0.35 (below 0.25: 33.3%, 0.25-0.35: 37.5%, above 0.35: 69.9%),
but the middle band is **n=16**.

**Pre-committed rule: move the floor to 0.35 when that band reaches n ≥ 93** and still
underperforms the band above it. Not before, and not on judgement — otherwise this is the same
over-fitting refused for the confidence tiers on n=69.

### 9.7 Re-read date: **2026-09-15**

Snapshots began 2026-08-10, so the CLV sample starts from zero regardless of the 2,250 settled
outcomes already stored. Football settles ~87 fixtures/week; not all are snapshotted, so a
conservative ~45/week reaches the §9.1 sample of ~223 in **roughly five weeks**.

Deliberately **one week before the 22 September API-Football renewal decision**, so the first
real CLV read informs it rather than arriving after the money is spent.

**Interim reads are for instrumentation only.** Any figure before 2026-09-15 is a pipeline
check, not evidence, and must not be quoted as a track record in either direction.

---

## 10. One prediction per fixture (added 2026-08-13)

Found while building `ml/notebooks/prediction_history.ipynb`, which is exactly what that notebook
is for.

`run_predictions` **appends** a prediction row rather than replacing one, so a fixture
re-predicted as its features changed carries a series. Every metric in §4 was computed over rows,
which weights a fixture by how often it happened to be re-predicted:

    settled football PRE_MATCH rows   143
    distinct fixtures behind them      63     one of them carrying 25 rows
    reported accuracy              0.5524
    accuracy, one row per fixture  0.3651     <- an 18.7pp overstatement

All 143 were genuine forecasts made before kickoff, so this is not a leakage problem. It is a
denominator problem: **a fixture is one event and contributes one result.**

`_representative_prediction_ids` picks the last row created BEFORE kickoff, partitioned by
`(fixture_id, kind)`. Three choices worth stating:
- **Before kickoff, not merely newest.** `created_at` alone is not trustworthy — regeneration
  reset it on 91 football rows to timestamps after their own kickoffs (§2b already records this).
- **Partitioned by kind too**, so a fixture holding both a forecast and a retrodiction keeps one
  of each rather than one hiding the other from its own population.
- **The series is kept, not deleted.** It is a real record of how a forecast moved as injuries and
  odds landed. The defect was consuming it as though each row were an independent event.

Corrected live figures at the time of the fix — **neither is reportable**, both sit below the
§9 floor of 93:

    football   n=63   0.3651   CI [0.257, 0.489]
    tennis     n=75   0.6533   CI [0.541, 0.751]

Football now reads BELOW its own baseline. That is a worse number than was previously displayed
and it is the honest one; the previous figure was an artefact of counting.
