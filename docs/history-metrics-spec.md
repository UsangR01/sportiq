# `GET /history` — metrics specification

**Status:** draft, pre-registered. Written *before* the endpoint exists, deliberately, so its
shape is not chosen after seeing which cut of the data looks best.

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

## 9. Open, for P2 pre-registration

To be decided **before** the first read, not after:

1. The primary metric and population.
2. `MIN_REPORTABLE_N` and `MIN_BUCKET_N`.
3. What result counts as an edge, what counts as none, and what happens in each case —
   including the possibility that the answer is "stop surfacing Over/Under as a confident
   pick", which the r=+0.030 correlation already points at.
4. The re-read date, given both sports need months of accumulation.
