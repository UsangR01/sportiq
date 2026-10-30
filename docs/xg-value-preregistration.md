# Pre-registration: is live xG worth a recurring TheStatsAPI subscription?

Written 2026-10-30, **before any arm was run**. The point of pre-registering is that the
author does not get to relitigate after seeing the numbers — this project has twice had to
record an override because a rule was written loosely or late. Tolerances are stated here
rather than left implicit, which is the specific failure of the `LAST_N_FORM` rule.

## The question, and why the answer decides money

`team_features.xg_for_5`/`xg_against_5` have never had a source — API-Football returns no xG
at any tier. So the four rolling-xG features are present for ~98% of the 2025 test split and
absent for **100% of production**. The model is evaluated in a regime it never serves in.

xG is 4 of 23 Layer 1 features (17%), and it was adopted on measured evidence: rolling xG
correlates with actual total goals at r=+0.129 (CI [+0.055,+0.202]) against rolling goals at
r=+0.062 (CI spans zero), and it separated P(under 2.5) across quintiles 0.493 -> 0.309 where
rolling goals managed 0.036 and reversed mid-range.

Closing that gap means collecting xG **continuously** — it lives only at `/matches/{id}/stats`,
one call per match, no batching (a probe of the LIST endpoint returns 0 for every league).
At ~32 completed football fixtures/day that is ~1,000 calls/month forever, against a
100,000/month plan with no downgrade available.

**So the decision is: does removing xG measurably hurt? Arm B IS production's real feature
regime.** If it does not, we are paying to collect a feature that never reaches a user, and the
four features should be pruned from the vector so training stops measuring something serving
cannot have.

## Arms

Both at 33 leagues, identical split (TRAIN 2021-23 / VAL 2024 / TEST 2025), identical
`RANDOM_SEED`, both `--no-activate`. The ONLY difference is the four rolling-xG inputs.

- **Arm A** — 33 leagues, 23 Layer 1 features (current vector).
- **Arm B** — 33 leagues, 19 Layer 1 features (`--without-xg`).

Layer 2 is untouched in both: it consumes Layer 1's PREDICTED `xg_home`/`xg_away`, which is an
output, not one of the four inputs being dropped.

## PRIMARY criterion — the goals market, because that is what xG was bought for

`goals_total` currently leads roughly a fifth of cards and is admitted to headline picks;
corners is barred. Judge on what the market sells, not on MAE.

Removing xG (A -> B) is:

- **MATERIAL** if under-2.5 **or** under-3.5 Brier worsens by **>= 0.0020**, or if the
  under-3.5 reliability buckets lose monotonicity they had in Arm A.
- **NEGLIGIBLE** if **both** worsen by **< 0.0010** AND 1X2 RPS worsens by **< 0.0020**.
- **INCONCLUSIVE** otherwise — report it as such; it does not decide the subscription alone.

0.0020 is calibrated against this project's own history: Layer 1 tuning, described as the run
where "Over/Under finally moved", shifted under-3.5 Brier by 0.0030. Half that is the smallest
move worth paying a recurring bill for.

## SECONDARY, reported but not deciding

1X2 accuracy and RPS; xG MAE home/away (Layer 1's own job); scoreline log loss; corners Brier
at 9.5/10.5; the under-3.5 bucket spread and trend z (discrimination is the constraint
repeatedly identified as binding on this market).

## Decision rule

- MATERIAL -> live xG is worth having. Keep or re-subscribe, and wire ingest-time collection.
- NEGLIGIBLE -> cancel, and prune the four features from `FEATURE_NAMES`, because training on a
  feature serving can never supply is a train/serve mismatch we are choosing to keep.
- INCONCLUSIVE -> cancel on cost (1% utilisation of a plan with no downgrade), prune nothing,
  and record that the question is open.

## The 33-league retrain is a SEPARATE decision, with a separate rule

Adding 11 leagues changes the test set, so Arm A's headline accuracy is **not** comparable to
the incumbent `football_xgb_` trained on 22. Per this project's standing rule, compare each
against **its own** always-home baseline.

Promote Arm A only if its gap over its own baseline is **>= the incumbent's gap over its own**,
and RPS does not worsen by more than 0.0020. Otherwise the incumbent stays and Arm A is left
registered-but-inactive, like every other measurement arm.

## The incumbent's numbers, read BEFORE either arm finished

Pulled from the local MLflow store while Arm A was still assembling examples, so the promotion
bar is a fixed target rather than one chosen after seeing what Arm A scored.

    incumbent  football_xgb_ (22 leagues, trained 2026-08-23)
      test accuracy 0.5082   its own always-home baseline 0.4481   gap +6.01pp
      RPS 0.2084

So Arm A is promoted only if **gap >= +6.01pp against its own baseline** and **RPS <= 0.2104**
(0.2084 + the 0.0020 tolerance). Note the accuracy figures themselves are NOT comparable: Arm A
is scored on a 33-league test set and the incumbent on a 22-league one.
