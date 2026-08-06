# Tennis Over/Under **Total Games** — build plan

Companion to `SportIQ_Tennis_Games_Viability.ipynb`, which contains the evidence behind every
number here.

**Recommendation: build it — but in an order that lets us stop cheaply if it does not work.**

---

## 1. Why the order matters more than the plan

Football's Over/Under goals market shipped on top of a model that could not predict goals.
Measured afterwards: predicted xG correlated with actual total goals at **r = +0.030**, and
every reliability bucket sat flat at the base rate. It looked like a prediction and behaved
like a coin flip. Fixing it took an xG data collection, out-of-fold stacking and a feature
prune — weeks of work, after shipping.

So the first thing done for tennis was the measurement that was skipped then:

| signal | vs total games | vs first-set games |
|---|---|---|
| `rank_diff` | r = +0.023 | −0.014 |
| `rank_gap` | r = +0.051 | −0.016 |

At n = 17,051 those confidence intervals exclude zero, which makes them *statistically*
detectable and *practically* nothing — about **0.25% of variance**. The live tennis model is a
win/loss classifier, and a win probability genuinely does not tell you match length: a 70%
favourite can win 6-0 6-0 (12 games) or 7-6 7-6 (26).

**Building on today's features would reproduce the football result exactly.**

---

## 2. What we already have

| | status |
|---|---|
| **Odds** | ✅ 61 real prices today, lines 21.5 / 22 / 22.5 / 23.5 (BetMGM, BetOnline, Bodog) |
| **Historical game counts** | ✅ 17,273 completed ATP matches, 2021–2025, already collected |
| **Surface** | ✅ every match |
| **Ingestion path** | ✅ TheRundown `total` block already parsed for football |

Nothing in v1 requires a new subscription. That is the point of the sequencing.

---

## 3. The two signals that exist

**Surface** — grass runs ~4.5 games longer than clay:

```
Grass  28.5     Hard  25.3     Clay  24.5      (overall sd 8.1)
```

**Player tendency** — across 279 players with 30+ matches, average total games spans
**21.8 → 30.6**, sd 1.49 between players. Big servers hold easily and produce short sets;
grinders produce long ones. A real, stable, learnable trait.

> **Known confound, not yet resolved.** Some of that spread is opponent quality, not style — a
> player who mostly faces weaker opposition wins quickly regardless of how they play.
> Separating the two is step 2 below, and it is the part most likely to fail.

---

## 4. Build sequence

### Step 1 — Player rolling-average features *(no new data, no new quota)*

From `set_scores`, which we already fetch and currently discard after counting sets.

- `avg_total_games_last_N` per player (N = 10 and 20)
- `avg_games_on_surface` per player, per surface
- `avg_games_conceded_on_serve` proxy — games lost while serving, derivable from set scores
- `tiebreak_rate` — share of that player's sets reaching 6-6

Storage: extend `tennis_game_log_atp.parquet`, mirroring how corners were merged into the
football game log. Live serving reads the same values from accumulated fixtures.

### Step 2 — Opponent adjustment

Regress each player's average against opponent rank, and use the **residual** as the feature.
This is what separates "plays long matches" from "plays weak opponents". If the residual
carries no signal, the trait was mostly opponent quality and the market is not viable —
**stop here.**

### Step 3 — Count model, and the gate

A Poisson or Negative Binomial regressor on total games — *not* a reuse of the win/loss
classifier. Then Over/Under probabilities from the fitted distribution, exactly as
`markets.py:over_under_probs` already does for football goals.

> **Check the dispersion before choosing.** Football's overdispersion was assumed twice and
> measured false (var/mean = 1.0030), which killed a Negative Binomial plan that would have
> fitted a parameter that did not exist. Measure `var/mean` on total games first.

**GATE — do not skip.** On a held-out season, report:

- Brier score per line (21.5 / 22.5 / 23.5)
- Reliability buckets: predicted probability vs observed frequency
- Cochran-Armitage trend z across buckets

**Ship only if the trend is significant and the buckets are not flat at the base rate.** For
reference, football's current Over/Under sits at **z = +3.35, p = 0.0008** — that is the bar,
and it took real work to reach.

### Step 4 — Wire the market *(only if the gate passes)*

- `OddsMarket.GAMES_TOTAL` + migration
- Map TheRundown's tennis `total` block in `therundown.py`
- Add candidates to `_all_market_candidates`
- Mobile: no change needed — `goals_total` rendering already generalises

---

## 5. Quota and cost

| need | source | tier | cost |
|---|---|---|---|
| Historical game counts | `/matches` `set_scores` | **ALL-STAR** ✅ | already collected |
| Surface | embedded in match | **ALL-STAR** ✅ | free |
| Rolling averages | derived | — | free |
| Odds | TheRundown ATP | **Pro** ✅ | already paid |
| Serve stats (hold %, aces, break points) | `/match_stats` | **GOAT** ❌ | upgrade required |

`/match_stats`, `/player_career_stats`, `/head_to_head` and `/odds` all return **401** on the
current plan — verified live, not assumed.

**Steps 1–3 cost nothing.** Buy GOAT only if the free features clear the gate but fall short of
useful — at that point you know the market works and you are buying accuracy, not hope.

---

## 6. Explicitly not building

**First-set games.** sd 1.97 on a mean of 10.0, bounded 6–13 by the rules of a set, no feature
above |r| = 0.02, and P(over 9.5) = 0.542. Least signal, most noise, and no odds for it in our
feed.

**Live / in-play.** Not a market question but an architecture one: ingest polls every 5 minutes
while in-play prices move in seconds. A stale in-play price is *misleading*, not merely old —
the user sees value that closed minutes ago. Needs real-time ingest and live odds first.

---

## 7. Effort

| step | estimate | risk |
|---|---|---|
| 1 — rolling features | 1–2 days | low, data in hand |
| 2 — opponent adjustment | 1 day | **highest** — may kill the market |
| 3 — model + gate | 2–3 days | medium |
| 4 — wire market | 1 day | low, follows football |

**~1 week, with a genuine stop point after step 2** — which is the part worth protecting.
