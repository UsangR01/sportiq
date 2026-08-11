# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

- `SportIQ-PRD.docx` — Product Requirement Document v1.3
- `SportIQ-TDD.docx` — Technical Design Document v1.5
- `keys.docx` — **contains live API keys, a database connection string with plaintext password, Redis URL, Sentry DSNs, and an app secret key. Gitignored. Never read its contents into a commit, a generated file, or a tool call whose output could be persisted/logged. Never add it to `.gitignore` exceptions.**
- `feature_engineering.ipynb`, `Running - NBA Games Prediction Project.ipynb` — the user's own prior personal NBA-prediction project, added to the repo root as a source of feature-engineering ideas (real iterative Elo, win/loss streaks, richer H2H, Big5 key-player-by-box-score-presence) adopted into football's model/retrodiction — see "Real historical data + Elo/streaks/richer H2H" below. Not part of the SportIQ product itself; kept for reference/provenance, not imported or executed by any pipeline.
- `backend/` — FastAPI backend scaffold (see "Backend implementation status" below).
- `ml/` — NBA and (EPL-scoped) football model training pipelines (see "ML training" below).
- `mobile/` — Expo/React Native app scaffold (see "Mobile implementation status" below). No `infra/` yet.

The architecture section below is the *intended* design per the TDD/PRD. Where the real backend/mobile app now exist, prefer reading the source over the docs; the docs still lead for anything not yet built (ML training beyond NBA, infra).

## Backend implementation status

Real logic exists for: auth (`POST /auth/register|login|refresh`, Argon2id + JWT + rotating refresh tokens), guest sessions (`POST/PUT /guest/session`), `GET /picks` (the full TDD §4.2 algorithm — see `app/picks/service.py` for the DB-free EV/threshold/best-outcome math, unit-tested in `tests/test_picks.py`), `GET /sports`, `GET /fixtures*`, `GET/PUT /user/preferences`, `PUT /user/push-token`, and `GET /stats/model`. All SQLAlchemy models for the TDD §2.1 schema exist and migrate via Alembic.

**`GET /stats/model`** (TDD §4.1) returns the currently *active* `models_registry` row per sport (optionally filtered by `sport_slug`), joined to the sport's slug — reflects whichever model version is actually serving predictions right now, matching TDD §3.1's "promotion is a DB update, not a redeploy" design. `ModelRegistry` gained a `roi_simulation` column (nullable — a small-sample, directional backtest metric, not every version will have one) so `ml/training/train_nba.py`'s flat-stake ROI calculation is persisted going forward instead of only printed; the currently-active NBA row was backfilled with its real, already-known value (`0.20873793103448263`, from the training run recorded under "ML training" below) rather than left null or recomputed. Tested in `backend/tests/test_model_stats.py` with real seeded `Sport`/`ModelRegistry` rows (both an active and an inactive version, to confirm only the active one is ever returned).

**`DataSourceAdapter` methods that are real and live-verified**: `BallDontLieAdapter.fetch_fixtures`/`.fetch_team_stats` (NBA), `TheRundownAdapter.fetch_odds` (every sport except football's Brasileirão league — TDD §6.2's "odds is always TheRundown" no longer holds universally, see below), and — since the user obtained a real API-Football **Pro** subscription — `APIFootballAdapter.fetch_fixtures`/`.fetch_team_stats`/`.fetch_injuries`/`.fetch_odds` plus a standalone `fetch_h2h_win_rate` (all sports/leagues; see "Football model + key-player availability" below and the updated "External API research findings" entry). All make real HTTP calls and have been run against the live API end to end. `RotoWireAdapter`, `BallDontLieAdapter.fetch_injuries`, `TheRundownAdapter.fetch_fixtures`/`.fetch_team_stats`, and `SportsDataIOAdapter` entirely remain stubbed. Before ingestion can do anything, run `PYTHONPATH=. python scripts/seed_sports.py` once — nothing seeds a `Sport`/`League` row otherwise (this now seeds football + its 6 leagues too), so `ingest_fixtures`/`ingest_odds` silently have zero sports to iterate.

**`NBAModel` is a real, trained model** — see "ML training" below. `run_predictions.py` produces genuine `Prediction` rows (verified against the real Pistons-vs-Suns fixture: home_prob 0.565, confidence MEDIUM); `GET /fixtures/{id}` shows real odds and a real prediction joined together. `/picks` itself still won't show that particular fixture — it's `completed` with a January kickoff (a historical fixture kept around for cross-task verification), and `/picks` correctly only returns `scheduled` fixtures in the next 7 days; that's the filter working, not a gap.

**Big3/Top5 key player availability (TDD §3.3, §2.1) is real and fully wired**, in two stages that must never be merged into one query (see `app/models_ml/nba_key_players.py`'s module docstring):
- **Stage 1** (season-level, backward-looking): `ml/training/compute_key_players.py` ranks each team's players by a trailing WS/48 approximation among a 26+ MPG pool (falling back to the 18-26 MPG band if fewer than 5 qualify), writing the Top 5 to `team_key_players` once per team/season. Re-runnable/idempotent (delete-then-insert per team+season). Run for real across all 6 seasons — e.g. Detroit Pistons 2025: Jalen Duren, Cade Cunningham, Tobias Harris, Ausar Thompson, Duncan Robinson.
- **Stage 2** (pre-game, forward-looking, live production): `get_key_player_availability(db, team_id, season_year)` reads **only** `player_injury_status` (joined to `team_key_players` by player name, case-insensitive — the two tables share no ID space, same cross-provider mismatch pattern as fixtures/odds) and writes `TeamFeatures.key_players_available`/`.key_players_per_combined`. A player with zero injury-status rows at all counts as *available* (not on any injury report is itself informative); only a team with zero `team_key_players` rows for the season produces `(None, None)`. Computed at ingest time (`ingest_fixtures.py`) and again whenever the re-inference trigger fires.

> **MEASURED: the Stage 2 path is effectively inert — but the join is NOT the main reason.**
> An earlier version of this note said the name join "resolves 1% of the time" and called it a
> broken join. That framing was wrong and is corrected here rather than quietly edited.
>
> The 1% came from comparing 130 injured players against 2,609 top-5 names spanning **six
> seasons**, when `player_injury_status` holds only CURRENT state — most of that denominator
> could never match. Against the live 2026 rosters it is 7 of 250, i.e. **2.8%**. Low, but a
> different number and a different problem.
>
> Joining by name costs little: matching on `player_id` instead gives **31 vs 27** players.
> Both sides are API-Football ids for football, so the ID join is the correct one and worth
> switching to — it just is not the fix.
>
> The real constraints are **coverage and duplication**:
> - the injury feed holds **130 distinct players**, spanning only **2026-08-05 → 2026-08-10**,
>   because `INJURY_LOOKAHEAD_DAYS = 3` queries only dates with fixtures in the next three days
>   and most leagues are between seasons;
> - **12,330 rows for those 130 players = 95 duplicate rows each**, since the fixture-scoped
>   feed re-records the same injury every run instead of upserting;
> - Stage 1 holds **250 rows for 2026** against ~700 per completed season, so most teams have no
>   current roster to attach an injury to in the first place.
>
> Consequence is unchanged: where a roster exists, **132 of 143** `TeamFeatures` rows (92%)
> report all five available, so the live signal is near-constant and cannot move any model.
>
> **This is the prerequisite for any future player-availability work.** An external review of
> the dropped key-player feature correctly identified real derivation flaws — the top-5 was
> selected from WHOLE-SEASON data and applied to fixtures earlier in that same season (real
> leakage), and training used post-match appearance rather than anything knowable pre-kickoff.
> Both true. But every proposed replacement still depends on knowing who is unavailable, and
> that pipeline currently resolves 1% of the time. **Fix the join and confirm absences actually
> surface before rebuilding the feature** — otherwise it is a feature built on a variable we
> cannot observe. Availability very likely does matter in football; what has been measured is
> that our measurement of it is broken.
>
> A second, separate cause of missingness is NOT a bug: `key_players_available` is NULL for
> 100% of ATP rows by design (tennis never populates `TeamKeyPlayer`) and for pre-season leagues
> that have no per-player stats yet. Stage 1 holds ~800 rows per historical season but only 250
> for 2026, so it also needs re-running for the current season once matches have been played.
- **Re-inference trigger** (`ingest_injuries.py`) now checks `team_key_players` membership by name (not a salary-rank proxy) *and* an actual per-fixture 3-hour-before-tip-off window (`Fixture.kickoff_utc.between(now, now+3h)`, previously just "any fixture today") *and* actually dispatches `run_predictions.delay(...)` (previously logged only — dead code from before a trained model existed).
- **The historical-training-label counterpart is a deliberately separate, non-reusable function**: `ml/training/train_nba.py`'s `historical_key_player_availability` derives an availability label from **completed-game box-score presence** (`nba_api` player-game-logs, cached via `collect_nba_data.py`'s `collect_player_game_log`) — fine for backtest labels, explicitly documented as **never** to be imported into the live Stage 2 path (and kept in `train_nba.py`, outside `nba_key_players.py`, specifically so it can't be accidentally imported live). `backend/tests/test_nba_key_players.py::test_stage2_follows_injury_status_not_box_score` asserts the two diverge on the same underlying facts (a player marked `OUT` in `player_injury_status` who nonetheless has real minutes in the box score) — this is the regression guard against Stage 2 ever drifting into target leakage.
- `ws_48`/`per` are **explicitly simplified, documented approximations** (a PIE-based per-48 value formula; a Hollinger-style uPER rescaled to PER's 15.0 league-average convention) — not bit-exact Basketball-Reference/Hollinger reproductions.
- Manually verified end-to-end with a synthetic `player_injury_status` row (no real RotoWire/BallDontLie injury access — see below): marking a real Top-5 player `OUT` dropped `key_players_available` 5→4, flowed through `_maybe_trigger_reinference` into `TeamFeatures`, and changed the feature vector `_run_predictions` actually saw.
- A pre-existing, never-before-exercised bug was found and fixed while rebuilding `ingest_injuries.py`: `PlayerInjuryStatus(team_id=update.team_external_id, ...)` was assigning the injury provider's own external team ID directly into the internal UUID FK column (same bug class as earlier fixture/odds work) — fixed via a new `_resolve_team` helper that resolves through `Team.external_id` first. Never triggered before because both injury adapters (`RotoWireAdapter`, `BallDontLieAdapter.fetch_injuries`) were still `NotImplementedError` stubs.
- **TDD §7's NBA cost-breakdown table is stale** — it still describes a "salary-weighted historical injury-impact proxy," language from before this feature replaced that design. Flagging it, not editing the TDD.

## Football model + key-player availability (TDD §3.2/§3.3) — real, trained, live-verified

Built once the user obtained a genuine API-Football **Pro** subscription (7,500 req/day; confirmed via `GET /status`: `active: true`, `end: "2026-08-29T08:58:56+00:00"` — **the subscription is time-limited to about a month from when this was built**, a real constraint, not a permanent unlock). This replaced the prior free-tier key that blocked `next`/current-season fixtures entirely (see the updated "External API research findings" entry below).

**Deliberate scope decision, not a hidden gap**: the pipeline is built generically (works for any of the 5 domestic leagues — EPL/Ligue1/Bundesliga/LaLiga/SerieA, all seeded via `scripts/seed_sports.py`, all with working fixture/odds ingestion), but **real trained-model historical data collection now covers EPL and Brasileirão** (5 seasons each, 2021–2025 — Brasileirão was added specifically to give retrodiction and the model itself more real data; see "Real historical data + Elo/streaks/richer H2H" below). The other 3 European leagues are still seeded with working fixture/odds ingestion but have no historical training data collected. A full 5-league × 5-season historical lineup-presence collection would run ~8–9k API calls, close to the daily ceiling in one sitting, and the subscription itself is only active for ~1 month — expanding to the remaining 3 leagues is a natural, bounded follow-up.

**Two-layer model (TDD §3.2)** — `app/models_ml/football.py`: two XGBoost Poisson regressors (`objective="count:poisson"`, one per side) produce expected goals from `app/models_ml/football_features.py`'s **18** pre-match features, then an XGBoost multiclass classifier (`objective="multi:softprob"`) predicts home/draw/away from those two xG values plus `FootballModel.LAYER2_CONTEXT_FEATURES` (form, H2H incl. avg goals, Elo diff, win streaks) —

> **The vector is 18, NOT the 21 this document claimed until 2026-08-09.** Commit `d0a24d9`
> pruned the four key-player features and `moneyline_implied_prob_home` after measuring 1X2
> accuracy IDENTICAL without them and Over/Under discrimination BETTER. This stale line is
> not a harmless typo — it cost ~10,500 real API calls on 2026-08-09, spent collecting
> lineups and rosters for nine new leagues to populate features the model can no longer
> consume. **`app/models_ml/football_features.py:FEATURE_NAMES` is the only authority on
> what the model reads; check it before collecting anything to feed it.** The same applies
> to `compute_football_key_players.py`, which still computes Stage 1 rankings that no
> football feature currently uses — it is retained for the live Stage 2 availability path
> and the mobile display, not for training.
 `requirements.txt`'s `catboost` dependency stays unused; XGBoost does both layers rather than mixing boosting libraries. One-vs-rest isotonic calibration per class, renormalised to sum to 1 (the 3-way-market extension of NBA's binary calibration). A documented simplification: Layer 2 trains on Layer 1's own in-sample predictions for the training split (a proper stacked-generalization setup would use out-of-fold predictions there) — flagged in `train_football.py`'s docstring, not hidden.

**Real historical data + Elo/streaks/richer H2H — a real pivot, driven by wanting genuinely trustworthy past-game (retrodicted) predictions.** The user asked how many seasons of data predictions actually draw on, was told the honest answer (training: 6 NBA seasons / 5 EPL-only football seasons; but *retrodiction* for a past game only had our own live DB's ~7-day fixture history to work from — a real, thin gap), and in response asked for retrodiction to use ALL real historical data for the season, and for the model's own accuracy to be pushed higher using ideas from the user's own prior personal NBA-prediction project (two Jupyter notebooks, `feature_engineering.ipynb` and `Running - NBA Games Prediction Project.ipynb`, added to the repo root). That notebook's own real walk-forward backtest (`RidgeClassifier` + `SequentialFeatureSelector`, genuinely leakage-safe `train = seasons < current`) scored **62.65% accuracy** — LOWER than this project's own NBA model (68.57%), so the useful takeaway was specific FEATURE ideas, not the notebook's modeling approach:
- **Real, persistent, iterative Elo rating** (`app/models_ml/elo.py`, adopted from the notebook's own sequential Elo-update loop): `INITIAL_ELO=1500`, `K_FACTOR=32`, standard win/draw/loss update. Architecturally different from every other feature here — it needs genuinely STATEFUL, sequential computation across a team's entire match history, not an independently-re-derivable rolling window. Two code paths, by necessity: `elo.compute_elo_history(games_df)` walks a full historical game log ONCE in chronological order for training (`ml/training/train_football.py`); live serving instead reads real, incrementally-updated state off a new `teams.elo_rating` column (Alembic migration `f3a8b1c9d4e2`), updated exactly once per real completed match in `app/workers/ingest_fixtures.py:_maybe_settle_outcome` (the same idempotency guard that already prevented double-writing the `Outcome` row now also prevents double-applying Elo). The model itself only ever sees `elo_diff` (home minus away), not two absolute ratings — Elo is only meaningful relatively (matches how the notebook itself used `elo_rating` vs `elo_rating_opp`).
- **Win streaks** (`win_streak_home`/`win_streak_away` — `losing_streak` is tracked too, on `TeamStats`/`TeamFeatures`, but deliberately left out of the model's own feature vector since "not on a win streak in this match" already implies it, per the notebook's own boolean-mask-cumsum streak trick, reimplemented here as a plain reversed scan). Training reads this straight from the game log (`football_features.py:_win_streak`); live serving reads API-Football's real `"WWDLW..."` form string (`app/adapters/api_football.py:_parse_streaks` — a draw at the most recent match breaks both streaks to `(0, 0)`).
- **Richer H2H**: `fetch_h2h_stats` (new, alongside the existing `fetch_h2h_win_rate`) extends the *same* `/fixtures/headtohead` call to also return average goals scored/allowed vs the specific opponent — real data the endpoint was already returning, just not read before. `h2h_avg_goals_scored_home`/`h2h_avg_goals_allowed_home` join `h2h_win_rate_home` in both `assemble_from_game_log` and `assemble_from_live_db`.
- **Real historical Brasileirão data collection**: `ml/training/collect_football_data.py` was generalized from EPL-only to a `LEAGUE_CONFIGS` dict (mirroring `compute_football_key_players.py`'s existing `TARGET_LEAGUES` pattern) — **3,800 real game-log rows and 42,971 real lineup-presence rows across 1,900 real Brasileirão fixtures**, 5 seasons (2021-2025, using the same integer season labels as EPL despite Brasileirão's different Jan-Dec calendar — confirmed live, both conventions accept the same season-year integer as a query param). Parquet files are now per-league-suffixed (`football_game_log_epl.parquet`/`football_game_log_brasileirao.parquet`, etc.) rather than one shared unsuffixed set, so adding a league never overwrites another's already-collected data. **Brasileirão has zero historical odds collected** (TheRundown has zero Brazil-league coverage at all, confirmed live/documented below; API-Football's own real Brasileirão odds coverage doesn't extend to arbitrary historical dates the way lineups/fixtures do) — a real, accepted gap, not fabricated data. `ml/training/compute_football_key_players.py`'s `TARGET_LEAGUES` was likewise extended so Brasileirão's Stage 1 key players are now computed for all 5 historical seasons (previously only its current, live season) — needed so retrodiction's real-lineup-presence key-player feature (see below) has real Top-5 rosters to check historical box scores against.
- **Real, pooled retraining** (`ml/training/train_football.py`, now EPL+Brasileirão pooled — safe since no team ID collides between the two providers' team spaces, so each team's Elo/rolling-form state only ever evolves from its own league's real matches): **47.89% test accuracy vs. a 46.45% "always pick home" baseline** (the baseline itself shifted from 42.63% since pooling changed the home-win-rate prior — not an apples-to-apples number vs. the old EPL-only run), real RPS **= 0.2138** (improved from 0.2223). Real trained artefact registered as `football_xgb_v20260729144210` in `models_registry`. **A genuine data-quality bug was found and fixed while computing the ROI metric on this larger pool**: a handful of collected odds rows carry an implausibly extreme real decimal price (e.g. 100.0, matching an American +9900 quote) — not the already-documented masked-book `0.0001` sentinel (`_american_to_decimal` already filters that), just a bad/stale real quote from one of the 3 unlocked TheRundown affiliates. Left in, `.groupby(...).max()` picks these as "best odds" and inflates the ROI metric to an absurd +206%; fixed with a `PLAUSIBLE_MAX_DECIMAL_ODDS = 15.0` filter before the groupby. With the fix: flat-stake ROI on n=45 home-favoured picks with real odds = **-11.5%** — a real, small-sample, negative-but-plausible number (not a positive signal, same "no real edge demonstrated yet" honesty as the pre-fix EPL-only run).
- **Retrodiction rebuilt** on top of all of the above — see the updated "Retrodicted predictions" section below.

**Big3/Top5 key player availability, football's Stage 1 (TDD §3.3)** — `app/models_ml/football_key_players.py`, mirroring NBA's design:
- Ranks by API-Football's own real per-match `games.rating` stat directly — **no hand-derived approximation needed** (unlike NBA's WS/48/PER, which had to be approximated since Basketball-Reference's real formulas aren't practically reconstructible here). `TeamKeyPlayer.rank_metric == .combined_metric` for every football row by design, not an oversight.
- Gating pool is season-**total** minutes (≥900 ≈ 10 full matches, falling back to ≥450 ≈ 5 matches), not NBA's per-game MPG bands — a 90-minute match / ~38-match season doesn't map onto NBA's 48-minute/82-game convention, and total minutes better reflects genuine rotation status than a per-appearance average.
- Real Stage 1 data written across all 5 EPL seasons via `ml/training/compute_football_key_players.py` — e.g. Arsenal 2025: D. Rice (7.46), Gabriel Magalhães (7.36), B. Saka (7.24), Martín Zubimendi (7.06), W. Saliba (7.04).
- **Stage 2 is shared, unchanged, with NBA** — moved to a new `app/models_ml/key_player_availability.py` module (both `nba_key_players.py` and `football_key_players.py` now hold only Stage 1; `nba_key_players.py` re-exports `get_key_player_availability` for backward compatibility). The underlying `team_key_players`/`player_injury_status` join-by-name logic was already sport-agnostic once `TeamKeyPlayer.ws_48`/`.per` were renamed to `rank_metric`/`combined_metric` (pure rename, zero behavior change for NBA — see the schema divergence below).
- The historical-training-label counterpart lives in `train_football.py`'s `historical_key_player_availability`, built from real per-fixture lineup/appearance data (`/fixtures/players`, the football "box score") — kept out of `app/models_ml/`, same leakage-guard separation as NBA's. `backend/tests/test_football_key_players.py::test_stage2_follows_injury_status_not_lineup_presence` is the regression guard, mirroring NBA's own required leakage test.
- Manually verified end-to-end against **real production data** (not a synthetic team): looked up Arsenal's real 2025 Top 5 via `get_key_player_availability`, inserted a real-shaped `PlayerInjuryStatus` row marking D. Rice `OUT` (source `API_FOOTBALL`), confirmed availability dropped 5→4 and the combined-metric total dropped by exactly D. Rice's own rating (36.16→28.70) — then cleaned up the synthetic row afterward so it doesn't pollute real ingest data.

**API-Football's `/injuries` is fixture-scoped** (`player.type: "Missing Fixture"`), structurally different from RotoWire's rolling current-status feed the original NBA design assumed — `APIFootballAdapter.fetch_injuries` adapts this into the same bulk-fetch shape the ABC already expects by querying `/injuries?league=X&season=Y&date=Z` for every date with a real upcoming fixture in the next 3 days, across every league in `LEAGUE_IDS` (season computed per-league — see the Brasileirão entry below for why this matters). Every returned row maps to `InjuryStatus.OUT` — this feed has no GTD/doubtful signal at all, unlike RotoWire's. Confirmed live: genuinely empty for fixtures too far in the future (no fabricated data), which is correct, not a gap.

**A real pre-season data-quality issue was found and fixed while running this against the live, not-yet-started 2026-27 season**: API-Football's `/teams/statistics` returns `"average": {"total": "0.0"}` (a **string zero**, not `null`) for a team with zero matches played yet in a brand-new season — `_compute_team_stats` was initially treating this as a real 0.0 goals-per-game value rather than "genuinely unknown," which would have fed a fabricated neutral signal into the model. Fixed by gating `attack_str`/`defence_str` on `fixtures.played.home + .away > 0` before trusting the goals-average fields at all — same "never fabricate a neutral value" principle documented in `nba_features.py`. Confirmed live: real EPL `TeamFeatures` rows for the actual 2026-08-21 season-opening fixtures now correctly show `None` across the board (zero matches played yet in the new season) rather than misleading zeros.

**API-Football has a real, dedicated head-to-head endpoint** (`/fixtures/headtohead?h2h={id1}-{id2}`) — simpler than NBA's `fetch_h2h_win_rate`, which has to manually search one team's own fixture history since BallDontLie has no dedicated H2H endpoint. Both are standalone functions outside the `DataSourceAdapter` ABC (H2H needs both teams, doesn't fit `fetch_team_stats(team_id)`'s per-team shape).

**A 6th league — Brasileirão (Brazil Série A, "Brasileirão Betano") — was added specifically because it's actually in season** (confirmed live: current season "2026" runs 2026-01-28 to 2026-12-02) while all 5 European leagues are in their off-season until 2026-08-21, so this is the only league with real live fixtures/predictions to actually observe right now:
- Real league_id **71**, confirmed via `GET /leagues?country=Brazil` (found under the plain name "Serie A" — API-Football doesn't disambiguate Brazil's Série A from any other country's in its `name` field, only `country`).
- **A real season-convention bug was found and fixed while adding this**: `_current_football_season` originally assumed every football league follows the European Aug-May convention (season year = the year it starts). Brasileirão instead runs Jan-Dec of a single calendar year — applying the European rule to it would compute the *wrong* season for most of the year (any month before July would look a full year too far back). Fixed by making `_current_football_season(league, now)` branch on a new `CALENDAR_YEAR_SEASON_LEAGUES = {"brasileirao"}` set; `fetch_injuries` (which loops over every league) now computes the season **per-league inside the loop** rather than once globally — it was silently applying one shared season value to every league before this, which would have been subtly wrong the moment a second calendar-year league was ever added, even though it happened to be harmless while every configured league shared the same convention.
- **TheRundown has zero Brazil-league coverage** (confirmed live via `GET /sports` — no Brazil-related `sport_id` exists at all on this subscription), so `TheRundownAdapter.fetch_odds("football", "brasileirao", ...)` correctly raises via `_rundown_sport_id_for`. This exposed a real latent bug in `ingest_odds.py`: `_ingest_odds()`'s per-league loop had no exception isolation, so one league's unmapped-provider error would have crashed odds ingestion for *every other league and sport* in the same run. Fixed with a targeted `try/except ValueError` around each league's odds fetch, logging and continuing — fixtures/team-stats/injuries/predictions for Brasileirão are entirely unaffected since none of them depend on TheRundown.
- Stage 1 (`ml/training/compute_football_key_players.py`) now takes a `(league_slug, [seasons])` list rather than one hardcoded league/season-range — EPL keeps its full 5-season historical window (backing the trained model), Brasileirão gets only its current season (real data written: **88 `team_key_players` rows across 20 teams** — fewer than the full 100 since a mid-season league naturally has some teams with fewer than 5 qualifying players so far, one team currently has zero real qualifying rows at all, which is itself correctly reflected as `(None, None)` availability rather than a fabricated value). The registered `FootballModel` itself stays EPL-trained — this doesn't retrain or add a Brasileirão-specific model, just gives Stage 2 real availability data for a real in-season league, consistent with the TDD's one-model-per-*sport* (not per-league) design.
- Verified live end-to-end: `ingest_fixtures` pulled **10 real Brasileirão fixtures** (some genuinely `live` at the moment this was run — e.g. Botafogo vs Grêmio, kickoff 2026-07-29 20:00 UTC), and `run_predictions` produced a real prediction for a real scheduled fixture (Internacional vs Flamengo): home/draw/away = 0.179/0.247/0.573, confidence `MEDIUM` — a sensible result (Flamengo is a genuinely stronger side, correctly favored on the road).

**3 more leagues — Scottish Premiership, MLS, and the Chinese Super League — added per direct user request**, following the exact Brasileirão precedent (seed + generic pipeline, no dedicated model retrain): real league IDs confirmed live via `GET /leagues?search=X`/`?country=X` — Scottish Premiership couldn't be found by an "MLS"/"Scottish Premiership" name search at all (API-Football's own `name` field is just bare "Premiership"/"Major League Soccer", disambiguated only by `country`), Scottish Premiership=**179** (country=Scotland), MLS=**253** (country=USA, found only via `GET /leagues?country=USA` after `search=MLS` returned only "MLS All-Star"/"MLS Next Pro"), Chinese Super League=**169** (country=China, `name` field is just "Super League").
- **Season convention, confirmed live per league rather than assumed**: Scottish Premiership follows the same Aug-May European convention as the 5 original leagues (2026 season: 2026-07-31 to 2027-04-10 — literally the day after this was added) and does NOT go in `CALENDAR_YEAR_SEASON_LEAGUES`. MLS (2026: 2026-02-21 to 2026-11-08) and the Chinese Super League (2026: 2026-03-06 to 2026-11-08) are both real calendar-year leagues like Brasileirão and were added to that set.
- **Real coverage differs sharply by league, confirmed live via each league's own `/leagues` season-coverage flags before assuming anything**: MLS is the standout — its current 2026 season has full real coverage across the board (fixtures/lineups/statistics/players/injuries/odds all `true`) and is genuinely mid-season right now, the same "actually in season" reason Brasileirão was prioritized originally. The Chinese Super League is also mid-season with full coverage except `injuries: false` for 2026 specifically (a real, accepted gap — `fetch_injuries` just returns nothing for CSL, same graceful-empty behavior already established elsewhere). Scottish Premiership's new season hasn't kicked off yet (starts tomorrow relative to when this was added) — every coverage flag for season 2026 is `false` except `odds`, so `TeamFeatures`/Stage 1 key players are correctly all-`None`/zero until real matches start accumulating stats; this is the same pre-season "genuinely unknown, don't fabricate a neutral value" situation already documented for `_compute_team_stats`, not a bug.
- **TheRundown coverage, confirmed live via `GET /sports`**: MLS has real coverage (`sport_id=10`, added to `_RUNDOWN_SPORT_IDS`) — the only one of the three where TheRundown and API-Football's own `/odds` are both genuinely real and complementary. Scottish Premiership and the Chinese Super League have **no** TheRundown entry at all (same real gap as Brasileirão's missing Brazil entry) — both get no `_RUNDOWN_SPORT_IDS` key, so `_rundown_sport_id_for` raises for them and `ingest_odds.py`'s existing per-adapter `try/except ValueError` isolation (originally added for Brasileirão) already handles it with zero new code; both leagues' real odds come from API-Football's own `/odds` alone, confirmed live: **721 rows across 6 Scottish Premiership fixtures, 458 rows across 16 CSL fixtures** (plus 740 MLS rows from the combined TheRundown+API-Football pull).
- Stage 1 (`ml/training/compute_football_key_players.py`) gained all three at `[2026]` only (current season, mirroring Brasileirão's original addition, not EPL's full 5-season depth) — real data written: **0 rows across 12 Scottish Premiership teams** (expected — no season means no real per-player stats to rank yet, will need a re-run once matches start), **125 rows across 30 MLS teams**, **80 rows across 16 CSL teams** (a clean 5-per-team since the CSL happens to have zero teams with fewer than 5 qualifying players so far).
- **A real, previously-latent bug across 4 leagues (including the already-existing Brasileirão) was found and fixed while doing this**: `compute_football_key_players.py`'s `get_or_create_team` call was populating `Team.short_name` from API-Football's own `team.code` field (e.g. `"COL"`) instead of the full team name `ingest_fixtures.py` already uses for this column (per the documented "API-Football has no short abbreviation field... own name is used as short_name" convention above) — and that `code` field turns out to **not be unique within a league**: confirmed live, Colorado Rapids AND Columbus Crew both code to `"COL"` in real MLS data, and the same collision pattern already existed, latent, in Scottish Premiership (`"DUN"` ×2), the Chinese Super League (`"SHA"` ×3), and Brasileirão itself (`"ATL"` ×3, `"COR"` ×2) — the last one had simply never been exercised because Brasileirão's real odds always hit the `Fixture.external_id` direct-match fast path, never falling through to the fuzzy team-abbreviation join that actually reads `short_name`. Surfaced as a real `MultipleResultsFound` crash the first time real MLS odds were ingested (TheRundown's fuzzy-match path does read `short_name`). Fixed two ways: (1) `compute_football_key_players.py` now passes `short_name=team["name"]`, matching `ingest_fixtures.py` exactly, so whichever code path creates a Team row first no longer matters; (2) a bulk `short_name = name` backfill was run against every existing football `Team` row (94 rows corrected across all affected leagues); (3) `app/fixtures/service.py:find_fixture_by_abbreviations_and_time` was hardened regardless — an ambiguous multi-team or multi-fixture match now degrades to "no confident match" (`None`) instead of raising, consistent with the function's own stated "never guess" contract, so a future collision anywhere can never again crash a whole league's odds-ingestion run. Regression-tested in `backend/tests/test_fixture_matching.py`.
- Verified live end-to-end for all three: real fixtures ingested (Scottish Premiership's real 2026-27 opener fixtures, e.g. Dundee Utd vs Rangers 2026-07-31; MLS's real recently-completed and upcoming fixtures; CSL's real recently-completed fixtures), real odds ingested (see above), and `run_predictions` produced real predictions for a real scheduled fixture in each league (Celtic vs Dundee, CF Montreal vs New England Revolution, Tianjin Teda vs Yunnan Yukun) — all three used the same registered EPL/Brasileirão-trained `FootballModel`, consistent with the TDD's one-model-per-*sport* design; no retrain was needed or attempted. One genuinely interesting, verified-benign finding while spot-checking these: the Scottish Premiership fixture (all-`None` pre-season features) and the MLS fixture (real, distinct features — different Elo/attack/defence/form per team) produced **byte-identical** calibrated home/draw/away probabilities (0.571/0.232/0.198) — confirmed NOT a bug by comparing each fixture's raw, pre-calibration Layer 1 xG output (genuinely different: e.g. corners_xg_home 4.64 vs 7.88), which proves each fixture's real features did reach the model; the two matchups' raw scores simply happened to land in the same isotonic-calibration step, a known characteristic of isotonic regression's piecewise-constant output with a modest training sample, not a wiring defect.
- **Deliberately out of scope, matching Ligue1/Bundesliga/La Liga/Serie A's existing precedent**: no historical training-data collection (`ml/training/collect_football_data.py`'s `LEAGUE_CONFIGS`) for any of these three — the registered `FootballModel` stays EPL/Brasileirão-trained; these three get real fixtures/odds/injuries/predictions/Stage-2-availability from the shared model, just not their own historical backtest data or a dedicated retrain, unless asked for.

**API-Football's own `/odds` is now a real, second odds source — closing the odds gap Brasileirão originally had, without replacing TheRundown**: confirmed live via each league's `/leagues` `coverage.odds` flag that this is genuinely per-league, not per-sport or plan-wide — Brasileirão has real coverage (**13 real bookmakers per fixture, including Bet365, Pinnacle, and Betfair** — richer than TheRundown's 3-of-~15-unmasked situation, and already real decimals, no American-to-decimal conversion needed), while all 5 European leagues report `coverage.odds: False` on this same Pro plan (unchanged from the original pre-Pro finding). The two providers are complementary per league, not redundant: TheRundown remains the only real odds source for the 5 European leagues; API-Football is now the only real odds source for Brasileirão.
- `AdapterFactory.get_odds_adapter()` (singular, always-TheRundown) is now `get_odds_adapters(sport_slug) -> list[DataSourceAdapter]` — football resolves to `[TheRundownAdapter, APIFootballAdapter]`, every other sport still resolves to `[TheRundownAdapter]` only, preserving TDD §6.2's original "odds is always TheRundown" behavior everywhere it still holds. `ingest_odds.py` queries every adapter for a league and merges results; a `ValueError` from one adapter (a league it has no coverage for) is caught per-adapter so it can't block a *different* adapter's real data for the same league.
- **A real simplification fell out of this**: API-Football's `/odds` response carries its own fixture id — the *same* id space as `Fixture.external_id` for any football fixture (since API-Football is also the fixtures/stats provider). `app/workers/ingest_odds.py:_resolve_fixture` now tries a direct `Fixture.external_id` match before falling back to the fuzzy team-abbreviation-plus-kickoff-time join TheRundown-sourced odds still need (a genuinely different ID space, per TDD §6.2's original design) — real Brasileirão odds matched **111 real rows across all 10 fixtures** on the first run with zero fuzzy-match misses, versus needing name-based matching for every European-league fixture.
- Verified fully live end-to-end, including in the actual mobile app (not just the API): after ingesting real odds and running predictions for the remaining Brasileirão fixtures, `GET /picks?min_odds=1.5&sport_slug=football` returned real picks with real expected-value math (e.g. Internacional vs Flamengo: away @ 1.97, 57.3% model probability, EV +0.13) — confirmed visually in the Picks tab (Expo web, headless-screenshotted) showing 6 real football picks sorted by probability, exactly matching the API response.

## Tennis (ATP) — real, trained, live-verified end to end; WTA still blocked on tier

Added per direct user request, using BallDontLie's tennis API (the same account/key already
used for NBA). Live research against the real OpenAPI specs
(`https://www.balldontlie.io/openapi/atp.yml`/`.../wta.yml`), later corroborated by BallDontLie's
official PDF API documentation (kept locally at the repo root as
`balldontlie_api_documentation.pdf` but deliberately NOT committed — a 4.5MB binary of
third-party docs freely available online isn't worth permanent git history in a public repo;
every fact attributed to it below was also independently confirmed live against the real API),
found a hard constraint that shapes the whole feature: **`/matches` requires the ALL-STAR plan tier;
`/odds`, `/head_to_head`, `/match_stats` require GOAT tier; only `/players`, `/tournaments`,
`/rankings` are on the Free plan.** The user confirmed their real subscription is **ALL-STAR
for ATP only** — WTA still genuinely 401s on `/matches` until that tour is separately
subscribed (a real, live-confirmed 401, isolated by the per-league exception handling in
`ingest_fixtures.py`/`ingest_live_scores.py` so it can never block ATP or any other sport).
Confirm/upgrade WTA's own subscription before trusting any of that tour end-to-end — ATP
itself is now fully real and verified (see below).

**Scope decisions, explicit and user-confirmed**: build both ATP and WTA together from the
start (not a phased ATP-then-WTA rollout — WTA's code is real and unit-tested, just blocked
live on tier), and ship predictions before odds — tennis odds are an explicit fast-follow, not
v1 scope (mirrors NBA/football's own graceful probability-only-pick degradation when no real
odds exist yet). A later direct request added two more real features on top of the original
plan — **H2H on the specific surface, and each player's streak on that surface separately,
alongside their overall H2H/streak** — see `FEATURE_NAMES` below (14, not the original 11).

**A real, serious target-leakage bug was found and fixed only after the first real training
run** (impossible to catch before ALL-STAR access, since it only shows up in genuinely
completed match data): BallDontLie's `/matches` response ~~always~~ **usually** lists the
eventual **WINNER** as `player1` for a completed match — confirmed live, 20/20 in a sampled
batch of real 2022 matches — while a genuinely scheduled (not-yet-played) match shows no such
pattern (the winner isn't known yet, confirmed live against real 2026 upcoming matches).

> **"Always" is too strong, and the difference matters — measured 2026-08-10.** The ordering
> is a property of SETTLED HISTORICAL data on the LIST endpoint, not of completed matches in
> general:
>
>     LIST /matches?season=2022    60/60 = 100%
>     LIST /matches?season=2025    59/59 = 100%
>     LIST /matches?season=2026    41/60 =  68%     <- current season
>     GET  /matches/{id}           12/25 =  48%     <- coin flip
>
> Both facts stand. The leakage was real and the `_home_away_players` id-based tiebreak below
> is still required, because training reads settled seasons where the ordering is ~100%
> informative. But the rule **cannot be inverted to recover a winner** — at 48% on the
> single-match endpoint it carries no information, which is exactly the case where a winner is
> wanted (a retirement with tied completed sets). That is why
> `ingest_fixtures.SPORTS_WITHOUT_DRAWS` refuses to settle a tied tennis score rather than
> assigning a winner from `player1`. The original design
(inherited from the plan's own, pre-ALL-STAR assumption that `player1`/`player2` was a neutral
positional label) trusted that ordering directly as home/away — which baked the outcome
straight into the label. First real training run: train (2021-2023) and validation (2024)
splits came back **100% "home won"**, test (2025) "only" 93.6% — XGBoost couldn't even fit
(`ValueError: Invalid classes inferred... Expected: [0], got [1]`), since a single-class `y`
can't be fit at all. Fixed with `app/adapters/balldontlie_tennis.py:_home_away_players` — a
stable, outcome-independent tiebreak (lower external player id = home) applied identically
whether a match is scheduled or completed, used by **both** the live adapter's
`_map_match_to_fixture_payload` (so a fixture's home/away identity can never flip between an
early scheduled-ingest and a later completed one, since the provider's own player1/player2
slots may get reordered post-hoc but the id-based rule never does) and
`ml/training/collect_tennis_data.py`'s `collect_game_log` (train/serve parity, same principle
as every other sport in this codebase). Regression-tested in
`test_balldontlie_tennis_adapter.py` (`test_home_away_players_is_id_based_not_player1_player2_position`,
`test_map_match_to_fixture_payload_home_away_survives_player1_player2_swap`) — a match where
the winner is deliberately placed in the player1 slot must still resolve home/away (and the
correctly-paired sets-won score) by id, not by position. After the fix, real label
distribution is sane: 60-64% "home won" across all three season splits (train/val/test each
have both classes present) — the residual skew above 50% is a real, understood, and accepted
artifact of the id-based tiebreak (BallDontLie's player ids likely correlate loosely with
account-creation order, not match outcome), not a leak.

**Real ranking-points collection was rewritten around a genuine efficiency finding in the
official PDF, after the first attempt crashed a ~7-hour run at 11,000/17,008 lookups with zero
progress saved** (an `httpx.ReadTimeout`, only retried-on-429 before this): the PDF documents
`/rankings`' `player_ids` query param as an `array` type, combinable with `date` — confirmed
live that a single call with several `player_ids[]` values plus one `date` returns real,
distinct per-player rows in one round trip. `ml/training/collect_tennis_data.py:collect_rank_points`
now batches by ISO week (153 distinct weeks replace 17,008 individual (player, week) calls,
chunked to `RANK_BATCH_SIZE=100` per call) and paces every real request proactively
(`RANK_REQUEST_DELAY_SECONDS=1.1`, mirroring `collect_football_data.py`'s own
`ODDS_REQUEST_DELAY_SECONDS` precedent) — the original script fired one request per
(player, week) back-to-back with no proactive pacing, which on ALL-STAR's documented 60
req/min limit exhausts the whole per-minute budget in under a second, then waits out an entire
~50s cooldown per subsequent request, repeating forever; a stale, still-running duplicate copy
of the same crashed script was separately found and killed mid-diagnosis, itself eating the
rate budget (the same "duplicate background process" footgun already documented elsewhere in
this project). The rewritten collection completed all 17,008 real lookups in about 7 minutes
(97.9% real coverage, 2.1% null for players genuinely unranked/outside the tracked field at
that date — never fabricated as 0), replacing a run that hadn't finished in ~7 hours.

**Real training result, honestly reported**: `ml/training/train_tennis.py` (temporal split
2021-2023 train / 2024 val / 2025 test, 17,741 real examples, Optuna 50 trials, isotonic
calibration) — **test accuracy 63.86% vs. a 64.11% "always pick home" baseline**. Unlike
NBA/football, that baseline isn't a meaningful comparison here — "home" is just the
id-based tiebreak above, not a real signal (tennis has no home-court advantage), so beating or
losing to it doesn't say much either way. A more meaningful sanity check — "always pick the
higher-ranked player" by real rank points — scores **62.22%** on the same test set, so the
model does show a real, modest edge over that baseline (63.86% vs 62.22%), though a small one
on a genuinely new model with a modest feature set. Registered as `tennis_xgb_v20260801195314`
(`is_active=True`), RPS/Brier logged to MLflow under the `tennis_win_probability` experiment.
No historical odds were collected (predictions ship first, odds are the explicit fast-follow —
see below), so `roi_simulation` is `None` for this model, an honest gap, not a bug.

**Verified live end-to-end, not just unit-tested**: `_ingest_fixtures_for_league` pulled real
ATP fixtures (5 real tournaments overlapping the ingest window, confirmed correctly scoped —
see the tournament-window note below), and every real scheduled fixture in the next 7 days
(19 total) got a real `Prediction` row from the newly-registered model — e.g. Taylor Fritz
(home) 71.6% vs Brandon Nakashima 28.4%, HIGH confidence, a sensible result given Fritz is the
more established, higher-ranked player. **The Celery-queued auto-prediction path
(`ingest_fixtures.py`'s "queue `run_predictions` for any fixture with no prediction yet"
mechanism, see the NBA/football section above) did not drain during this verification** — a
pre-existing worker/queue health issue unrelated to tennis's own code (the already-running
Celery worker process from earlier in this session didn't pick up the newly-queued tasks);
predictions were generated by calling `_run_predictions` directly instead, which exercises the
exact same feature-assembly/model/DB-write path Celery would have run. Worth checking the
Celery worker's actual health before assuming this reproduces for other sports too — flagged,
not root-caused further here (out of scope for the tennis feature itself).

**A separate, real oddity noticed while ingesting**: the dev DB now also holds several
thousand real ATP fixture rows spanning 2007-2025, well outside the ±7-day ingest window
`_ingest_fixtures_for_league` actually requests — confirmed this is NOT a bug in the current
adapter (a direct, isolated test of `fetch_fixtures`'s tournament-window overlap logic against
the live API returned exactly 5 correctly-scoped current tournaments, no historical leakage)
— it's leftover data from this session's own earlier live-verification/testing steps before
ALL-STAR access was confirmed. Harmless (completed fixtures are excluded from the
prediction-queueing loop regardless of age) but not cleaned up — flagged rather than silently
left undocumented.

**A tennis player is a `Team` row (a "team" of one)** — `Team`/`TeamStats`/`TeamFeatures` had
no roster/multi-player assumption anywhere, confirmed by reading the actual models, so this
needed no renaming or new table. `TeamKeyPlayer`/`PlayerInjuryStatus` (the Big3/Top5
availability feature) are simply never populated for tennis — every read path already
degrades to `(None, None)` gracefully for a team/season Stage 1 never ran for.

**`app/adapters/balldontlie_tennis.py`** (new) — `BallDontLieTennisAdapter`, one class
covering both tours via `_TOUR_PREFIXES = {"atp": "/atp/v1", "wta": "/wta/v1"}`, selected
through the `league` parameter `fetch_fixtures`/`fetch_team_stats` already carry.
- **Tour-prefixed external IDs (`f"atp:{id}"`/`f"wta:{id}"`) are load-bearing, not
  cosmetic**: both `get_or_create_team` and `Fixture`'s own unique constraint
  (`uq_fixtures_sport_external_id`) key only on `(sport_id, external_id)`, not `league_id` —
  confirmed by reading both directly. Since ATP and WTA share one `sport_id="tennis"` with
  independently-sequenced provider IDs, an unprefixed id=142 in both tours would silently
  collide into one `Team`/`Fixture` row. Caught during planning via a dedicated Plan-agent
  review, not discovered live (can't be, without ALL-STAR access yet).
- **`fetch_fixtures` does NOT assume `/matches` supports a date-range filter** (confirmed
  live: `/matches?start_date=X&end_date=Y` is silently ignored, and tennis matches are
  tournament-scoped, unlike NBA's `/games`) — it lists `/tournaments` (Free tier, real
  `start_date`/`end_date`, confirmed live that an unfiltered call already returns only the
  current season's ~64 real tournaments rather than full history) overlapping the ingest
  window, then fetches `/matches?tournament_ids[]=X` per tournament. **The plural
  `tournament_ids[]` is load-bearing** — the singular `tournament_id` (matching every other
  single-resource filter convention in this API, and this provider's own OpenAPI spec) is
  silently IGNORED, confirmed live with a nonsense id returning the same unfiltered results as
  any real one; only the array form actually filters. Independently confirmed via the official
  PDF's own query-parameter table, which lists only the plural form.
- **`Outcome.home_score`/`away_score` = sets won** (e.g. 2–0, 3–1), not games or points — this
  is what `_maybe_settle_outcome`'s win/loss derivation and the free, already-wired
  `Team.elo_rating` auto-update key off (`apply_match_result` only reads the sign of the
  difference, so this is a real, working signal with zero extra ingestion work, not used in
  v1's feature set though — see below).
- **`_map_status`**: `finished`/`walkover`/`retired`/`defaulted` → `completed` (a real result
  exists in every case), `in_progress` → `live`, else `scheduled` — no `FixtureStatus.
  POSTPONED` mapping needed, simpler than football's equivalent. Flagged as a real,
  live-data-dependent judgment call in the module's own docstring: football's own `_map_status`
  buckets its "WO" (walkover/awarded) into `POSTPONED` instead, on the reasoning that a
  forfeited game has no real market to show — whether tennis's zero-play `walkover` should
  follow that precedent instead is worth revisiting once real `match_status` proportions are
  visible.
- **`scheduled_time` is the real, live-confirmed per-match kickoff field** (`_match_date`/
  `_match_kickoff_utc` fall back to the tournament's own `start_date` only in the rare case
  it's genuinely absent).
- **H2H is derived manually from a player's own match history** (`fetch_h2h_stats`, mirrors
  `balldontlie.py:fetch_h2h_win_rate`'s existing NBA precedent), not the GOAT-gated
  `/head_to_head` endpoint — keeps H2H reachable at ALL-STAR tier alone. Returns both the
  overall H2H win rate AND the H2H win rate restricted to the current match's surface from one
  shared fetch of the two players' match history (no extra API call for the surface cut).
- **`surface_win_rate`/`surface_streak` are fixture-specific** (the *current* tournament's
  surface) and deliberately NOT cached `TeamStats`/`TeamFeatures` columns — they don't fit
  `ingest_fixtures.py`'s per-team-per-run cache. `fetch_match_surface` makes one live call to
  `/matches/{id}` to read the current match's own `tournament.surface`; that surface is then
  threaded into both `fetch_h2h_stats` and `fetch_surface_stats` so it's fetched once and
  shared, not re-fetched per feature — mirrors NBA's `h2h_win_rate_home` being a live call
  rather than a cached column.
- **Real per-season history requires looping an explicit `season` param** —
  `/matches?player_ids[]=X` with no `season` returns only a thin ~3.5-month recent window
  (confirmed live), not full career history; `_fetch_matches_across_seasons` loops
  `TENNIS_SEASONS_BACK=3` seasons explicitly for every form/streak/H2H/surface computation.
- **No goals-scoring or home-court concept exists for tennis** — `attack_str`/`defence_str`/
  `xg_for_5`/`xg_against_5`/`home_win_rate`/`away_win_rate`/`season_point_diff` all stay
  `None` (never fabricated). `elo_rating` also stays `None` on this adapter's own `TeamStats`
  — `Team.elo_rating` (the real, persistent value) is populated separately and generically by
  `ingest_fixtures.py:_maybe_settle_outcome` for every sport already.
- **`fetch_injuries` returns `[]`** — no tennis injury feed at MVP, same as every
  non-NBA/football sport today.

**`app/models_ml/tennis_features.py`** (new) — 14 features: `rank_diff`,
`form_win_rate_home/away`, `days_since_last_match_home/away`, `win_streak_home/away`,
`h2h_win_rate_home`, `h2h_win_rate_surface_home`, `surface_win_rate_home/away`,
`surface_streak_home/away`, `moneyline_implied_prob_home` (always `None` for v1). The surface
H2H/streak features (added per a direct follow-up request, on top of the original 11) capture
a real tennis-domain signal distinct from their overall counterparts — a player's H2H edge or
current form can look meaningfully different on one surface than across their career as a
whole. **No `home_court_indicator`** — a real NBA signal (genuine home-court advantage) with
no tennis analog, omitted rather than inherited as a fabricated constant ("home" here is only
ever the `_home_away_players` id-based tiebreak, a label-stability device, never a real
signal — see the leakage-bug writeup above). **`rank_diff` instead of Elo as the primary
strength signal** — real, provider-computed ranking points from `/rankings`, simpler and more
honest than approximating Elo the way football did (from the user's own notebook); needed one
new nullable column, `TeamFeatures.rank_points` (Alembic migration `e4f6a2c8b1d9`), since the
existing `elo_rating` column is semantically Elo and shouldn't be overloaded.
`assemble_from_game_log`/`assemble_from_live_db` mirror `nba_features.py`'s train/serve-parity
contract and leakage guard (`GAME_DATE < as_of_date`) exactly.

**`app/models_ml/tennis.py`** (new) — `TennisModel`, a direct copy of `nba.py`'s shape (single
XGBoost `binary:logistic` + isotonic calibration, no draw) — not football's two-layer Poisson
stack, since tennis is also a 2-outcome, no-draw sport. Registered in
`app/models_ml/runner.py`'s `_MODEL_CLASSES` and `app/workers/run_predictions.py`'s
`_assemble_features` dispatch. `app/adapters/factory.py`'s `_STATS_ADAPTERS["tennis"]` is
registered; `_ODDS_ADAPTERS`/`_INJURY_ADAPTERS` are deliberately left unset for tennis
(defaults to `[TheRundownAdapter]`, which raises a per-adapter-isolated `ValueError` until
real tennis coverage is confirmed and mapped — zero new code needed for the
predictions-first/odds-fast-follow decision). `scripts/seed_sports.py:seed_tennis()` seeds one
`Sport(slug="tennis")` + two `League` rows (`atp`, `wta`, `country=None` — confirmed
`League.country` is nullable and `League.tier` is never read anywhere in the backend, so a
tour that isn't country-scoped is a clean fit).

**Real historical data collection** (`ml/training/collect_tennis_data.py`, ATP only —
re-runnable unchanged for WTA once that tour's subscription is confirmed, nothing else about
the shape is ATP-specific): pulls each season directly via `/matches?season=X` (confirmed
live, working back to at least 2021, correctly scoped and non-duplicated — no `/tournaments`
call needed at all here, since each match embeds its own `tournament` object with surface).
Real result: **35,482 game-log rows** (one per player per completed match, 2021-2025) and
**17,008 real rank-point lookups** (batched/paced per the efficiency finding above). Confirmed
BallDontLie has no free/unthrottled equivalent to NBA's separate `nba_api` — bulk history
comes from the same rate-limited, tier-gated API used for live serving, which is exactly why
the rank-points batching/pacing rework mattered.

**218 backend tests passing before this round of fixes, 235 after** (the leakage-bug
regression tests above plus the existing pure mapping/aggregation and feature-assembly
leakage-guard tests), `ruff`/`black` clean. `ml/training/train_tennis.py` is real and run;
`GET /fixtures`/`GET /fixtures/{id}` serve real ATP predictions today. **Deliberately not
extended in this pass**: WTA data collection/training (blocked on that tour's own
subscription — the code is generic enough to just point `TOUR`/`league` at `"wta"` once it
is), and tennis odds (explicit fast-follow, not v1 scope).

### Tennis retirements + tournament grouping (both from real user-reported bugs)

**A real, triple bug in set counting, reported from a screenshot** ("The game shown actually
ended 1:0 with the away player retiring. So, where did you get the 1:1 from"): `_sets_won`
counted **any** set where a player happened to be ahead, including a set abandoned mid-play
when a player retires. Confirmed against real API data for Popyrin vs Kokkinakis (`set_scores`
= 6-4, then 2-3 when Kokkinakis retired): the second, never-completed set was credited to
Kokkinakis, producing **1-1** — an impossible tennis scoreline, since tennis has no draws.
Three consequences, not one:
1. Wrong score displayed (1-1 instead of the real 1-0).
2. **Inverted verdict** — Popyrin genuinely WON, but a stored 1-1 made
   `evaluatePickCorrectness` (`home_score > away_score`) mark a **correct** prediction as a
   red ✗ failure.
3. `_maybe_settle_outcome` settled a `MatchResult.DRAW`, which can never happen in tennis.

Fixed with `_is_completed_set` (6+ games with a 2-game margin, 7-5, or a 7-6 tiebreak — also
covers long deciding sets like 70-68); `_sets_won` now skips any set that isn't genuinely won.

**The load-bearing discovery that shaped the fix**: this real retirement came back as
`match_status: "finished"` — BallDontLie exposes **no retirement marker at all** in that
field, so the adapter's original assumption that `_COMPLETED_MATCH_STATUSES`' "retired"/
"walkover"/"defaulted" values would identify these is wrong against real data.
`_match_result_type` therefore infers it **structurally** from the score: an incomplete set, or
a winner who never reached `MIN_SETS_TO_WIN_A_MATCH` (2). The provider's explicit status is
still honoured when present, since a walkover with zero sets played can only be identified
that way.

**A retirement is deliberately shown as VOID, not as a win or a loss** — per the user's own
framing ("not a failed prediction"). `fixture_live_state.result_type` (Alembic migration
`a1d5c3e7b904`, NULL for a normal result) drives a neutral grey "VOID · <pick>" badge plus a
"Retired"/"Walkover" label in `FixtureCard.tsx`, and suppresses the ✓/✗ entirely. Chosen over
crediting the model with the win it genuinely earned here, because **most bookmakers void bets
on a retirement** — a green tick would imply a payout the user may never have received.
Deliberately a column rather than a new `FixtureStatus` value: the match really IS completed
with a real winner, so every existing `status == COMPLETED` path (settlement, retrodiction,
feed filtering) keeps working untouched; only the *presentation* of the result changes.

**"Impossible draw" turned out to be too strong a framing, corrected mid-repair**: a
retirement can legitimately leave equal completed sets (real example: `6-1, 6-7, 0-2 ret.` —
1-1 in completed sets, but Cruz Hewitt is the real winner). `scripts/repair_tennis_retirement_scores.py`
(one-off, re-fetches from the real API and recomputes through the fixed code path, never
patching values by hand) correctly *skips* those rather than forcing a fabricated winner —
they're already handled by `result_type`, which is what actually suppresses the bogus verdict.
~~**Known, currently-inert gap**: ~20 tennis `Outcome` rows still carry `MatchResult.DRAW`~~ —
**fixed 2026-08-10.** `_maybe_settle_outcome` derived the result from the score alone (correct
for football, wrong for a tennis retirement) and fell through to `MatchResult.DRAW` on a tie.
`ingest_fixtures.SPORTS_WITHOUT_DRAWS = {"tennis", "nba"}` now settles **nothing** for a tied
score in those sports — no `Outcome` and no draw-shaped Elo update, since both live in that
function. The 12 accumulated rows were deleted via
`scripts/remove_impossible_draw_outcomes.py`.
- **No winner is guessed, because none is available** — see the measured `player1` breakdown
  above. The earlier note here said the winner "is known independently"; it is not, on the
  endpoints that would be used. At 48% that would have been a coin flip.
- **An absent `Outcome` is the honest state**, and it stays settleable if a real winner signal
  appears. It is retried each ingest at no API cost.
- **Deletion only sticks because of the guard.** Not hypothetical: the first removal was undone
  within minutes by the running Celery worker, which still held pre-guard code — the
  stale-worker trap, hit for the second time in one day. Verified after restarting: two real
  scheduled `ingest_live_scores` runs, two guard activations, zero rows recreated.
- Elo is still not recomputed for those fixtures (it would need a full ordered replay) and
  remains genuinely inert, since tennis's feature set uses `rank_diff`, not Elo.

**Tournament grouping with flag + surface** (direct user request: "I want the games separated
by tournament name. Inline with the tournament name, add country flag and surface... This will
guide users to the right sections in their betting apps"). A whole tour is ONE `League` row, so
the feed previously rendered an undifferentiated wall of matches under "ATP Tour".
`fixtures.tournament_name`/`.tournament_surface`/`.tournament_location` (same migration) are
populated from the tournament object **already embedded in every match response** — zero extra
API calls. `groupByLeague` in `(tabs)/index.tsx` now keys on tournament when present (namespaced
`tournament:`/`league:` so the two can never collide), falling back to league grouping for a
fixture ingested before these columns existed rather than dropping it.
- **The flag needed a hand-built map, and this is a real constraint, not a shortcut**:
  BallDontLie's tournament `location` is a **CITY** ("Montreal", "Indian Wells") and there is
  **no country field at all**, so `countryForTournamentLocation` in `lib/countryFlags.tsx` maps
  city → country client-side. Every key was taken from the real `/tournaments` response (all 60
  distinct locations for the current season), not guessed; 21 additional flag PNGs were added
  (an ATP season visits ~30 countries vs football's handful). An unmapped city falls through to
  the existing 🌍 globe rather than showing a wrong flag — the safe direction to fail.
  "Multiple Locations" (Davis Cup) is deliberately unmapped for that reason.
- Verified live via the real API: 75 matches under "National Bank Open presented by Rogers |
  Hard | Montreal", 59 under "Mubadala Citi DC Open | Hard | Washington".

**A third instance of the stale-worker trap, worth internalising**: the corrected scores kept
reverting to 1-1 mid-verification. Cause: the Celery worker had been running since *before* the
fix (worker started 08:17:55, fix written 09:38:38), so its in-memory copy of `_sets_won` was
the buggy one and `ingest_live_scores`' 5-minute schedule re-wrote the bad score every cycle.
The local `uvicorn` was even staler (two days). **Neither Celery nor a plain `uvicorn` picks up
code changes without a restart**, and the symptom is never an error — it looks like "my fix
didn't take". Restart both after any worker/adapter change before concluding anything from live
data.

## Measuring whether the predictions work

Spec: `docs/history-metrics-spec.md`, written **before** the endpoint was touched so its shape
could not be chosen after seeing which cut of the data looked best.

**`predictions.kind`** (`pre_match` | `retrodiction` | `unknown`) records provenance at write
time. Both paths wrote to one table with nothing to tell them apart, so `/history/summary`
averaged them and reported one accuracy. Measured: mixing moved football **56.1% → 53.3%** and
tennis **63.6% → 61.7%** — *downward*, the opposite of the expected direction, because
retrodictions score WORSE (`assemble_from_game_log` carries a leakage guard and thinner
features). The fault was never hindsight flattering the model; it was averaging two populations
with different feature quality and reporting neither.
- **A timestamp cannot substitute for the column.** `created_at < kickoff` breaks on
  regeneration — 91 football predictions were regenerated on 2026-08-10, resetting `created_at`
  to after those kickoffs. So the historical backfill is **one-directional**: before-kickoff
  proves a forecast, the reverse proves nothing, and those rows stay `UNKNOWN` rather than being
  guessed. `UNKNOWN` is also the column default, so a future write path that forgets to set it
  is excluded from skill measurement rather than silently claiming to be a forecast.

**`pick_snapshots`** records the pick as it was SHOWN, because `best_pick` is computed per
request and was never stored — grading it later would mean recomputing against today's odds and
today's guards, i.e. a different product than users saw. **Nothing before 2026-08-10 is
recoverable.** Captured **4-8 hours out**, and that window is load-bearing: snapshotting next to
`capture_closing_odds` (T-10..45min) would make CLV meaningless *by construction*, since
`taken/closing - 1` is ~0 when both prices are minutes apart. Delegates to `_bulk_best_picks`,
the feed's own selection, with a test asserting the delegation.

**Every accuracy carries `n`, a 95% Wilson interval and its minimum detectable effect.** Live:
football n=139, 0.561, CI [0.478, 0.641], detectable 0.105; tennis n=77, 0.636, CI
[0.525, 0.735], detectable 0.136. **Both are underpowered for the edge we believe in** — 4.1pp
football against a 10.5pp detectable effect, needing ~891 graded picks against 139; tennis needs
~5,406 against 77. An early percentage is noise in either direction, which is exactly why the
fields sit beside it.

### What the first measurements found

- **CLV is negative, but less so than the naive number.** Proxy over pre-match picks: favoured
  side **-3.47%**, control (the side NOT favoured) **+0.83%**, a gap of **-4.30pp**. The control
  matters — football's control is also negative (-2.40%), so a real part of the raw -3.61% is
  systematic price drift rather than anything about the model. **The control is not clean**
  either: for a 3-way market it is the less-likely of home/away, usually the underdog, whose
  prices are longer and more volatile. Directionally this is still evidence against an edge; it
  is not a measurement to quote precisely.
- **The confidence badge is miscalibrated, and possibly inverted.** HIGH claims 74.1% and
  delivers **60.9%** (n=69); MEDIUM claims 57.8% and delivers **68.5%** (n=89). So MEDIUM
  outperformed HIGH. The intervals overlap, so the ordering is not established — but the
  calibration gap is stark, and CLAUDE.md has always said these thresholds were "provisional
  guesses". They are now measured, and wrong.
- **The completeness floor works, and 0.25 may be too low.** Below 0.25: 33.3% (n=9). 0.25-0.35:
  **37.5%** (n=16). Above 0.35: **69.9%** (n=123). The real break is at 0.35, not 0.25 — where
  mobile's own `LOW_CONFIDENCE_COMPLETENESS` already sits. The 0.25 choice was defensible on the
  evidence used at the time (extremeness: 85% of picks below 0.25 were >=0.90 versus 6% above),
  but *correctness* evidence points higher. Small n, wide intervals — a candidate to revisit,
  not a settled answer.

## Pick ranking, odds reliability, and prediction-quality measurement

A block of work driven by two user observations that turned out to share one root cause: "a
chunk of the MLS predictions were all under 3.5 ... is that not too confident", and "I never
see a prediction favouring an away team winning or drawing".

**`_pick_best` now ranks by EXPECTED VALUE, not raw probability** (`app/fixtures/router.py`).
The old rule returned the highest-probability candidate, which answers "what is most likely to
be true" rather than "what is worth backing" — very different questions. Under 3.5 goals is
intrinsically an ~80% event, so it beat every 1X2 (~50%) and double-chance (~70%) candidate
almost every time. Measured before the fix: 13 of 14 MLS cards showed the same pick, and on the
15 fixtures where the model genuinely favoured the away side, X2 surfaced 9/9 times in
Brasileirão but **0/6** in MLS/CSL.
- **`MAX_EDGE_OVER_MARKET = 0.15`** rejects any pick whose probability exceeds the bookmaker's
  implied probability by more than 15 points. EV ranking *alone* would still reward an
  overconfident probability, so the two work together: EV decides ordering, the guard decides
  trustworthiness. Calibrated against the real case — ~85% claimed at 1.60 (~62% implied) that
  measured out at ~70%. Deliberately not vig-adjusted (that needs every outcome's price, which
  isn't always ingested); the un-adjusted figure errs toward *keeping* a pick.
- **The probability floor is applied BEFORE ranking, not to the winner afterwards.** Ranking
  globally by EV and then testing the floor silently dropped whole fixtures, because the
  highest-EV candidate is often a high-odds/low-probability one — a real case from the test
  suite has corners OVER at 28% priced 3.50 beating corners UNDER at 72% priced 1.30. Filtering
  first answers what the user actually asked: the best value *among picks at least this likely*.
- Candidates with no odds can be neither valued nor guarded, so they still rank by probability
  — the only option for a sport with no odds coverage (tennis).
- Live result: the best-pick mix went from 41/51 `goals_total/under` to a genuine spread
  (29 over / 12 under / 7 1X / 2 corners), max displayed probability 96-100% → **90.9%**, and
  **0 of 150** Over/Under probabilities now exceed 95%.

**A two-day odds outage, found only while verifying the above** — and the reason the EV work
would otherwise have changed nothing. Odds ingestion had written nothing since 2026-07-31,
leaving **6 of 51** upcoming football fixtures with any odds at all.
`TheRundownAdapter.fetch_odds` had **no retry and no pacing**, while `ingest_odds` fans out
(`days_ahead + 1`) requests per league across ~7 leagues every 5 minutes; a single 429 raised
straight out of `fetch_odds` and killed the whole task, every cycle, indefinitely. The knock-on
effect is the important part: **with no odds, EV ranking and the `min_odds` filter both silently
degrade to probability-only behaviour** — i.e. the exact "every pick is UNDER 3.5" symptom that
looked like a modelling problem. Fixed with retry-on-429 honouring `Retry-After`,
`ODDS_REQUEST_DELAY_SECONDS = 2.0` proactive pacing (CLAUDE.md already documented that this
provider's 429s escalate into spurious 401s under burst load, so retrying alone isn't enough),
and per-adapter `httpx.HTTPError` isolation in `ingest_odds.py` so one provider's rate limit
can't take down every other league — the same isolation already applied to
`ingest_fixtures.py`/`ingest_live_scores.py`.

**Over/Under goals now has held-out evaluation, every training run** (`train_football.py`) —
the honest root cause of it shipping visibly overconfident. 1X2 has always reported
accuracy/RPS, but nobody had ever measured whether a stated "85% chance of under 3.5" was
right; the market shipped unvalidated. Each run now reports, per line, the Brier score plus a
reliability table of predicted-probability bucket vs. the frequency the event actually
occurred, logged to MLflow. Per line rather than aggregated (1.5/2.5/3.5 have very different
base rates, and one number would hide a bad one); buckets under 20 samples are suppressed as
noise rather than printed as if meaningful.

**The out-of-distribution diagnosis was right, but the mechanism was wrong** — worth recording
because the initial explanation was stated confidently and was wrong. The theory was "MLS
scores more than the training data, so the model under-predicts goals". Collecting the real
history refuted it: **MLS averages 2.930 goals/match, essentially identical to EPL's 2.927**.
The actual cause is that **Brasileirão is a low-scoring outlier at 2.411** (P(under 3.5) 0.789
vs EPL's 0.658), so pooling only EPL+Brasileirão biased the model toward P(under 3.5) ≈ 0.79
when MLS/CSL truly sit at ~0.66. Same conclusion (add the missing leagues), different reason.
- `LEAGUES` in `train_football.py` is now EPL + Brasileirão + MLS + CSL + Scottish Premiership;
  `collect_football_data.py` gained matching `LEAGUE_CONFIGS` entries.
- **Collection is staged (`--leagues` / `--stages`) because it genuinely cannot run in one
  sitting**: a game log costs ~1 call per league-season (~20 calls total for three leagues,
  and it's the data that actually fixes the goal distribution), while lineups and corners cost
  1 call PER FIXTURE — ~9,600 for three leagues, past API-Football's 7,500/day ceiling.
- Training tolerates a league having a game log but no lineups/corners (`_load_optional`): its
  key-player features come through as `None`, which XGBoost's own missing-value handling
  covers. Strictly better than excluding the league entirely.
- Retrained result, reported straight: Over/Under mean gaps are now **-0.003 / -0.003 / -0.009**
  for under 1.5/2.5/3.5, against **+0.119 (MLS)** and **+0.083 (CSL)** measured live before.
  1X2 47.46% vs 45.60% baseline, RPS 0.2179 — marginally below the prior 47.89%/0.2138 but on a
  materially harder 5-league test set, so not directly comparable. Flat-stake ROI -0.26%
  (n=48), up from -11.5% but still no demonstrated edge. Registered as
  `football_xgb_v20260802100216`.
- **The reliability buckets expose what the mean hides, and it matters more than the
  calibration fix**: for under 3.5, the 0.5-0.6, 0.6-0.7 and 0.7-0.8 buckets all come out at
  ~0.675 — simply the base rate. The model is no longer overconfident, but it is also **barely
  discriminating** between high- and low-scoring fixtures on this market. This is why further
  isotonic calibration of Over/Under was **deliberately not done**: with the mean gap already
  ~0, calibration would mostly flatten those buckets toward the base rate, making the numbers
  more honest while making the picks *less* useful. The real constraint is signal, not
  calibration — that points at better features or a distribution with a dispersion parameter
  (Negative Binomial / Dixon-Coles), not at another calibration layer.

**`predictions.feature_completeness`** (Alembic migration `c4f8a2b6e1d3`) records the fraction
of the model's own feature vector that had a real value at inference time, via a shared
`app/predictions/service.py:feature_completeness` helper used by all three prediction paths
(live inference plus both retrodiction workers). A prediction built from a mostly-empty vector
isn't wrong, but it carries far less information — and the feed rendered both with identical
authority. The motivating case: 26% of retrodicted ATP fixtures came out at exactly 0.562
because those players' prior-match history was largely absent. Nullable with **no backfill** —
older predictions genuinely have no measurement, and inventing one retroactively defeats the
point. Measuring it immediately surfaced how thin current inputs are:

    EPL 0.12 | Scottish Prem 0.19 | Brasileirão 0.38 | MLS 0.48 | CSL 0.49

EPL and the Scottish Premiership are between seasons, so their teams have no played matches to
derive form/attack/defence from. `FixtureCard.tsx`'s `LOW_CONFIDENCE_COMPLETENESS = 0.35` is
set from that measured spread rather than by feel — **0.5 would have flagged every single
fixture and told the user nothing**; 0.35 separates "no real data yet" from "partial but
genuine data". Below it the badge dims and reads "limited data", worded as a limitation of the
DATA rather than a hedge on the number.

**Dimming was not enough, and `MIN_FEATURE_COMPLETENESS = 0.25` (`app/fixtures/router.py`) is
the hard version.** Found by opening the running app after the 18-league retrain: Tottenham vs
Newcastle served **1X at 99.7%** — Newcastle to neither win nor draw at 0.3% — from a vector
with **3 of 31 features** populated, because EPL's season had not opened and neither side had
played. Two things made it worse than a single silly number:
- **Those picks RANKED FIRST.** August EPL fixtures have no odds posted yet, and with no odds
  both EV ranking and `MAX_EDGE_OVER_MARKET` degrade to raw probability — so the emptiest
  vectors sorted to the top of the feed. Dimming a badge does not stop that.
- **It pre-dated the retrain.** The same vector through the previous 9-league artefact gives
  93.6%, so pooling worsened an existing defect rather than creating one. Worth stating plainly
  because the timing invited the opposite conclusion.

The floor is **measured, not chosen by feel** — over all 159 football predictions carrying a
completeness value, the share whose best 1X/X2 probability was extreme:

    completeness    n    >=0.90     settled accuracy
    0.00-0.15      26    22 (85%)    0% (n=1)
    0.15-0.25       8     1 (13%)   43% (n=7)
    0.25-0.35      17     1  (6%)   33% (n=3)
    0.35-0.50      44     0  (0%)   38% (n=13)
    0.50+          64    62 (97%)   81% (n=64)

**The 0.50+ row is why this is a completeness floor and NOT a cap on extreme probabilities** —
that band is just as extreme and is 81% correct on a real settled sample. Confident output
built on real data is the product working; a probability cap would blunt exactly the band that
earns its confidence while leaving the empty-vector band untouched. 0.25 is where the cliff
sits (85% extreme below, 6% just above), deliberately **below** mobile's 0.35: dimming a badge
is cheap if over-applied, removing a pick is not, so the hard bar is the stricter one. NULL
passes, for the same no-backfill reason the column itself is nullable. A suppressed fixture
still appears in the schedule with `best_pick: null` — same shape as POSTPONED — rather than
vanishing. Live effect: max served probability 0.997 → 0.934, picks ≥0.95 went 16 → **0**.
Regression-tested in `backend/tests/test_feature_completeness_floor.py`.

**Negative Binomial for Over/Under was investigated and DELIBERATELY NOT BUILT — the premise
was measured and found false.** The stated reasoning for it (twice, confidently) was: "real
football goals are overdispersed, so Poisson's variance-equals-mean assumption gives too-thin
tails and overstates UNDER, and home/away goals are positively correlated so the Poisson-sum
independence assumption compounds it." Measured across all 8,718 real pooled fixtures, both
halves of that are wrong:

    total goals   mean=2.794  var=2.802  var/mean=1.0030   <- Poisson assumes exactly 1.0
    corr(home goals, away goals) = -0.058                  <- slightly NEGATIVE, not positive
    P(under 1.5/2.5/3.5): Poisson-at-the-mean vs empirical differs by <= 0.007

Total goals are essentially **exactly** Poisson-dispersed. The two sides are individually very
mildly overdispersed (1.047, 1.088), but their slight NEGATIVE correlation cancels it, leaving
the total at 1.003. Adding a Negative Binomial dispersion parameter would fit a dispersion that
does not exist — added complexity, no gain, and a real risk of fitting noise. Dixon-Coles-style
correlation adjustment is equally unjustified at r = -0.058.

**The distribution was never the problem; the signal is.** Two independent measurements agree:
the retrained model's own reliability buckets for under 3.5 all land at ~0.675 (the base rate)
across n=1,704 test fixtures, and on completed fixtures with a stored prediction the
correlation between predicted total xG and actual total goals is **+0.030 — about 0.1% of
variance explained** (n=66, so wide error bars, but pointing the same way as the much larger
bucket evidence). Predicted xG spans 0.53-4.13 while actual totals span 0-7 with more than
twice the standard deviation.

So Over/Under goals is currently a base-rate guess wearing the clothes of a prediction. The
honest options are better goal-predictive features, or not surfacing it as a confident pick —
NOT another distributional or calibration layer, both of which would only relabel the same
absent signal.

**Resolved 2026-08-11: goals_total no longer wins the headline pick**
(`app/fixtures/router.py:NO_DEMONSTRATED_SIGNAL_MARKETS`). Re-measured on real settled
fixtures, predicted total against actual total:

    goals_total     n=242   r=+0.049   0.2% of variance explained   <- barred
    corners_total   n=234   r=+0.288   8.3% of variance explained   <- kept

**Corners was assumed to be the same case and is NOT** — the assumption was about to be stated
before it was measured. At n=234, r=+0.288 is roughly 4.4 standard errors from zero: a real if
modest signal, and it keeps its place. Goals confirms the earlier r=+0.030 finding on a much
larger sample (n=242 vs n=66), which is why this was decidable without waiting for the CLV
read — no amount of closing-line data makes a market explaining 0.2% of variance informative.

This is a bar on the MARKET, distinct from `MIN_EDGE_OVER_BASE_RATE` and
`MIN_FEATURE_COMPLETENESS`, which both judge an individual pick. Goals still appears in
`all_market_picks` and the fixture detail's Other Markets, and an explicit `market=goals_total`
request is still honoured — asking for it differs from it winning by default.

Real effect on the live feed: 130 → 123 fixtures keep a headline pick (7 lost it entirely),
and the mix went `goals 30% / h2h 30% / corners 29% / DC 11%` → `corners 50% / h2h 33% /
DC 18%`. **Corners at 50% is worth watching**: single-market concentration is the exact shape
of the original "every card shows UNDER 3.5" complaint, now with a market that has measured
signal rather than none.

**`scripts/purge_tennis_test_pollution.py`** removed 2,729 pre-2021 tennis fixtures left by
exploratory ingest runs during the tennis build-out (6,277 → 3,548). Conservative by design:
only fixtures predating the 2021-2025 training window, and Teams (players) are deliberately
NOT deleted — an orphan player row is harmless, whereas deleting one still referenced by an
in-window fixture is not. Dry-run by default, `--confirm` to execute.

## Score display, history backfill, and real live-score ingestion

Added so completed fixtures show a final score and the Home feed can browse past/future days, not just the next 7 days forward — see `mobile/app/(tabs)/index.tsx`'s day-strip and `mobile/components/fixtures/FixtureCard.tsx`'s score badge.

**`ingest_live_scores.py` is now real** — previously a documented, permanently-unreachable stub (TDD's `DataSourceAdapter` ABC has no method mapping to TheRundown's scores endpoint). Real fix: reuse `fetch_fixtures` (the stats/fixtures adapter — API-Football or BallDontLie) instead of adding a new adapter method at all — the same `/fixtures`-equivalent endpoint `ingest_fixtures.py` already calls carries live goals/status for any fixture in its queried date range. `ingest_live_scores.py` re-queries a `+/-1 day` window around "now" every 5 minutes and updates `Fixture.status`/`FixtureLiveState` for fixtures already in the DB (a fixture it discovers for the first time is left for `ingest_fixtures.py`'s own daily run, not ingested here).

**`FixturePayload` gained `home_score`/`away_score`/`match_minute`** (both real adapters populate them — API-Football from `fixture.goals`/`fixture.status.elapsed`, BallDontLie from `home_team_score`/`visitor_team_score`; BallDontLie's `match_minute` stays `None`, no clock-minute field exists, only a quarter number which is a different unit). `FixtureLiveState` — a table that already had the right shape (`home_score`/`away_score`/`match_minute`/`period`/`status`/`last_updated_utc`) but that nothing had ever written to — is now real via `app/workers/ingest_fixtures.py:_upsert_live_state`, reused for both in-progress AND completed fixtures (a completed fixture's row simply stops updating once the game ends, which is exactly "final score," no separate settlement step needed). `GET /fixtures` (list, not just detail) now bulk-fetches this per page so the Home feed can show a score inline without a per-fixture call.

**`fetch_fixtures` gained an optional `days_back` param** (both real adapters + all stub adapters, for ABC compliance) so `ingest_fixtures.py` can backfill the last 7 days of completed fixtures (`FIXTURE_HISTORY_DAYS`, symmetric with the existing 7-day forward `FEATURE_LOOKAHEAD_DAYS`) — nothing had ever ingested a past fixture before this, only ever forward-looking.

**A real, previously-dormant `outcomes` table now gets written to**: `Outcome`/`MatchResult` (TDD schema) existed but nothing ever inserted a row — `_maybe_settle_outcome` (called from both `ingest_fixtures.py` and `ingest_live_scores.py`, idempotent) writes one the moment a fixture completes with real scores. This is a real, unblocking step for `GET /history`'s own documented blocker ("no settled outcomes exist") — aggregating these into `/history` itself is **done** — see "Measuring whether the predictions work" below.

**A real, previously-latent `TeamFeatures` duplicate-row bug was found and fixed while touching this code**: re-running `ingest_fixtures.py` (daily, per TDD §2.3) for the same not-yet-played fixture inserted a brand-new `TeamFeatures` row every time — there was no dedup at all. Over several days before kickoff this would accumulate multiple rows per `(team_id, fixture_id)`, and `run_predictions.py`'s `.scalar_one_or_none()` lookup would eventually raise `MultipleResultsFound`. Fixed with delete-then-insert per `(team_id, fixture_id)`, the same idiom already used for `team_key_players`; regression-tested in `backend/tests/test_ingest_fixtures.py::test_rerunning_ingest_does_not_duplicate_team_features` via a fake adapter run twice. Completed fixtures are now also excluded from the feature/prediction loop entirely (they don't need pre-game features), avoiding wasted `fetch_team_stats` calls on the newly-backfilled historical games.

**A real, pre-existing status-mapping bug was found via live data on a real matchday, not synthetically**: while backfilling Brasileirão, 4 real fixtures turned out to be genuinely **postponed** (`API-Football fixture.status.short == "PST"`) — `_map_status`'s old blanket "anything else is live" fallback mapped this to `"live"`, which would show a LIVE badge with no score for a match that isn't actually happening. ~~Fixed by mapping `PST`/`CANC`/`ABD`/`SUSP`/`INT`/`TBD`/`AWD`/`WO` to `"scheduled"` instead — the least-misleading bucket available given `fixtures.status` still has no cancelled/postponed value~~ — **superseded, see "Mobile implementation status"'s own POSTPONED-status entry below**: mapping to `"scheduled"` turned out to be actively misleading in its own right (the Picks feed kept showing a live market prediction/odds badge for a game that was never going to be played), so these 8 codes now map to a real, dedicated `FixtureStatus.POSTPONED` instead. This bug predates today's session but had never been exercised until a real postponement actually occurred during testing.

## Retrodicted predictions for completed fixtures (`app/workers/backfill_predictions.py`)

Added so the Home feed can show "what the model would have called" alongside a real final score for past days, not just for upcoming ones — and colour-code it against the real result (`mobile/components/fixtures/FixtureCard.tsx`'s green/red badge, ✓/✗ included so colour is never the only signal).

**Deliberately a different code path from live inference, not a shortcut through it**: `run_predictions.py`'s live path assembles features from `TeamFeatures` — a snapshot of team form *as of ingest time* (now), which for a past game would leak every result that happened *after* it, including its own eventual outcome once enough time passes. Instead, `_retrodict_league` builds a leakage-safe "game log" and feeds it through the *same* `app/models_ml/football_features.py:assemble_from_game_log` function `train_football.py` used to train the model — the historical-training and retrodiction code paths are genuinely the same function, not just the same idea, and its own internal `GAME_DATE < as_of_date` filter is the leakage guard.

**Rebuilt to use real, deep historical data instead of only our own live DB's thin ~7-day window** — the user explicitly asked for this ("predictions for past games should be based on all historic data for the seasons... it's a one-off prediction, we wouldn't have any fear of leak here"), since a genuinely trustworthy-looking past-game prediction matters for user trust/adoption even though it's not staked on anything:
- The game log is now the *same* multi-season parquet cache `train_football.py` trains on (`ml/data/football_game_log_{league}.parquet`, currently EPL and Brasileirão — see "Real historical data" above), with any of our own DB's completed fixtures not already present in that cache appended on top (keeps retrodiction fresh between historical-collection runs without needing to re-run the full collection every time; de-duped by the provider's own fixture id, which is the same ID space in both places since API-Football is the fixtures/stats provider for both our live DB and the training cache). A league with no cached parquet yet (the other 3 European leagues) falls back to DB-only history — thinner, same honest gap as before this rebuild, not an error.
- **`elo_diff` is real now** (previously always `None`) — `app/models_ml/elo.py:compute_elo_history` walks the combined game log once per retrodict run.
- **Key-player availability now uses REAL BOX-SCORE/LINEUP PRESENCE ("who literally played"), not Stage 2's pre-game `player_injury_status`** — the same Big5/Big3 approach from the user's own prior NBA notebook, explicitly authorized for retrodiction specifically since a completed fixture's outcome is already known and this is a one-off backtest-style prediction, never a forecast. Lives in a new, clearly-separated module, `app/models_ml/historical_key_players.py` (shared by `train_football.py` and `backfill_predictions.py` — previously two near-duplicate copies of the same logic) — mirrors the existing `historical_key_player_availability`-vs-`get_key_player_availability` separation already established for NBA, specifically so it can never be imported into the live Stage 2 path by accident. A fixture whose lineup isn't already in the cached parquet (i.e. completed more recently than the last historical-collection run) gets one real, one-off live fetch via the new `app/adapters/api_football.py:fetch_lineup_presence` — a real API call, but only ever for a fixture whose outcome has already happened, the same "retrodiction can afford real per-fixture calls live serving can't" tradeoff as the rest of this rebuild.
- **Moneyline-implied-probability is still always `None`** — no historical odds exist for arbitrary past DB fixtures beyond EPL's bounded training-time sample, which isn't indexed for point lookups by fixture. A real, same-shaped gap as before this rebuild, not fabricated.
- Verified live end-to-end: deleted a real completed fixture's existing `Prediction` row, re-ran `_backfill_predictions()`, confirmed a genuinely different prediction was generated by the newly-retrained model (`football_xgb_v20260729144210`) — proving the new game-log/Elo/lineup-presence pipeline is actually exercised, not just present in code.
- **Known follow-up, not attempted**: existing `Prediction` rows created by the OLD, thinner version of this worker (before this rebuild) are left as-is — there's no schema field distinguishing "a real live pre-game forecast" from "an old thin retrodiction" for an already-completed fixture, so blindly regenerating every existing row risked silently discarding genuine pre-game forecasts along with the ones actually worth upgrading.

Wired automatically: `_retrodict_league` runs at the end of `ingest_fixtures.py`'s own per-league backfill (football only — `assemble_from_game_log`'s shape doesn't fit NBA's), so every newly-completed fixture gets a real retrodicted prediction with no separate schedule needed. `backfill_predictions()` (a Celery task) exists as a standalone manual entry point across every football league at once, not because the per-league path needs it.

Stubbed (signature + schema exist, body is `NotImplementedError`/`501`, pending real API keys): ~~`GET /history`~~ — **no longer stubbed, and it had not been for some time.** It is live, backed by real settled outcomes, and serving `/history`, `/history/summary` and `/stats/model`. This line said otherwise for long enough that it was quoted back as a reason to "build" an endpoint that already existed. **The cost of that drift was worse than a wasted afternoon**: because nobody was looking at it, `/history/summary` spent that time averaging pre-match forecasts together with retrodictions and reporting the result as one accuracy — a live, user-facing number computed over a population nobody had chosen. See "Measuring whether the predictions work" below. `RotoWireAdapter` and `BallDontLieAdapter.fetch_injuries` (live-tested: BallDontLie's `/nba/v1/player_injuries` 401s on this key's plan, same paid-tier gate as `/season_averages`/`/standings`; no `ROTOWIRE_API_KEY` was ever provisioned), `TheRundownAdapter.fetch_fixtures`/`.fetch_team_stats`, and `SportsDataIOAdapter` entirely. `ingest_injuries.py`'s NBA path (re-inference trigger and DB-write logic) is fully real and tested — only the underlying HTTP calls into RotoWire/BallDontLie are stubbed, so NBA's `player_injury_status` stays empty in practice until one of those exists. Football's injury path is real end-to-end since API-Football's `/injuries` is live. `FootballModel` is a real, trained model, and football's odds now come from both TheRundown and API-Football (per-league, see above) — see "Football model + key-player availability" above.

**Known divergences from the TDD, introduced deliberately while building — check these before assuming the docs are authoritative:**
- `refresh_tokens` table and `users.expo_push_token` column exist in code but aren't in the TDD §2.1 schema listing (the TDD's own prose requires both — §4.3, §5.4).
- `fixtures.external_id` (indexed, unique with `sport_id`) exists in code but isn't in the TDD §2.1 schema listing either. Without it, ingest workers have no way to dedupe against a provider's own fixture ID — matching against the internal UUID PK (what the code did before this was added) can never hit, so every ingest run would insert duplicates forever.
- `FixturePayload` (`app/adapters/base.py`) carries `home_team_name`/`away_team_name`/`*_short_name` and a `status` string beyond the original TDD-derived shape — a first-seen team needs a display name to create its `Team` row, and a fixture ingested from a non-"upcoming" window (see BallDontLie note below) needs to say it's already `completed` rather than defaulting to `scheduled` forever.
- `app/fixtures/service.py:get_or_create_team` — a get-or-create-by-`(sport_id, external_id)` helper. Needed for the same reason as `fixtures.external_id`: fixture payloads carry provider IDs, not our internal UUIDs, and nothing else resolves that mapping.
- `AdapterFactory`/`ingest_fixtures.py` resolve a provider by `sport.slug` (football/nba/nfl/nhl/mlb), not by a `sports.data_source_slug` column — TDD §6.2 references that column but §2.1's schema doesn't define it.
- ~~`fixtures.status` only has `scheduled|live|completed` (per the TDD §2.1 enum) even though TDD §2.3 talks about "cancelled/postponed" fixtures — no such enum values exist yet.~~ — **superseded**: a real `POSTPONED` value now exists (Alembic migration `c9e1a4f7d2b6`), added as one shared bucket for every non-live/non-scheduled provider status rather than 8 individually modeled ones — see "Mobile implementation status" below for the full writeup. Still a real, deliberate scope cut from the TDD's implied per-reason granularity, just no longer a total gap.
- ~~Live score ingestion (`ingest_live_scores.py`) has no adapter method to call~~ — **superseded, now real**: rather than adding a new `DataSourceAdapter` method, it reuses `fetch_fixtures` (see "Score display, history backfill, and real live-score ingestion" above), which already carries live goals/status for football and NBA.
- No `watchlist` table exists, so the T-60-minute kickoff push reminder (TDD §5.4) can't be implemented yet — PICK-07 "save to watchlist" is a Phase 2 Could-Have in the PRD.
- Confidence-tier (`High`/`Medium`/`Low`) numeric thresholds are provisional guesses in `app/predictions/service.py` — neither doc defines them.
- `fixtures.odds_provider_external_id` (indexed, unique with `sport_id`, separate from `fixtures.external_id`) exists in code but isn't in the TDD §2.1 schema listing. The odds provider and the stats/fixtures provider for a sport use **different ID spaces for the same real-world fixture** whenever they're different providers (BallDontLie fixtures + TheRundown odds for NBA; API-Football fixtures + TheRundown odds for the 5 European football leagues) — there's no shared ID to join odds to a fixture on in that case. Populated on first successful match via `app/fixtures/service.py:find_fixture_by_abbreviations_and_time` (team abbreviation + a kickoff-time tolerance window), then used as a fast path on later ingests. When the SAME provider supplies both (API-Football fixtures + API-Football odds for Brasileirão), `app/workers/ingest_odds.py:_resolve_fixture` matches directly on `Fixture.external_id` instead — no fuzzy join needed at all (see "Football model + key-player availability" above).
- `DataSourceAdapter.fetch_odds`'s signature is `(sport, league, days_ahead)`, not `(fixture_ids: list[str])` as originally drafted — mirrors `fetch_fixtures`'s shape. Real odds providers are queried by sport+date-range, not by IDs the caller already knows (and, per the point above, the caller's fixture IDs aren't in the odds provider's ID space anyway). All 5 adapter stubs were updated to match; only `TheRundownAdapter`'s is implemented for real.
- `OddsPayload` (`app/adapters/base.py`) carries `home_team_short_name`/`away_team_short_name`/`kickoff_utc` beyond the original shape — needed for the fixture-matching above. `fixture_external_id` on this dataclass is the *odds* provider's event ID, not the stats provider's.
- Only the `h2h` (moneyline) market is ingested by `TheRundownAdapter.fetch_odds`. `spread`/`total` aren't mapped: the `odds` table's generic `home_odds`/`draw_odds`/`away_odds` columns model a two/three-way price, not a point-spread or an over/under line, and nothing downstream (`/picks`, the EV formula) consumes those markets yet.
- `TeamStats`/`team_features` carry a `season_point_diff` column (season-long average point differential, distinct from the last-10 `attack_str`/`defence_str`) beyond the TDD §2.1 shape — the NBA model's `net_rating_diff` feature needs a longer-window signal alongside the short-term form one, and there was nowhere to source it from.
- `app/adapters/balldontlie.py:fetch_h2h_win_rate` is a standalone function, **not** part of the `DataSourceAdapter` ABC — H2H is fixture-specific (needs both teams), not a generic per-team stat, so it doesn't fit `fetch_team_stats(team_id)`'s shape. Called directly by `app/models_ml/nba_features.py`, not through the adapter interface.
- `BaseModel.__init__` gained a second `version` parameter (`app/models_ml/base.py`) — `Prediction.model_version` was originally being set to `model.artefact_path` (a raw filesystem path) because that was the only thing `BaseModel` carried; fixed to carry the `models_registry.version` string too.
- `DataSourceAdapter.fetch_team_stats`'s signature gained an optional `league: str | None = None` kwarg — `BallDontLieAdapter` derives NBA's single league/season internally and ignores it; `APIFootballAdapter` requires it (a team's league can't be inferred from `team_id` alone, and `/teams/statistics` needs both league and season). Threaded through from `ingest_fixtures.py`'s existing per-league loop.
- `app/fixtures/service.py:find_fixture_by_abbreviations_and_time` gained an optional `league_id` kwarg — NBA never needed it (one league per `sport_id`), but football's several leagues sharing one `sport_id` created a real `MultipleResultsFound` risk if two teams in different leagues ever shared an abbreviation. `ingest_odds.py` passes it through; omitting it preserves the exact prior NBA behavior.
- `TeamKeyPlayer.ws_48`/`.per` renamed to `rank_metric`/`combined_metric` (Alembic migration `6a3f9c1e2b7d`) — the only NBA-specific column names in an otherwise sport-agnostic table, renamed so football's Stage 1 (a single real `rating` stat, not two hand-derived approximations) could populate the same columns. Pure rename, zero behavior change for NBA. Same migration adds `InjurySource.API_FOOTBALL` — **note the stored value is `'API_FOOTBALL'` (uppercase, matching the Python enum's member *name*, not `.value`)**, confirmed live after an initial attempt to add lowercase `'api_football'` raised `InvalidTextRepresentationError` (SQLAlchemy's `Enum` column stores the member name for this project's existing `ROTOWIRE`/`BALLDONTLIE` values — see `29c85029ecef_init_schema.py`'s literal `sa.Enum("ROTOWIRE", "BALLDONTLIE", ...)` call).
- `app/models_ml/key_player_availability.py` is a new module holding Stage 2 (`get_key_player_availability`), shared unchanged between NBA and football — split out of `nba_key_players.py`, which now holds only NBA's Stage 1 and re-exports the shared function for backward compatibility.
- `teams.elo_rating` (Alembic migration `f3a8b1c9d4e2`) exists in code but isn't in the TDD §2.1 schema listing — a real, persistent, incrementally-updated per-team Elo rating (`app/models_ml/elo.py`), distinct from the pre-existing `team_features.elo_rating` (a point-in-time snapshot taken at ingest time). `team_features.win_streak`/`.losing_streak` (same migration) are likewise new, sourced from `TeamStats.win_streak`/`.losing_streak`.
- `app/models_ml/historical_key_players.py` is a new module holding the shared box-score/lineup-presence retrodiction-label logic (`historical_key_player_availability`, `index_played_names`, `load_team_key_players_by_team_season`) — previously duplicated inline inside `train_football.py` only, now also used by the rebuilt `app/workers/backfill_predictions.py`. Deliberately separate from `key_player_availability.py`'s live Stage 2 path, same separation principle as NBA's `historical_key_player_availability`.
- Training-time odds matching for football (`ml/training/collect_football_data.py`/`train_football.py`) joins API-Football's own team `code` field against TheRundown's `teams_normalized.abbreviation` as a best-effort cross-provider key — **not verified to match exactly for every club** (this codebase's existing abbreviation-consistency note only ever confirmed BallDontLie vs TheRundown for NBA); unmatched fixtures simply get no real odds/moneyline feature, same graceful-miss behavior as every other cross-provider join here.
- The NBA model's feature set is **16, not TDD §3.3's 13** — see `app/models_ml/nba_features.py`'s module docstring for the full reasoning. It now includes the 4 key-player-availability features (`key_players_available_home/away`, `key_players_per_combined_home/away` — see the Big3/Top5 section above), which the TDD's own §2.1 design added later, replacing a "salary-weighted injury-impact proxy" idea. Pace differential is still omitted even though it's computable at *training* time from `nba_api`'s box-score columns — BallDontLie's live `/games` response has no shooting stats to derive it from at serving time, and a feature that's real in training but permanently `None` in production is worse than not having it. `moneyline_implied_prob_home` is included but genuinely sparse (see below).

**Known follow-ups, not yet fixed:**
- `ingest_fixtures.py`'s team-features loop now caches `fetch_team_stats` per-run and `app/adapters/balldontlie.py`'s `_get_with_retry` backs off on 429 — but BallDontLie's free tier is still tight enough that a large batch of genuinely distinct teams (not just repeats within one run) can exhaust the retry budget. Fine for NBA's 30-team universe; revisit if this pattern gets reused for a sport with far more teams.
- Fixture matching in `find_fixture_by_abbreviations_and_time` assumes team abbreviations are unique per sport and consistent across providers — confirmed true for BallDontLie vs TheRundown on NBA (both use "PHX", "DET", etc.) but not verified for any other provider pair.
- `models_registry.artefact_path` is stored as an absolute Windows path (`C:\Users\User\...`) — fine for this machine, not portable to a Linux container (the actual Render.com deploy target per TDD §7). Revisit before any real deployment — probably a relative path resolved against a configured models directory.
- The training script's flat-stake ROI metric only covers home-side picks with real odds (n=22-30 depending on the run) — the collected odds sample only captured `home_odds`, not `away_odds`, so away-side picks aren't included. A real but small-sample, directionally-positive result (+6.9% to +14.4% across two runs); not a statistically robust claim.

## Additional football prediction markets (double chance, Over/Under goals, Over/Under corners)

Added on top of the core home/draw/away 1X2 market, per a direct user request. Live-researched
before building (minimal, quota-conscious real API calls) rather than assumed — this changed
the actual scope of what's possible per league:

- **Double chance (1X, X2)**: pure arithmetic on the existing calibrated home/draw/away
  probabilities (`app/models_ml/markets.py:double_chance_probs` — P(1X) = P(home)+P(draw),
  P(X2) = P(away)+P(draw)). No new model, no new training data.
- **Over/Under goals** (lines 1.5/2.5/3.5) and **Over/Under corners** (line 9.5, the standard,
  most-traded corners line — confirmed live via Bet365's own offering): both derived by
  treating each side's expected count as independent Poisson-distributed and summing the two
  rates (a standard result: the sum of two independent Poissons is itself Poisson) —
  `app/models_ml/markets.py:over_under_probs`. Goals reuses Layer 1's existing xG output
  (`xg_home`/`xg_away`, now persisted on `Prediction` — previously computed then discarded).
  Corners needed a genuinely new pair of Poisson regressors (`corners_home_model`/
  `corners_away_model`, bundled into the SAME football artefact) — ~~deliberately reusing
  Layer 1's exact same 21-feature vector as input, not new corner-specific rolling-form
  features~~ (a documented simplification: goal-scoring strength correlates with corner
  generation in practice), so this needed a new training TARGET but zero new live
  ingestion/features **— superseded, see the corners-specific rolling-features entry below**.
  Real corners training data was collected via `/fixtures/statistics`
  (`Corner Kicks` stat, confirmed live for both EPL and Brasileirão back through at least
  2024) — `ml/training/collect_football_data.py:collect_corner_stats`, 1 API call per
  fixture (same real, unavoidable cost as lineup collection): **2,818 EPL rows / 2,652
  Brasileirão rows** (~70-74% fixture coverage — not every historical fixture's
  `/fixtures/statistics` call returned a value, left as a real gap, not zero-filled). Retrained
  football model: 1X2 accuracy/RPS unchanged (47.89%/0.2138, as expected — Layer 1/2 untouched),
  new corners regressors' test MAE ≈ **2.2 corners** (average of home/away) on 589 test rows
  with a real corner count — a real, modest predictive signal, not a strong one, consistent
  with reusing goal-based features rather than corner-specific ones.
- **Corners-specific rolling features, built after the user asked whether the already-
  accumulating corner history was actually being used as a model input** (it wasn't — see the
  bullet above; that was flagged honestly at the time as a "documented simplification", not
  something already built despite the framing sounding like it). `app/models_ml/
  football_features.py` gained `CORNERS_FEATURE_NAMES = FEATURE_NAMES + 4 more`
  (`corners_for_home`/`corners_against_home`/`corners_for_away`/`corners_against_away`, each
  team's own rolling average corners won/conceded over its last 5 real matches) — deliberately
  **not** added to `FEATURE_NAMES` itself, so Layer 1's goals regressors and Layer 2's 1X2
  classifier are completely unaffected; only `corners_home_model`/`corners_away_model` see the
  expanded 22-feature vector (18 + 4; it was 25 before the `d0a24d9` prune described above),
  via a new `corners_row` in `app/models_ml/football.py:predict`
  (falls back to `layer1_row` for an older artefact with no `corners_feature_names` key — those
  artefacts' corners regressors were only ever trained on the 21-feature vector, so that's the
  *correct* row for them, not a degraded fallback). Training-side: `merge_corners_into_game_log`
  attaches `CORNERS_FOR`/`CORNERS_AGAINST` onto the game log from the already-collected corners
  parquet before `assemble_from_game_log` runs. Live-serving side: a new `_corners_rolling_live`
  reads our OWN accumulating `fixture_live_state.home_corners`/`away_corners` (populated once
  per fixture at real settlement time, see `_maybe_fetch_corner_stats` above) across a team's
  last 5 real completed fixtures — **no new live API call at all**, confirmed live against a
  real fixture (Colorado Rapids-shaped MLS matchup): `_corners_rolling_live` returned real,
  distinct averages per team from genuinely accumulated history. A `games_df` that never had
  the merge applied (`app/workers/backfill_predictions.py`'s own retrodiction game log) simply
  gets `None` for all four — a real, accepted gap, not extended to retrodiction in this pass.
  **Honest result, not spun positively**: retrained football model, real MLflow-logged
  `corners_test_mae` went from **2.1988 → 2.2048** (589 test rows) — statistically
  indistinguishable, arguably marginally worse, despite ~92% of sampled fixtures having a real
  rolling corners value available (checked directly, not assumed — this wasn't a coverage/
  cold-start problem). The architecture is correct and now genuinely uses real corner-specific
  history instead of reusing goal-shaped features, with zero regression risk to the 1X2/goals
  model (confirmed: accuracy/RPS identical to the prior run) — but it did not measurably improve
  corners predictions in this training run, most likely because attack/defence-based features
  already correlated with most of the same signal, or because 589 test rows is too small a
  sample to detect a real but modest effect. Kept because it's the right foundation regardless
  (real data, no fabrication, will keep accumulating live history going forward), not because it
  already moved the metric.
- **Real odds coverage differs sharply by league/provider — confirmed live, not assumed**:
  API-Football's `/odds` has real "Double Chance", "Goals Over/Under", and "Corners Over Under"
  bet types, but (per the existing per-league coverage finding above) that endpoint only has
  real odds for Brasileirão — the 5 European leagues get **no real double-chance or corners
  odds at all** (probability-only for those markets there). TheRundown's raw `total` block
  DOES carry real Over/Under-goals lines/prices for EPL (confirmed live — same masked-vs-real
  per-affiliate pattern as moneyline) but has **no double-chance or corners market of any kind**
  (its line blocks only ever have `moneyline`/`spread`/`total` keys) — so double chance and
  corners odds are Brasileirão-only, full stop, until a provider with real European coverage
  for those markets is found.
- **Schema**: `Odds` gained `line`/`over_odds`/`under_odds` (nullable) plus two new
  `OddsMarket` enum values, `DOUBLE_CHANCE`/`CORNERS_TOTAL` (Alembic migration `a7c4e2f1b8d3`)
  — double chance reuses the existing `home_odds`/`away_odds` columns (a genuine 2-way market,
  same shape as h2h without a draw) rather than adding yet more columns. `Prediction` gained
  `xg_home`/`xg_away`/`corners_xg_home`/`corners_xg_away` (same migration), all nullable and
  always `None` for NBA.
- **`GET /picks` generalised via `market`/`line` query params** (`h2h` default, preserving
  every existing caller's behavior) rather than new endpoints — `double_chance`/`goals_total`/
  `corners_total` all reduce to the same "pick the model's favourite among whichever outcomes
  have real odds" shape (`app/picks/service.py:best_outcome_from_candidates`), they just differ
  in where the probability/odds come from. `goals_total`/`corners_total` require a `line` query
  param and 422 if it's missing or not one of the supported lines.
- **`GET /fixtures/{id}`'s `prediction` gained an `extra_markets` object** (double chance +
  every supported Over/Under line's probability pair, all derived server-side from the stored
  Prediction row via the same `app/models_ml/markets.py` functions) — so the mobile fixture
  detail screen doesn't need to duplicate any Poisson math itself, just format numbers already
  computed once in Python.
- Mobile: `mobile/app/(tabs)/picks.tsx` originally gained a market chip selector plus a
  goals-line selector for this — **both since removed**, see "Home/Picks merge" below for the
  full history (`picks.tsx` itself no longer exists, having been merged into `(tabs)/index.tsx`,
  which later dropped market-chip filtering entirely as UI clutter). `mobile/app/fixture/
  [id].tsx` gained an "Other Markets" section rendering `extra_markets` inline below the
  existing probability bar — this part is still real and unchanged.

**A real, separate bug was found and fixed while working on this** (reported by the user:
"games not meeting a threshold are still visible"): `GET /picks`'s threshold check
(`meets_threshold`, now deleted — dead code once the fix landed) tested whether **any** of a
fixture's three h2h markets cleared `min_odds`, while the pick actually shown/ranked came from
a completely separate `best_outcome` call that picks whichever market the model favours most —
with no connection between the two. A fixture could pass the threshold on its `away_odds`
(e.g. 6.00) while the actually-recommended pick was `home` at a sub-threshold price (e.g.
1.30), and the API would return it anyway. Fixed in `app/picks/router.py` by computing the
outcome FIRST, then checking that specific outcome's own odds against `min_odds` — the
threshold is now a promise about the pick actually displayed, not about the fixture having
some market, somewhere, that happens to clear it. Regression-tested in
`backend/tests/test_picks_router.py` (a fixture whose favoured selection is deliberately
below-threshold while a different market's odds clear it, asserting it's excluded).

**A second, previously-latent test-infra bug was found and fixed while adding the regression
test above**: `app/core/redis.py`'s connection pool is a module-level singleton, same
one-loop-per-pytest-test problem `tests/conftest.py` already handles for the DB engine — but
Redis's pool was never included in that fixture, since no prior test file had two separate
async test functions both make a real Redis call (`GET /picks`'s caching) in the same session.
Surfaced as `RuntimeError: Event loop is closed` on the second test. Fixed by adding the exact
same dispose-after-every-test pattern for `app.core.redis._pool`.

**A real, found-in-production overconfidence bug in Over/Under goals/corners, reported by the
user from a real screenshot** ("the % probability outputted for predictions... appears too
high for some markets"): confirmed live — several real Scottish Premiership/MLS/CSL picks
showed 98-100% confidence on an Over/Under goals line, an implausible number for any real
match. Root-caused to two independent, stacking issues:
- **Stale `TeamFeatures`** for the specific affected fixtures (`attack_str`/`defence_str`/
  `form_pts_5` all `None`, only `elo_rating` populated) — traced to the same ingest-isolation
  gap fixed earlier the same session (tennis's real 401 silently killing the shared
  `ingest_fixtures`/`ingest_live_scores` run before it reached these teams). Fixed by manually
  re-running `_ingest_fixtures_for_league` for `mls`/`csl`, confirmed live the real, populated
  stats came back immediately (matching a direct live adapter call for the same teams).
- **Over/Under had NO calibration at all** — confirmed by re-running predictions for the same
  fixtures with the now-real features: total xG was still only ~0.8 (implausibly low for a
  real match), proving the null-feature theory was only half the story. The deeper issue:
  `app/models_ml/markets.py:over_under_probs` always ran a raw Poisson CDF straight off Layer
  1's own uncalibrated xG output — unlike the core 1X2 market, which gets isotonic probability
  calibration, Over/Under had none at all. For fixtures whose feature distribution the
  EPL/Brasileirão training data barely covers (Scottish Premiership/MLS/CSL reusing this same
  model, see above), Layer 1 can produce implausibly low expected-goals values, and an
  uncalibrated Poisson CDF turns that straight into an overconfident probability with no
  correction.
- **Fix**: `app/models_ml/football.py:FootballModel.predict` now runs `xg_home`/`xg_away`/
  `corners_xg_home`/`corners_xg_away` through their own `IsotonicRegression` calibrators
  (`ml/training/train_football.py`, fit on real validation-set fixtures — same tool as the 1X2
  calibrators, just calibrating a predicted RATE against its own empirically-observed real
  value instead of a class probability, hence `y_min=0.0` and no `y_max`, unlike the 1X2
  calibrators' `[0.001, 0.999]` probability bound). **Layer 2 (the 1X2 classifier)
  deliberately keeps consuming the RAW, uncalibrated xG** — that's what it was trained against
  in `train_football.py`; feeding it the calibrated value would be a real train/serve
  mismatch introduced by this very fix, not an improvement. `.get()` on every new calibrator
  key keeps an older artefact predating this working exactly as before (uncalibrated).
  Regression-tested in `backend/tests/test_football_model.py` with a fake "doubling"
  calibrator stub, confirming the returned `xg_home`/`xg_away`/`corners_xg_*` are calibrated
  while the row sent to the (also faked) Layer 2 model is provably still the raw value.
- **Retrained and verified live, honestly (not spun positively)**: real test-set xG MAE
  improved modestly — `xg_home` 0.9945 → 0.9625, `xg_away` 0.8602 → 0.8240 — while 1X2
  accuracy/RPS stayed byte-identical (0.4789/0.2138, exactly as expected since Layer 2 is
  untouched). New artefact registered as `football_xgb_v20260801084740`.
- **First fix pass only covered 2 of the affected fixtures, confirmed insufficient from a
  follow-up screenshot** ("2 are adjusted but 2 are not") — re-ingesting fixtures/refreshing
  `TeamFeatures` and regenerating a *sample* prediction proved the fix worked, but every OTHER
  already-predicted fixture in these leagues still held its OLD stale-feature,
  pre-calibration `Prediction` row, since a league re-ingest only auto-queues
  `run_predictions` for a fixture with *no* prediction yet (deliberately, so a routine daily
  re-run doesn't waste real API calls re-predicting something unchanged) — it does not
  retroactively regenerate ones that already exist. Fixed properly: re-ingested
  `scottish_prem`/`mls`/`csl` (all three share this EPL/Brasileirão-trained model) to refresh
  every team's features, then deleted and regenerated **all 34** real scheduled fixtures'
  predictions across the three leagues, not just a couple. Every single one now lands in a
  realistic total-xG range (1.69–2.77, matching typical real match scoring) — including the
  two the user specifically flagged as still-broken, SHANGHAI SIPG vs Shandong Luneng
  (98% → 85.7%) and Chengdu Better City vs Wuhan Three Towns (99% → 90.8%).

## Head-to-head panel replaces the raw Odds table on fixture detail

Per direct user request: "Users don't find the Odds section useful, instead they've asked that
this be replaced with H2H statistics between the two teams." Turned out to be cheaper than it
looked — `/fixtures/headtohead` was already being called every time a prediction is generated
(`fetch_h2h_stats`, feeding the model's `h2h_win_rate_home`/`h2h_avg_goals_scored_home`/
`h2h_avg_goals_allowed_home` features).

**Redesigned again, per a direct follow-up request**, after the user saw the first version (a
3-box record + an avg-goals line + a list of individual past meeting scores) and asked for 5
specific aggregate stat comparisons instead ("show important stats that will give users
confidence on the prediction... Average goals/corners/Total Shots/Shots on goal/Ball Possession
in the last 5 meetings for Team A and B Respectively"), dropping the individual-meeting-score
list entirely:

- **`app/adapters/api_football.py`** gained `MatchStats` (`corners`/`shots`/`shots_on_goal`/
  `possession_pct`, all nullable) and `fetch_match_stats(fixture_external_id)` — one real call to
  `/fixtures/statistics`, confirmed live to return `"Corner Kicks"`/`"Total Shots"`/`"Shots on
  Goal"` as real ints and `"Ball Possession"` as a **string percentage** (`"27%"`, parsed via
  `.rstrip("%")` before `float(...)`) per team. `fetch_corner_stats` (used by the existing
  corners-settlement path) is now a thin wrapper over this, preserving its exact prior external
  behavior. `H2H_DETAIL_MEETINGS = 5` is a deliberately separate constant from the model-feature
  `H2H_LOOKBACK_MEETINGS = 10`, so this display-only panel can never silently change model
  training/serving behavior.
- **`H2HDetail`** dropped `recent_meetings` entirely and gained `avg_corners_home/away`,
  `avg_shots_home/away`, `avg_shots_on_goal_home/away`, `avg_possession_home/away` (goals'
  `avg_goals_scored_home`/`avg_goals_allowed_home` renamed to `avg_goals_home`/`avg_goals_away`
  to match the new naming). `fetch_h2h_detail` now makes up to 6 real calls per fixture-detail
  view (1 `/fixtures/headtohead` + up to 5 `/fixtures/statistics`, one per counted meeting) — a
  real cost increase from the prior version's 1 call, still bounded per-view, not per-ingested-
  fixture, since this is fetched live at request time (unchanged from the original design).
- **Per-side averaging needs no perspective flip for match stats**, unlike goals: `MatchStats` is
  keyed directly by the real provider team external id, so `_parse_h2h_detail` just checks which
  external id matches the CURRENT fixture's home team per meeting — no
  `_goals_from_home_side_perspective`-style flip logic needed (goals still need it, since the raw
  API response pre-attaches them to a specific historical home/away slot).
- **Never fabricates a value**: each of the 5 stats' averages is computed from its own
  independent list of real, non-null values across the counted meetings — a meeting with real
  corners but a missing possession stat contributes to the corners average while being excluded
  only from the possession average, rather than dropping the whole meeting or fabricating a 0.
  Any single meeting's `/fixtures/statistics` call failing (`httpx.HTTPError`) degrades that one
  meeting's stats to empty rather than failing the whole panel.
- **Fetched live, at `GET /fixtures/{id}` request time — not persisted, not precomputed at
  ingest.** Unchanged from the original design: no new migration, no new column, no scheduled
  job — cost is paid only when a user actually opens a fixture's detail screen.
- **Football only, by design** — unchanged: gated on `sport_slug == "football"` plus both teams
  having a real `external_id`; `None` (never a fabricated empty record) for NBA, an unresolved
  team, or two teams that have genuinely never played each other.
- **Mobile**: `mobile/app/fixture/[id].tsx`'s `HeadToHead` component keeps the 3-box win/draw/win
  record, but the avg-goals line and recent-meetings list were replaced with a `StatRow` per
  stat (Goals/Corners/Total Shots/Shots on Goal/Possession), each showing the home team's value
  left-aligned and the away team's value right-aligned around a centered label — a row is
  omitted entirely (not shown as "— / —") when both sides are null for that specific stat.
- **Verified live end-to-end** against a real Internacional vs Cruzeiro fixture (Brasileirão):
  5 real meetings, 2 home wins / 1 draw / 2 away wins, real averages across all 5 stats (goals
  1.2/0.8, corners 6.0/5.4, shots 13.8/11.6, shots on goal 5.2/4.2, possession 49%/51%) —
  confirmed via both the raw `GET /fixtures/{id}` JSON and a screenshot of the actual rendered
  panel in the running mobile app (Expo web), no console errors. Full backend suite (187 tests),
  `ruff check .`, and `black --check .` all pass; `npx tsc --noEmit` clean on mobile.

## Mobile implementation status

`mobile/` is a real Expo Router app (SDK 57, TypeScript), scaffolded and live-tested end to end against the running backend on **both** Expo web and a real Android emulator (registration, login, logout, guest-session creation/migration, and every §5.2 screen route) — not just created and left unverified.

**Real and wired to the live backend**: NativeWind v4 (Tailwind for RN), TanStack Query, Zustand, Expo SecureStore-backed JWT storage (`lib/tokenStore.ts` + `store/authStore.ts`), and a thin `lib/api/*.ts` client layer mirroring every real backend schema by hand (`lib/api/types.ts` — keep these in lock-step with `backend/app/*/schemas.py`, there's no shared codegen). ~~All 9 TDD §5.2 routes~~ — **superseded, now 8**: Home and Picks were merged into one tab (see "Home/Picks merge" below), so `(tabs)/picks` no longer exists as a separate route. Remaining: `(tabs)/index` (now titled "Picks"), `(tabs)/live`, `(tabs)/profile`, `fixture/[id]`, `history/index`, `how-it-works/index`, `auth/login`, `auth/register`. Live calls real `GET /fixtures?status=live`; Profile calls real `GET/PUT /user/preferences` when authenticated; auth screens call real `POST /auth/register|login`.

**Home/Picks merge — per explicit user request ("I don't think we need two different pages - home and picks... I'd rather we have all the picks displayed in the home page")**: `(tabs)/picks.tsx` was deleted; `(tabs)/index.tsx` absorbed its market/line filter chips and min-odds slider, retitled to "Picks" in `_layout.tsx` (the user's own follow-up: "I want the current implementation of the home retained" — so the day-strip/league-grouped `SectionList`/live-first-ordering/`GuestBanner` structure stayed exactly as it was, only the filtering got stricter and multi-market):
- **min_probability's floor moved from 0.34 to 0.6, and — the actually load-bearing change — it's now a REAL server-side filter, not just a client-side highlight threshold.** Previously `FixtureCard` rendered every fixture regardless of probability, only swapping between a highlighted badge and a plain "Details" line at the 0.34–0.9 slider's position; the user's own words made clear that wasn't the intent ("the intention is not to surface all the games in the league... we just want the best odds with the highest probability of winning"). `GET /fixtures` gained `min_probability`/`min_odds` query params (both optional — omitting them, as the Live tab does, preserves the old show-everything behavior exactly); a fixture whose best pick doesn't clear both is dropped from the response entirely, not just de-emphasized.
- **`best_pick` is now drawn from ACROSS every market** (h2h, double chance, Over/Under goals, Over/Under corners), not just home/draw/away — the user's own words again: "This should be a combination of (1/2/X/1X/2X/corners (over/under)/goals (over/under))". `app/fixtures/router.py:_all_market_candidates` builds every real candidate (up to 12: 3 h2h + 2 double-chance + 6 goals-total-across-3-lines + 2 corners-total) from a fixture's Prediction row + its odds across all 4 markets, and `_pick_best` returns whichever has the single highest probability among those with real odds (falling back to probability-only if literally none have odds yet — same fallback the old h2h-only version had). A regression test (`test_fixtures_best_pick.py`) had to be updated for this: a fixture with home=0.20/draw=0.25/away=0.55 now correctly surfaces double chance's X2 (away+draw=0.80) as the best pick instead of the old h2h-only "away" — a real behavior change, not a bug, confirmed via the user's own explicit ask.
- **A `market`/`line` query param pair exists on `GET /fixtures`** (mirroring `GET /picks`'s own) to restrict `best_pick` to one specific market instead of the default combined-best-across-all — real and tested server-side, but **not exposed in the mobile UI at all**: a market-chip selector (`MarketFilterChips`, "All" + 4 specific markets) was built, then immediately removed again per an explicit follow-up ask ("I like to remove the filter for different markets... take all off and the default behaviour should always be all... This way the UI is less cluttered"). `mobile/components/MarketFilterChips.tsx` was deleted outright (nothing else used it); `index.tsx` never passes `market`/`line` to `listFixtures` anymore, so every fixture's `best_pick` is always the combined-across-all-markets choice. If a future screen wants per-market filtering again, the backend capability and its tests are still there unchanged — only the mobile plumbing for it was removed as unused.
- **A real regression, found via the user's own follow-up ask ("show past predictions like before indicating win or losses with green and red")**: once `best_pick` started being drawn from across every market, `FixtureCard`'s completed-fixture badge could only ever compute a real win/loss verdict for an **h2h** best_pick — a `double_chance`/`goals_total` best_pick (now the common case at a 60% floor) fell back to a grey badge with no ✓/✗ at all, silently losing the retrodiction-vs-real-result feature built earlier in this session. Fixed with `mobile/lib/pickFormat.ts:evaluatePickCorrectness`, which derives the real result from `live_state.home_score`/`away_score` (already available client-side, no new API call) for h2h (exact selection match), double chance (1X = home-or-draw, X2 = away-or-draw), and goals_total (real total goals vs. the pick's own line) — all **client-side**, no backend change needed. `corners_total` genuinely can't be verified this way (`FixtureLiveState` tracks no corner count at all, only goals) and stays a real, honest `null` → still shown as a neutral grey badge, never a fabricated ✓ or ✗.
- **`GET /picks` itself is untouched and still fully tested** — nothing currently calls it from mobile (the merged Picks screen uses `/fixtures` exclusively, since it needs day-strip/league-grouping/live-state that `/picks` was never shaped for), but it's kept as a real, working, separately-testable API rather than deleted, in case another consumer (a future web dashboard, third-party integration) wants a flat EV-ranked list without the schedule-browsing shape. `mobile/lib/api/picks.ts` and the `PickResponse` type were deleted (dead code once nothing in the app called them); `PickMarket` survived since `BestPick` still needs it to type `best_pick.market`.
- **A further follow-up ask, correctly distinguished from the fix above**: "I need all markets predicted in the past to still be shown to evaluate performance. I don't mean win and loss only... Everything should be shown." — this isn't about fixing `best_pick`'s single verdict (already done), it's that a completed fixture only ever showed its ONE winning market, discarding what the model called on every OTHER market for that same game. `FixtureSummary` gained `all_market_picks: list[BestPick]` — every real candidate across all four markets (`_bulk_best_picks` now returns `(best_picks, all_picks)`, building both from the exact same `_all_market_candidates` call per fixture, no extra DB query), deliberately **independent of the `market`/`line` query param** (which only restricts `best_pick` — a caller asking for `market=h2h` still gets the full cross-market breakdown in `all_market_picks`, since evaluating past performance shouldn't be limited by whatever market a filter happened to be scoped to). Mobile: `lib/pickFormat.ts:buildMarketBreakdown` groups these into one display row per genuinely distinct prediction — h2h shows all 3 outcomes and double chance shows both 1X/X2 (none of those are redundant with each other), but goals/corners totals collapse each line's real over+under pair (complementary probabilities, summing to ~1) down to the model's own favoured side, each verdict computed via the same `evaluatePickCorrectness`. Rendered as a wrapped row of small green/red/grey chips below the score on `FixtureCard`, only for completed fixtures.
- **The very next ask closed the one remaining honest gap**: "now show green and red for success or failure based on the actual result for all the markets" — `corners_total` was still the one market stuck permanently grey (no real corner-kick count was ever tracked anywhere). Fixed with a real, live-verified addition rather than faking a verdict: `fixture_live_state` gained `home_corners`/`away_corners` (Alembic migration `b2e6f4a9c1d7`), populated by a **new, one-off real API call** — `app/adapters/api_football.py:fetch_corner_stats` (same `/fixtures/statistics` "Corner Kicks" field `ml/training/collect_football_data.py:collect_corner_stats` already used for bulk historical collection, now a live single-fixture equivalent, mirroring `fetch_lineup_presence`'s established pattern) — called from `app/workers/ingest_fixtures.py:_maybe_settle_outcome`'s existing settlement idempotency guard (`_maybe_fetch_corner_stats`, football-only, degrades to `(None, None)` on any `httpx.HTTPError` rather than blocking Outcome/Elo settlement). `_maybe_settle_outcome` gained a required `sport_slug` param for this gating — every call site (`ingest_fixtures.py`, `ingest_live_scores.py`, and their tests) updated. **Verified live against the real API**: deleted a real fixture's `Outcome` row, re-ran settlement, confirmed real corner counts (Coritiba 8, Palmeiras 4) were fetched and stored. Mobile: `evaluatePickCorrectness`/`buildMarketBreakdown` gained optional `homeCorners`/`awayCorners` params, so `corners_total` now shows a real ✓/✗ whenever those exist — genuinely `null` (neutral grey, never fabricated) only for NBA or a fixture settled before this migration.
- A real, recurring **dev-environment gotcha hit twice while verifying this feature live**: the local `uvicorn --reload` process's `WatchFiles` watcher silently stopped picking up file edits partway through this session (confirmed via `GET /openapi.json` still showing the pre-edit param list) — no error, just stale code serving real requests. Fixed both times by killing the process and starting a fresh one, not by trusting `--reload` to recover on its own. Worth checking `/openapi.json` for a just-added query param before assuming a "why isn't this filtering working" symptom is a code bug.
- **A real, genuine bug found from the user's own screenshot** ("Results of past games are missing... Show green and red for success or failure based on the actual result for all the markets"): `GET /fixtures`'s `min_probability`/`min_odds` filtering was applying to EVERY fixture, including already-**completed** ones — a finished game with a modest-confidence prediction got silently dropped from the response entirely, exactly like every other fixture below the floor. That's the wrong semantics for a past result: `min_probability`/`min_odds` encode "is this worth betting on", a question that only makes sense for a game that hasn't been decided yet: a completed fixture is being reviewed for how the model actually performed, not considered as a future bet. Fixed in `list_fixtures` by always keeping `status == "completed"` fixtures regardless of the threshold, never touching scheduled/live filtering. **Verified live**: the two real fixtures that had actually finished for the selected day (Internacional 1-1 Flamengo, Mirassol 2-1 Remo — settled via a real `ingest_live_scores` run against the live API, which also populated their real corner counts per the fix above) now correctly appear with their full `all_market_picks` breakdown regardless of the 60%/1.50 floor, each market showing a real green/red verdict against the actual result.
- **The per-market chip breakdown was then reverted, immediately after the above shipped, per direct visual feedback on a real screenshot**: shown a completed Fluminense-Bahia card (score only, single "✗ OVER 1.5 85%" badge) side by side with the wrapped-chip breakdown, the user said "I dont need all these details [the chip breakdown] ... The depiction in the second image [the single badge] is fantastic enough." `FixtureCard.tsx`'s `MarketBreakdown` component and its usage were removed entirely, along with the now-dead `buildMarketBreakdown`/`MarketBreakdownItem` in `lib/pickFormat.ts` — completed fixtures now show only `ScoreBadge`'s single best-pick ✓/✗ line again, matching the pre-"show every market" behavior. `GET /fixtures`'s `all_market_picks` field itself was left alone on the backend (real, tested, harmless to keep returning) since the ask was specifically about mobile's visual density, not the API shape — nothing currently reads that field from mobile anymore, but it's kept for any future consumer per the same reasoning `GET /picks` itself was kept unused-but-real.
- **A design follow-up, from a screenshot showing "OVER 1.5 85%" and "UNDER 3.5 94%" badges at visibly different widths**: both `ScoreBadge`'s verdict pill and `PredictionBadge`'s upcoming-pick box now share one fixed `BADGE_WIDTH` (104px) in `FixtureCard.tsx`, text centered and clipped to one line, so a stacked list of cards lines up regardless of which market/selection won. Double chance's labels were also shortened from "1X (Home/Draw)"/"X2 (Away/Draw)" to plain "1X"/"X2" in `lib/pickFormat.ts` — the longer form was the actual driver of the width mismatch and wasn't needed anywhere else (the fixture detail screen already spells these out with its own copy).
- **A real, genuine data-integrity bug found from the user's own screenshot** ("Some of the originally scheduled games for yesterday were postponed hence not resolved - but they are still on display"): API-Football genuinely returns "PST" (and CANC/ABD/SUSP/INT/TBD/AWD/WO) for a real postponed/cancelled/abandoned fixture — confirmed live, 4 real Brasileirão fixtures were actually postponed (Sao Paulo-Santos, Botafogo-Gremio, Chapecoense-Vasco DA Gama, Atletico-MG-RB Bragantino, all 2026-07-29). `app/adapters/api_football.py:_map_status` was mapping every one of those 8 codes to `"scheduled"` (a prior, deliberate least-bad-bucket choice — see the TDD §2.1/§2.3 divergence note — made before the schema had anywhere better to put them), which meant a postponed fixture kept showing its pre-postponement market prediction/odds badge as if the game were still on, exactly the user's report. Fixed with a genuinely new `FixtureStatus.POSTPONED` value (Alembic migration `c9e1a4f7d2b6`, `ALTER TYPE fixture_status ADD VALUE` in an `autocommit_block()` per the same pattern as `6a3f9c1e2b7d`'s `API_FOOTBALL` addition) — `_map_status` now returns `"postponed"` for all 8 codes (still one shared bucket, not 8 individually modeled statuses, a deliberate scope cut flagged in the enum's own docstring). `GET /fixtures`/`GET /fixtures/{id}` now explicitly null out `best_pick`/`all_market_picks`/`prediction` for a POSTPONED fixture regardless of what Prediction/Odds rows already exist from before the postponement was known, and — same reasoning as the completed-fixtures fix above — POSTPONED fixtures are never hidden by `min_probability`/`min_odds` either (there's nothing to bet on, but the day's schedule should still show what happened to it). `ingest_fixtures.py`'s upcoming-features loop now also excludes POSTPONED alongside COMPLETED (no point computing a pre-game feature vector for a game that isn't being played). Mobile: `FixtureCard.tsx` gained a `PostponedBadge` (neutral grey "POSTPONED" pill, same fixed `BADGE_WIDTH` as every other badge) and an amber "Postponed" label, both rendered instead of the score/prediction slot — never showing stale data since the backend already nulled it. **BallDontLie has no equivalent signal at all** (its `status` field is a free-form string — "Final"/a start-time string/a period string — with no postponed marker, confirmed via its own adapter's docstring), so NBA fixtures genuinely can't be detected as postponed this way; a real, honest gap, not fabricated. **Verified live**: re-ran real ingestion against Brasileirão, confirmed all 4 fixtures — the exact ones from the user's screenshot — transitioned from `scheduled` to `postponed`, and that `GET /fixtures?min_probability=0.6&min_odds=1.5` still returns all 4 with `best_pick: null`/`all_market_picks: []` despite the floor.

**Country flags — a two-step fix, the first attempt genuinely insufficient**: per direct user request ("Instead of the world icon, lets use the countrys flag. Note all flags must be the same sizes"), `mobile/lib/countryFlags.ts`'s `COUNTRY_FLAGS` map (Unicode flag emoji) was missing entries for Scotland/China — the two new countries from the 3-leagues addition above — so both silently fell back to the 🌍 globe. First fix: added `Scotland: "🏴󠁧󠁢󠁳󠁣󠁴󠁿"`/`China: "🇨🇳"` and wrapped the rendering `Text` in a fixed 24×24 `View` for uniform sizing. **This was not enough** — the user's next screenshot showed Brazil/China rendering as literal `"BR"`/`"CN"` text and Scotland as a bare black flag, not real flags at all. Root cause: this genuinely can't be fixed by picking a different emoji — Unicode regional-indicator flag sequences (🇧🇷, 🇨🇳) simply don't compose into a flag glyph in this environment's font stack, they render as their raw underlying two-letter code text, and Scotland's flag is a 7-codepoint TAG sequence that degrades even further. Fixed for real by abandoning text emoji entirely: `mobile/lib/countryFlags.tsx` (renamed from `.ts`, now a component file) renders real flag PNGs (public-domain national symbols, downloaded from flagcdn.com) bundled locally under `mobile/assets/flags/` via RN's native `Image` component — a `CountryFlag` component replaces the old `countryFlag()` string helper, one call site (`(tabs)/index.tsx`'s section header). Every flag renders at an identical `size × size` box with `resizeMode="cover"`, not `"contain"` — real flags have different aspect ratios (Brazil's is far more square than the UK's or the US's), so center-cropping to a fixed square is what actually guarantees uniform *visual* size, not just an equal bounding box. The 🌍 fallback for a null/unrecognised country is untouched — single-codepoint emoji render fine everywhere; it was only ever the multi-codepoint flag sequences that broke.

**A real backend bug was found and fixed via this integration testing**: `RegisterRequest` (`app/auth/schemas.py`) has `model_config = ConfigDict(strict=True)`, and pydantic-core's strict UUID validator rejects a JSON string outright — it demands an actual `UUID` instance, which no JSON body can ever supply. Every real client sending a non-null `guest_session_id` (the TDD §2.1 guest-migration-on-register flow) hit a 422 until this was caught — fixed with a per-field `Field(strict=False)` override, regression-tested in `backend/tests/test_auth_schemas.py`. This had zero test coverage before (no prior test, curl check, or client ever exercised registration with a real, non-null guest session id) — same "first real caller finds a latent bug" pattern as the fixture/odds/injury ID-mapping bugs above.

**Deliberately deferred, not yet built** (still just `mobile/`'s own scaffold, not stubbed backend-style — there's no route/screen for these at all yet): the animated match tracker and Highlightly video embed (§5.3, both explicitly Phase-2/needs-a-license-or-WebSocket-feed anyway), bottom sheets (`@gorhom/bottom-sheet` — the TDD's guest soft-gate is currently a plain banner + Link, not a modal sheet), and charts (Victory Native XL — the fixture-detail probability bar is a plain `View`-based bar, not a chart library). None of these packages are installed yet; add them when their screen is actually built, not preemptively.

**Push notifications and biometric login (TDD §5.4/§5.1) are real, backend-wired, and live-verified on a real Android emulator — including simulated fingerprint touches, not just typechecked**:
- Backend: `PUT /user/push-token` (auth-gated, `app/users/router.py`) validates via `exponent_server_sdk.PushClient.is_exponent_push_token` and writes `users.expo_push_token`; `null` clears it (device disabled push). `app/workers/notify_users.py`'s `_send_push` is a real implementation now, not the old `NotImplementedError` stub — it POSTs to Expo's push API via `exponent-server-sdk` (already a declared dependency, previously unused), run off the event loop via `asyncio.to_thread` since the SDK is synchronous (`requests`, not `httpx`), and self-heals: `DeviceNotRegisteredError` clears that user's stale token so future runs stop retrying a dead one. `EXPO_ACCESS_TOKEN` is still unprovisioned (see `.env.example`) — Expo's push API accepts unauthenticated sends at a lower rate limit, so this is real and callable, just not rate-limit-hardened yet. Tested in `backend/tests/test_push_token.py`: token-format validation, real persistence via a real registered user, and the self-healing DB update (mocking only the third-party `PushClient.publish` call itself, per this project's established "mock the external API boundary, use real DB" convention).
- Mobile: `lib/notifications.ts` (permission request + Expo push token registration + local enabled-flag) and `lib/biometricAuth.ts` (SecureStore `requireAuthentication`-gated refresh-token vault, separate `keychainService` from the plain token storage per SecureStore's own docs) are both real, wired into `(tabs)/profile.tsx`'s two toggles and `auth/login.tsx`'s conditional "Log in as {email}" biometric button.
- **A second on-device-only crash, worse than the slider bug above — this one took the whole app down, not just one screen's data**: `expo-notifications` throws the instant it's imported inside Expo Go on Android (SDK 53+ dropped Expo Go support for Android remote push entirely — confirmed live via the redbox: *"Android Push notifications... removed from Expo Go with the release of SDK 53. Use a development build"*). A plain top-level `import` crashed the entire root layout. Wrapping the `require()` in try/catch was **not sufficient** — the thrown error still reached React Native's uncaught-error overlay regardless of the JS-level catch (the module appears to report it straight to the native error handler, not just via a catchable JS exception). The real fix was to never call `require("expo-notifications")` at all in this situation: `lib/notifications.ts`'s `isExpoGoOnAndroid()` checks `Constants.executionEnvironment === "storeClient"` (the non-deprecated replacement for `Constants.appOwnership`, which came back `null` at runtime on this SDK — confirmed live, not just per the deprecation notice) before ever touching the module. Every exported function in that file goes through this guard, and `addNotificationTapListener`/`registerForPushNotificationsAsync` degrade to a safe no-op / a caught `PushRegistrationError` respectively rather than a crash.
- Biometric login was verified genuinely end to end, including the tricky part: the backend **rotates refresh tokens on every `/auth/refresh` call**, so the biometric-gated vault goes stale after one use unless re-synced — confirmed this actually works by logging in with biometrics *twice* in a row (real fingerprint confirmation both times) and confirming two separate real `POST /auth/refresh → 200 OK` calls in the backend log, proving `storeBiometricRefreshToken` correctly re-persists the rotated token after every biometric login, not just the first.
- Enrolling a fingerprint on the emulator to actually test this was its own small side-quest: `adb shell input` interactions with Android's own PIN-entry/pattern-lock/fingerprint-enrollment screens rendered solid black in `adb exec-out screencap` (a real emulator/software-rendering quirk specific to certain system dialogs — `BiometricPrompt` itself has the same rendering issue) even though the real UI was live and responsive underneath; `adb shell uiautomator dump` (reads the accessibility tree, not pixels) confirmed the true state and provided real element bounds to tap/type against blind. `adb -e emu finger touch 1` is the documented way to simulate a fingerprint match on an AVD.

Push notifications aren't fully end-to-end deliverable yet regardless of the above: no `EXPO_ACCESS_TOKEN` is provisioned and this project has never been linked to a real EAS project (`Constants.expoConfig.extra.eas.projectId` is unset), so `getExpoPushTokenAsync` can't actually mint a token even on a platform where the module loads — confirmed live on Android (blocked earlier by the Expo Go restriction above) and not yet tried on iOS/web. Same "correct code, no live credential yet" status as RotoWire/BallDontLie injuries.

**Real bugs found and fixed while building the scaffold itself** (both are general React Native/Expo Router gotchas, not SportIQ-specific, but cost real debugging time):
- A require cycle (`store/authStore.ts` → `lib/api/auth.ts` → `lib/api/client.ts` → `store/authStore.ts`, from the API client reading tokens out of the Zustand store directly) showed up as a Metro bundler warning. Fixed by extracting token state into `lib/tokenStore.ts` — a plain, non-React module with its own tiny pub-sub — so the API client depends on it one-directionally, and `authStore.ts` becomes a thin Zustand wrapper that subscribes to it for UI reactivity. The client's own refresh-on-401 flow writes through `tokenStore` directly (not through Zustand), which is exactly why the subscription is needed to keep the UI in sync with a rotation the UI itself didn't trigger.
- `SportFilterChips`' horizontal `ScrollView` visibly stretched to fill half the screen — but only on screens where the `FlatList` below it was empty (Picks/Live with no matching fixtures), never on Home. Root cause: RN's `ScrollView` bakes `flexGrow: 1` into its base style; two `flexGrow: 1` siblings in a column flex container split whatever leftover space exists 50/50, and an empty `FlatList` leaves a lot of leftover space where a populated one doesn't. Fixed with `grow-0` on the chips `ScrollView`'s own className (not just its content container) — a real cross-platform footgun any future horizontal-scroller-above-a-list layout in this app could hit again.
- `lib/api/client.ts`'s `BASE_URL` needed a platform-specific rewrite: the Android emulator's `localhost` is the emulator's own loopback, not the host machine — `10.0.2.2` is Android's documented host alias. `resolveBaseUrl()` rewrites `localhost`/`127.0.0.1` → `10.0.2.2` only when `Platform.OS === "android"`; a real device instead needs an actual LAN IP in `EXPO_PUBLIC_API_URL` (this rewrite doesn't help that case). Confirmed live: without this, every request from the Android build failed with "Couldn't reach the SportIQ API" while the identical web build worked fine against the same backend.
- **A genuine on-device-only bug, only reproducible on a real Android emulator** (invisible on web, where the verification above had already passed): `@react-native-community/slider`'s Android-native `SeekBar` backing view fires `onSlidingComplete` once on mount with no real touch involved, reporting `minimumValue` (1.01) — silently overwriting a brand-new guest session's `min_odds` the instant the Picks tab was visited, before the user ever touched the slider. Confirmed by directly querying a freshly-registered account's `GET /user/preferences`: `default_min_odds` came back `1.01` despite the slider never being touched. Fixed in `app/(tabs)/picks.tsx` by gating `onSlidingComplete` behind a `hasStartedSliding` ref that's only set by a genuine `onSlidingStart` — a completion with no preceding start is now ignored. Verified fixed by clearing the emulator's app data (`pm clear host.exp.exponent`, for a truly fresh guest session) and confirming a fresh registration's `default_min_odds` came back `null` even after visiting Picks without touching the slider.

**Real, live-verified against the actual running backend on two platforms — not just typechecked**: a headless-browser session (Playwright + the system's own Edge/Chromium install) covered the web target; a real Android emulator (`Pixel_7_-_Newest` AVD, via Expo Go, driven by `adb shell input`/`uiautomator dump` for exact tap coordinates and `adb exec-out screencap` for visual verification) covered the native target. Both: Home renders real fixtures fetched from `GET /fixtures` (including the historical Pistons-vs-Suns fixture's real prediction probability bar and real BetMGM odds on `fixture/[id]`, web only — not re-checked on Android); Picks/Live correctly render their real empty states (no scheduled fixtures exist in the seed data, no fixtures are live); guest → register → authenticated Profile (real email, real `GET /user/preferences`) → logout → back to guest all round-tripped for real on both platforms; `history/index` correctly surfaces the backend's real `501` as a friendly message instead of erroring (web only). Zero console/runtime JS errors in the final pass on either platform. iOS is untested — no Mac available in this environment.

### Mobile dev commands

```bash
cd mobile
cp .env.example .env              # EXPO_PUBLIC_API_URL — defaults to http://localhost:8000
npm install
npx expo start --web              # web target
npx expo start --android          # Android emulator/device, via Expo Go (auto-installed on
                                   # first run) — requires an AVD already created in Android
                                   # Studio, or a physical device with adb debugging enabled
npx tsc --noEmit                  # typecheck — no ESLint/Jest configured yet, see "Not yet configured"
```

The backend must be running (`docker compose up -d && cd backend && uvicorn app.main:app`).
**If `mobile/.env`'s `EXPO_PUBLIC_API_URL` is a LAN IP rather than `localhost`, uvicorn MUST be
started with `--host 0.0.0.0`** — the default binds 127.0.0.1 only, so nothing listens on the
LAN interface and every request from the app fails with "Couldn't reach the SportIQ API" while
`curl http://localhost:8000/health` returns a perfectly healthy 200. Confirmed by breaking it:
restarting uvicorn without the flag took the app down while the API itself was fine, and the
give-away is that the uvicorn access log shows NO requests from the app at all — the client
never reached it. Check `EXPO_PUBLIC_API_URL` and the bind address before investigating CORS,
which is the more obvious suspect and was not the cause. For the web target, `CORS_ORIGINS` must include the Expo web dev server's origin (`http://localhost:8081` by default) — `backend/.env.example` doesn't ship this by default since it's a mobile-dev-only concern, add it to your local `backend/.env`; native targets (Android/iOS) aren't subject to CORS at all (that's a browser-only mechanism), so no CORS change is needed for them. iOS Simulator/device and a real (non-emulator) Android device are both untested so far — this machine has no Mac, and no physical device was connected.

## External API research findings — ground truth, not TDD assumptions

Live-tested against the real keys in `keys.docx` (minimal, quota-conscious calls) before implementing `BallDontLieAdapter`. Keep these in mind before touching any adapter — the TDD's own assumptions about at least one of these were wrong:

- **BallDontLie**: base URL `https://api.balldontlie.io/nba/v1`. Auth header is `Authorization: <raw key>` — **no `Bearer` prefix**. `GET /games` accepts `start_date`/`end_date` (`YYYY-MM-DD`), `seasons[]`, `team_ids[]`, `per_page` (max 100); pagination is **cursor-based** (`meta.next_cursor`), not offset. Each game embeds full `home_team`/`visitor_team` objects, so no separate `/teams` call is needed. `status` has no fixed enum: `"Final"` once done, a start-time string like `"7:00 pm ET"` before tip-off, a period string (`"1st Qtr"`, `"Halftime"`, ...) while live — see `_map_status` in `app/adapters/balldontlie.py`. `/season_averages/*` and `/standings` return 401 on this key's (free) plan — not used by the current implementation.
- **API-Football**: only reachable via the direct `v3.football.api-sports.io` host with an `x-apisports-key` header — **not RapidAPI**, despite TDD §2.2 saying "API-Football (via RapidAPI)". The *original* Free plan (confirmed via `GET /status`: Free, 100 req/day) blocked the `next` query param and any season after 2024 — no real upcoming/current-season fixtures were reachable at all, which is why `BallDontLieAdapter`, not `APIFootballAdapter`, became the *first* real adapter.
  **Superseded**: the user later subscribed to a real **Pro** plan (7,500 req/day, confirmed active until 2026-08-29 — time-limited, see "Football model" section above) that unlocks everything the free tier blocked. Confirmed live under Pro: real league IDs via `GET /leagues?name=X&country=Y` — Premier League=**39**, Ligue 1=**61**, Bundesliga=**78**, La Liga=**140**, Serie A=**135** (must match `app/adapters/therundown.py`'s `_RUNDOWN_SPORT_IDS` keys exactly — `epl`/`ligue1`/`bundesliga`/`laliga`/`seriea`). `next=N` and `from`/`to` date-range fixture queries both work for the current (not-yet-started) season. Historical coverage (fixtures/lineups/statistics/players/injuries) is complete back through at least 2021. `/teams/statistics?league=X&season=Y&team=Z` (needs league+season, not just team_id) returns `form` (real "WWDLW..." string), `goals.for/against.average.total`, and `fixtures.wins/played.{home,away}` — but **before a season's first match, the goals-average fields come back as the string `"0.0"`, not `null`** (a real data-quality gotcha, see "Football model" section for the fix); still no `elo_rating`/xG in the response at any tier. `/injuries?league=X&season=Y&date=Z` supports real bulk-by-league-and-date queries (not just by fixture) and is genuinely empty for dates too far in the future. `/players?team=X&season=Y` returns a real per-match `games.rating` plus `games.minutes`/`appearences` — but `statistics` is an array keyed by **competition**, so a player's cup-competition entry can precede their domestic-league one; never assume `statistics[0]`. `/fixtures/headtohead?h2h={id1}-{id2}` is a real, dedicated H2H endpoint (simpler than NBA's own manual-search workaround).
- **TheRundown**: correctly reached via RapidAPI (`therundown-therundown-v1.p.rapidapi.com`, `x-rapidapi-host`/`x-rapidapi-key` headers) — but the key must be *subscribed* on RapidAPI's site first (a bare "You are not subscribed to this API" 200-with-error response otherwise, even with the exact right host/key — this isn't a wrong-host signal the way it was for API-Football). Real sport IDs confirmed via `GET /sports`: NBA=4; each football league is its own ID, not one combined "football" ID — EPL=11, Ligue1=12, Bundesliga=13, La Liga=14, Serie A=15. Events come from `GET /sports/{id}/events/{date}?include=scores`, one date per call (no range param) — team info is embedded (`teams_normalized`, with a reliable cross-provider `abbreviation` field), so no separate team lookup is needed. **Odds are in American format** (`"format":"American"` in every line block) despite TDD §2.3 saying odds are "normalised to decimal format" at ingest — conversion is on us (`_american_to_decimal`). Confirmed live: only 3 of ~15 bookmaker affiliates (Bovada, Bodog, BetMGM) return real prices on this subscription; the rest (DraftKings, FanDuel, Pinnacle, Unibet, ...) return a masked `0.0001` sentinel for every field — treated as "no line from this book," not an error, and simply skipped. Historical dates have **no season cutoff** (unlike API-Football) but real-price coverage is inconsistent even for the 3 unlocked affiliates — confirmed some dates fully real, others fully masked, seemingly per-game/per-book rather than date-based.
- **TheRundown rate limiting escalates oddly under load**: a burst of ~60 requests with no delay produced a sustained run of `429`s that then escalated into `401`s on retry — manually confirmed seconds later (plain `curl`, no code involved) that the key was completely fine, so the `401`s were a transient RapidAPI-gateway artifact of the burst, not a real auth or account problem. 5 seconds between requests was confirmed reliably under the threshold; don't reflexively read a `401` from this provider as "key revoked" without checking via a slow, isolated request first.
- **Silent-wrong-`.env` trap for any one-off script outside `backend/`**: `pydantic-settings`'s `env_file=".env"` resolves against the process's **current working directory**, not the importing file's location. A script in `ml/training/` run from the repo root (rather than from `backend/`) silently loads a blank `.env` and every setting falls back to its default (e.g. `therundown_api_key=""`) — no error, just a confusing downstream `401` that looks exactly like a real auth/rate-limit failure. Fix: `load_dotenv(BACKEND_DIR / ".env")` explicitly before importing anything that calls `get_settings()` — see `ml/training/collect_nba_data.py`/`train_nba.py` for the pattern. If a script outside `backend/` ever gets a mysterious blank-credential failure, check this first.

## Secrets handling

- Real credentials live only in `keys.docx` (gitignored) and, once services exist, in environment variables (`.env`, Render/AWS secrets manager — see TDD §4.3, §7).
- When a task needs a specific key's value, read it from `keys.docx` at the point of use; do not copy values into source files, `CLAUDE.md`, commit messages, or scratch files that might get committed.
- The target GitHub repo (`UsangR01/sportiq`) is **public** — treat anything staged for commit as public before it's pushed.

## Intended architecture (per TDD v1.5)

Python-first, microservice-adjacent backend with a native mobile frontend:

```
External APIs → Celery ingest workers → PostgreSQL + Redis → FastAPI (REST/WebSocket) → Expo (React Native) app
```

- **Backend**: FastAPI (Python 3.12) + Uvicorn/Gunicorn, async SQLAlchemy over PostgreSQL 16, Redis 7 for caching/sessions, Celery + Redis broker for ingestion and inference jobs.
- **Mobile app**: Expo SDK 51+ (React Native, single TypeScript codebase for iOS + Android), Expo Router (file-based nav), NativeWind (Tailwind for RN), TanStack Query, Zustand, Expo SecureStore for JWT (never AsyncStorage). This is the *only* client — there is no web frontend in scope.
- **ML**: sport-specific models behind a common `BaseModel.predict()` interface, resolved at runtime via a `models_registry` DB table (model promotion = DB update, not a deploy). Football uses a two-layer Poisson (xG) → CatBoost/XGBoost (1X2) stack; NBA uses a single XGBoost binary classifier. Isotonic calibration on both.
- **Sport-agnostic design**: every core table is keyed by `sport_id`; adding a sport is a config/data insert (sport row, league row, model registry row, adapter registration), not a schema or backend code change. See TDD §6.
- **External data adapters**: all third-party APIs (TheRundown for odds/scores, API-Football for football stats, BallDontLie for live NBA stats, RotoWire for NBA injuries — feature-flagged on `ROTOWIRE_API_KEY` with automatic BallDontLie fallback, SportsDataIO for Phase 2 US sports) are accessed only through a `DataSourceAdapter` ABC (`fetch_odds`, `fetch_fixtures`, `fetch_team_stats`, `fetch_injuries`). Never call a provider directly from a worker or route.
- **NBA-specific constraint**: `stats.nba.com` (`nba_api`) blocks all cloud-provider IPs (AWS/GCP/Render/Heroku) at the network level. `nba_api` is for **offline historical training data only** (run locally or on a non-cloud VPS); it must never be called from a production Celery worker. Live NBA production stats come from BallDontLie exclusively.
- **Core endpoint**: `GET /picks?min_odds=&sport_slug=&limit=` — the odds-threshold filter is the product's primary differentiating feature (TDD §4.2). It must stay fast (Redis-cached, <300ms p95).
- **Guest access**: most screens (home feed, picks, fixture detail, global history) are usable without an account; guest filter state lives in Redis (24h TTL, no PII), keyed by an anonymous UUID, and migrates into `user_preferences` on registration. Auth gating is a soft, dismissible bottom-sheet prompt — never a hard block on core content.
- **Video features are legally constrained**: only Highlightly-licensed short clips (Option A, MVP) and an internally-computed animated match tracker with no video (Option B, Phase 2) are in scope. Live game video streaming of any real league is permanently out of scope — do not implement or suggest unlicensed stream embeds.

### Repository layout (TDD §9)

```
backend/app/{auth,fixtures,odds,picks,predictions,history,sports,users,core,workers,adapters,models_ml}/  # exists
backend/alembic/            # exists — DB migrations
docker-compose.yml          # exists — local Postgres 16 + Redis 7 (dev-only, not in TDD §9)
mobile/app/                 # exists — Expo Router file-based routes: (tabs)/, fixture/[id], history/,
                             # how-it-works/, auth/ (see "Mobile implementation status" above)
mobile/components/          # exists — fixtures/ (FixtureCard, LiveBadge), SportFilterChips, GuestBanner
mobile/lib/, mobile/store/  # exists — API client + per-resource modules, tokenStore, Zustand stores
ml/training/                 # exists — collect_nba_data.py, train_nba.py, compute_key_players.py
                             # (NBA) + compute_football_key_players.py, collect_football_data.py,
                             # train_football.py (football, EPL-scoped) — see "ML training" below
ml/data/, ml/artifacts/, ml/mlruns/  # exists — parquet cache, joblib artefacts, local MLflow store
ml/notebooks/, ml/evaluation/  # not yet created (TDD §9 lists these; training/ came first)
infra/{render.yaml,aws/}    # not yet created
.github/workflows/          # not yet created
```

### ML training (`ml/training/`)

The only trained model so far is NBA's, produced by a three-script pipeline. All three scripts add `backend/` to `sys.path` themselves and explicitly `load_dotenv(BACKEND_DIR / ".env")` (see the `.env`-resolution gotcha above) — run from the repo root:

```bash
backend/.venv/Scripts/python ml/training/compute_key_players.py # Stage 1 of the Big3/Top5
                                                                 # feature (TDD §3.3): ranks
                                                                 # each team's Top 5 by
                                                                 # trailing WS/48 approximation,
                                                                 # writes team_key_players for
                                                                 # all 6 seasons. Run this
                                                                 # before collect_nba_data.py/
                                                                 # train_nba.py, or the 4 new
                                                                 # key-player features train on
                                                                 # an empty table.
backend/.venv/Scripts/python ml/training/collect_nba_data.py   # ~14.5k game-log rows + ~154k
                                                                 # player-game-log rows (6
                                                                 # seasons, nba_api) + a bounded
                                                                 # real odds sample (60 dates,
                                                                 # TheRundown) — caches to
                                                                 # ml/data/*.parquet, skips
                                                                 # re-fetching whichever parquet
                                                                 # files already exist
backend/.venv/Scripts/python ml/training/train_nba.py          # Optuna (50 trials) -> isotonic
                                                                 # calibration -> joblib artefact
                                                                 # under ml/artifacts/ -> MLflow
                                                                 # run under ml/mlruns/ -> a real
                                                                 # models_registry row
```

Real results from the last run (temporal split: train 2020-21..2023-24, validate 2024-25, test 2025-26, now with 16 features including key-player availability): **68.57% test accuracy vs. a 55.43% "always pick home" baseline**, Brier/RPS 0.2002, and a +20.9% flat-stake ROI on the 29 test-set games where a real bookmaker price existed and the model favoured home (small sample — directional, not a statistically robust claim; not a rigorously isolated before/after comparison against the prior 12-feature run, since the underlying data window shifted too).

`app/models_ml/nba_features.py` is the single source of truth for the feature vector — `assemble_from_game_log` (training, from the cached parquet) and `assemble_from_live_db` (serving, from `TeamFeatures` + a live H2H call + the `Odds` table) must stay in lock-step, or the model sees different inputs live than it was trained on. See that module's docstring for the full 16-feature list and why it's 16, not TDD §3.3's 13.

`train_nba.py`'s `historical_key_player_availability` builds a backtest label for the 4 key-player features from box-score presence (`index_played_names` pre-indexes the ~154k-row player-game-log into a `dict[(game_id, team_abbr), set[names]]` once — calling it per-row against the raw DataFrame instead was a real perf bug caught during this work, hanging the script past 5 minutes). This label is intentionally **not** the same code path as live Stage 2 (`get_key_player_availability`, `player_injury_status`-only) — see the Big3/Top5 section above.

Windows-specific gotcha: `train_nba.py`'s `main()` must stay a single `asyncio.run(main_async())` wrapping the *entire* script. Two separate `asyncio.run()` calls in one process (e.g. one early for loading `team_key_players`, another later for `models_registry` registration) corrupt the shared `async_session_factory`/engine on this platform (`AttributeError: 'NoneType' object has no attribute 'send'`) — same root cause as the `tests/conftest.py` engine-dispose fixture below.

**Football training pipeline** (EPL + Brasileirão pooled — see "Football model + key-player availability" above for the full scope/feature-set writeup), same three-script shape:

```bash
backend/.venv/Scripts/python ml/training/compute_football_key_players.py # Stage 1: ranks each
                                                                          # EPL AND Brasileirão
                                                                          # team's Top 5 by real
                                                                          # games.rating, writes
                                                                          # team_key_players for
                                                                          # 5 historical seasons
                                                                          # each (2021-2025), plus
                                                                          # Brasileirão's current
                                                                          # season 2026
backend/.venv/Scripts/python ml/training/collect_football_data.py       # EPL: 3,800 game-log rows,
                                                                          # 40,760 lineup-presence
                                                                          # rows, 378 usable odds
                                                                          # rows. Brasileirão (new):
                                                                          # 3,800 game-log rows,
                                                                          # 42,971 lineup-presence
                                                                          # rows, 0 odds (TheRundown
                                                                          # has no Brazil coverage)
                                                                          # -> per-league-suffixed
                                                                          # ml/data/football_*_{league}.parquet
backend/.venv/Scripts/python ml/training/train_football.py              # pools both leagues'
                                                                          # parquet -> Poisson xG
                                                                          # regressors -> multiclass
                                                                          # classifier -> per-class
                                                                          # isotonic calibration ->
                                                                          # joblib artefact -> MLflow
                                                                          # run -> a real
                                                                          # models_registry row
```

**Current model: `football_xgb_v20260810070503`** (superseding `v20260809224336`, which had the
identical 1X2 pipeline — only the corners regressors differ, see the corners note below).
**Pooled across 18 leagues** (the 9 original +
the 9 Tier-1 additions — `allsvenskan`, `eliteserien`, `veikkausliiga`, `ekstraklasa`,
`denmark_superliga`, `liga_i`, `j1_league`, `czech_first`, `austria_bundesliga`). Temporal
split train 2021-22..2023-24 / validate 2024-25 / test 2025-26, 27,232 examples (16,287 /
5,458 / 5,487), **18** features. Real result: **48.57% test accuracy vs. a 44.43% "always pick
home" baseline** (+4.14pp), RPS **0.2144**, corners MAE **2.167**, flat-stake ROI **-6.4%**
on n=38 — still no demonstrated betting edge, and n=38 is far too small to claim one either way.

**Compare the gap over each run's OWN baseline, never the headline accuracy.** Changing the
league pool changes the test set, so 0.4916 (9 leagues) and 0.4857 (18) are scores on different
exam papers. The gap held (+4.09pp → +4.14pp) and the real gain was elsewhere: the under-3.5
reliability buckets became **monotonic for the first time** (.585/.715/.693/.733 →
.604/.673/.716/.755, trend z +3.35 → +5.92). Under 9 leagues the model's own ordering did not
survive contact with the data — fixtures it rated 0.6-0.7 outscored ones it rated 0.7-0.8 —
which is the defect that matters for a product that asks users to bet on that ordering.
Calibration was already ~0 beforehand and stayed there, so this is a discrimination gain, the
constraint repeatedly identified as binding on this market.

Earlier lineage, for reference: EPL+Brasileirão only scored 47.89% vs 46.45% with RPS 0.2138.

**The nine Tier-1 leagues are now SERVED as well as trained, and the gap between those two is
a trap worth knowing.** `ml/training/train_football.py`'s `LEAGUES` and
`app/adapters/api_football.py`'s `LEAGUE_IDS` are **separate wirings**. The retrain pooled 18
leagues while `LEAGUE_IDS` still listed 9, so the model had learned from leagues the app
ingested nothing for — no fixture, no odds, no prediction, nothing a user could ever see.
Nothing failed; the work simply never reached anybody.
`backend/tests/test_train_serve_league_parity.py` now pins it (parsing `LEAGUES` via `ast`, so
the backend suite needn't import xgboost/mlflow/optuna). **One direction only** — everything
trained must be served, but served-but-untrained is legitimate, since one model serves the
whole sport (Scottish Prem/MLS/CSL were served by an EPL/Brasileirão-trained model for weeks).
- **Season conventions were confirmed per league, never assumed** — assuming Europe's Aug-May
  shape is precisely the Brasileirão bug. Derived at **zero API cost** from the real match
  dates already sitting in `ml/data/football_game_log_*.parquet`: Allsvenskan (2025-03-29 to
  2025-11-29), Eliteserien, Veikkausliiga and the J1 League (2025-02-14 to 2025-12-06) all open
  and close inside one calendar year and join `CALENDAR_YEAR_SEASON_LEAGUES`; Ekstraklasa,
  Danish Superliga, Liga I, Czech First and Austrian Bundesliga run Aug-May and stay out.
- **TheRundown carries only the J1 League** (`sport_id` 19) of the nine; the other eight get
  odds from API-Football alone, via the per-adapter `ValueError` isolation Brasileirão
  established. Added cost ≈ 5k calls/day, ~6% of Ultra's 75,000 (odds runs every 6 hours, not
  every 5 minutes — check the real beat schedule before estimating this).

**Counting `celery` processes does NOT tell you how many schedulers are running — a claim
this document previously got wrong.** A `celery worker` or `celery beat` launched once shows up
as **TWO** python PIDs on Windows: the `.venv` launcher and the child it spawns. Verified by
starting exactly one of each and finding 4 PIDs, then checking `ParentProcessId` — each pair is
parent→child. An earlier pass here asserted "2 beats are running, so every scheduled task fires
twice against a metered API"; that was **wrong**, inferred from a raw process count without
checking parentage, and it is corrected rather than deleted because the wrong inference is the
part worth not repeating. Always check parentage before concluding anything from a count:

    Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
      Where-Object { $_.CommandLine -match 'celery' } |
      Select-Object ProcessId, ParentProcessId

**The real trap, hit again the same session, is the stale worker.** Celery worker and beat were
started at 00:11:00; `api_football.py` gained the nine new `LEAGUE_IDS` entries at 00:32:15.
Every scheduled `ingest_odds`/`ingest_fixtures` run afterwards used the OLD nine-league map, so
the new leagues silently got no odds at all — `LEAGUE_IDS.get(league)` returned `None`, raised
`ValueError`, and the existing per-adapter isolation swallowed it as "no coverage". Calling
`_ingest_odds_for_league` directly ingested 214 rows immediately; the scheduled path ingested
zero. **Neither Celery nor `uvicorn --reload` reliably picks up code changes — restart both
after touching an adapter, and be aware `--reload`'s child can outlive a killed parent and keep
serving stale code from the port.**

**Tooling now exists for this, and it should be used rather than rediscovered** — the failure
above recurred twice more after being documented, so documentation alone demonstrably does not
prevent it:
- `scripts/dev_worker.py` (and `--beat`) runs the worker/beat with restart-on-change. Start
  them this way rather than with a bare `celery` command; a hand-started process has nothing
  protecting it.
- `scripts/check_stale.py` compares every long-lived process against the files on disk and
  exits non-zero if any is stale, so it can gate a verification step instead of being read by
  eye. **It covers beat as of 2026-08-11** — previously only the worker published a
  fingerprint, so a stale SCHEDULER reported "ok". That blind spot was where the damage was:
  `snapshot_picks.py` and its `beat_schedule` entry were written three hours after beat was
  launched, so the running scheduler held a schedule with no snapshot entry and never
  dispatched it. Nothing errored. A stale WORKER applies old logic to work it receives; a
  stale BEAT never dispatches the work at all, which is quieter and worse. Immediately after
  it was wired, this same check caught `uvicorn --reload` silently stale again.
- `backend/tests/test_beat_liveness.py` asserts every scheduled task belongs to a module the
  worker actually imports (checked against `include`, since `celery_app.tasks` is empty in a
  plain pytest process), and pins the snapshot entry by name.

**Sentry now covers the worker and beat, not just the API** (`app/core/observability.py`,
2026-08-11). It had only ever been initialised in `create_app()`, which is backwards for this
project: the API fails loudly in front of a user, while every silent failure listed above
happened in a worker and was unreportable. `monitor_beat_tasks=True` (beat only) registers each
`beat_schedule` entry as a Sentry cron monitor, so a scheduled task that *stops arriving* is
alerted on — the one failure ordinary error reporting cannot catch, since it produces no
exception, no failed task and no log line. Events carry `component` and the loaded
`code_fingerprint`, so a stack trace that cannot be reproduced from current source is
identifiable as a stale process at a glance. **No `SENTRY_DSN_BACKEND` is provisioned yet**, so
this is real and inert locally — same "correct code, no live credential" status as
RotoWire/Expo push.

A saturated rate limit poisons diagnosis in the same way: a background collection run using the
450/minute budget makes an unrelated probe return empty `response` lists, which reads exactly
like "this league has no data" (the false negative that nearly wrote off Liga I and the J1
League). Check for a running collection before believing an empty response.

The Tier-1 leagues originally carried a game log and real xG but **no corners and no lineups** —
`_load_optional` tolerates both absences (those rows score as missing, which XGBoost handles).
Their corners have **since been collected** (14,390 rows across 7,195 fixtures, 35-71% coverage
per league — Veikkausliiga is genuinely thinnest at 35%, left as a real gap, not zero-filled).
Lineups are still absent and deliberately so: the key-player features they would feed were
pruned in `d0a24d9`, so collecting them would repeat the ~10,500-call mistake described above.

**`football_xgb_v20260810070503` is the current model** — the first run whose corners regressors
actually see those nine leagues. Before it, the corners boosters were **byte-identical** across
the 9- and 18-league artefacts (verified by hashing them) because those leagues contributed
zero corners rows. After it the hashes invert exactly as they should: `corners_home_model`/
`corners_away_model` **changed**, `layer1_home_model`/`layer1_away_model`/`layer2_model` are
**byte-identical**. That is the proof that 1X2 accuracy/RPS being unchanged (0.4857/0.2144) is
correct scoping rather than a silently-broken run — the same "unchanged metric after adding
data" signature as the pruned key-player features, but this time checked rather than assumed.

**Collecting current-season data does NOT reach the model until the split windows advance —
this cost a full no-op retrain on 2026-08-10.** `train_football.py`'s split is
`TRAIN=[2021,2022,2023] / VAL=2024 / TEST=2025`, and **any season outside those three windows
is silently dropped**: examples are assembled for it and then matched by no split. After
collecting 2026 history for nine leagues the example count rose 27,232 → 27,914 and the
retrain produced a model whose every booster hashed **byte-identical** to the previous one.
The collection was not wasted — it was what fixed retrodiction, which reads the game log
directly rather than through these windows, and it is what exposed the Elo bug below — but it
cannot influence training yet. Advancing the windows is deliberately **not** done yet: season
2026 holds 1,344 rows against ~10,900 for every completed season, so promoting a ~12%-complete
season to TEST would turn every headline metric into a small-sample number while still looking
like a like-for-like comparison. Revisit when 2026 is substantially complete, and move all
three windows together.

**Do not read corners MAE 2.167 → 2.157 as an improvement.** The corners *test set* grew from
2,291 to 3,731 rows (training 6,521 → 10,877) precisely because those leagues now contribute,
so the two numbers score different exams — the identical trap as comparing headline accuracy
across league pools. What genuinely improved is trustworthiness: the MAE is now measured on 63%
more real fixtures and trained on 67% more.

`ml/training/collect_football_data.py`'s lineup collection (`/fixtures/players`, 1 call per fixture — no bulk-by-league-and-date equivalent exists for lineups the way `/injuries` has) hit real `429`s under sustained load (roughly every ~100-150 calls, both for EPL originally and again for the new Brasileirão run); the same retry-with-backoff pattern as `collect_nba_data.py`'s odds pull handled these automatically. `ml/training/compute_football_key_players.py`'s own `/players` pagination calls hit the same real `429`s partway through Brasileirão's historical backfill (no retry logic existed there originally, unlike the fixtures/lineups script) — fixed by adding the same `_get_with_retry` backoff pattern; the run was resumed from where it stopped rather than restarted from scratch, since the underlying Stage 1 write is already idempotent (delete-then-insert per team+season).

`historical_key_player_availability` (the leakage-guard-required backtest label, built from real per-fixture lineup/appearance presence) now lives in a shared `app/models_ml/historical_key_players.py` module (previously duplicated between `train_football.py` and, before this pass, not used by retrodiction at all) — intentionally **not** the same code path as live Stage 2, same separation as NBA's `train_nba.py` counterpart, guarded by `backend/tests/test_football_key_players.py::test_stage2_follows_injury_status_not_lineup_presence`.

### Backend tooling (real — see backend/requirements.txt, pyproject.toml)

Local venv on **Python 3.11** (3.12 targeted by the TDD/Dockerfile isn't installed on this machine; nothing in the code needs 3.12-only syntax, so this is a deliberate, tracked mismatch — not an oversight):

```bash
py -3.11 -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/requirements.txt

docker compose up -d                 # Postgres 16 + Redis 7
cd backend && alembic upgrade head    # apply migrations
PYTHONPATH=. python scripts/seed_sports.py   # one-time: inserts nba + football Sport/League rows

uvicorn app.main:app --reload         # then GET /health, /docs — LOOPBACK ONLY
uvicorn app.main:app --reload --host 0.0.0.0   # required if mobile/.env points at a LAN IP
pytest
ruff check . && black --check .

# Required for ANY of TDD §2.3's scheduled ingestion (odds/live-scores every 5 min, injuries
# every 30 min, fixtures daily at 02:00 UTC) to actually happen automatically — see the
# "Celery worker/beat" note below for why this was silently never running.
celery -A app.workers.celery worker --loglevel=info --pool=solo   # Windows: no prefork pool
celery -A app.workers.celery beat --loglevel=info                 # separate process, same app
```

Note the `PYTHONPATH=.` on the seed script — run directly (`python scripts/seed_sports.py`) it fails with `ModuleNotFoundError: No module named 'app'`, since a script's own directory (`scripts/`), not the cwd, becomes `sys.path[0]`.

**The suite runs against a DEDICATED database, `sportiq_test`, created and migrated
automatically.** It used to run against the dev database and left real damage there: 164 user
rows carrying a fixture push token (a broad `notify_users` run would have fired 164 sends that
could only fail), fake `Sport` rows that reached the app's own dropdown, and thousands of
exploratory fixtures. Several tests still carry explicit teardown written solely because of
that; it can go whenever someone touches them.
- `tests/conftest.py` derives `<configured database>_test` and sets `DATABASE_URL` **before any
  `app.` import**. That ordering is load-bearing: `app/core/database.py` builds its engine at
  import time from an `lru_cache`d `get_settings()`, so the first call decides the database for
  the entire session. Anything imported above the redirect pins the dev database permanently.
- **It refuses to start if the test URL and the application URL resolve to the same database**,
  which is the property that actually prevents recurrence. Override with `TEST_DATABASE_URL`.
- The database is created via `asyncpg` (already a dependency, so no new one) on its own
  connection, then migrated with `alembic upgrade head` **in a subprocess** so alembic's event
  loop cannot collide with pytest-asyncio's. Migrations rather than `create_all`, because this
  schema carries enum values added by `ALTER TYPE` in autocommit blocks (`FixtureStatus.
  POSTPONED`, `InjurySource.API_FOOTBALL`) — `create_all` would build those from the current
  Python definition and hide a migration that fails to reproduce them.
- Verified by running the full suite twice: the dev database stayed at exactly 1,454 users
  while the test database went 21 → 42.

`tests/conftest.py` disposes `app.core.database`'s shared async engine after every test — without it, DB-touching tests fail intermittently (`AttributeError: 'NoneType' object has no attribute 'send'` on Windows, or `RuntimeError: ... attached to a different loop` elsewhere) because that engine is a module-level singleton bound to whichever event loop first used it, and pytest-asyncio gives each test its own loop by default. If a new DB-touching test starts failing this way, check that fixture is still wired up before assuming it's a real bug.

**Celery worker/beat were never actually running in local dev — the exact same bug as the paragraph above, just never hit until this session**, found via the user's own report: a completed real fixture (Coritiba vs Cruzeiro) was still showing its pre-game prediction badge instead of a win/loss verdict hours after kickoff. `celery_app.conf.beat_schedule` (`app/workers/celery.py`) has always been complete and correct — `ingest_live_scores`/`ingest_odds` every 5 minutes, `ingest_injuries` every 30, `ingest_fixtures` daily — but no Celery worker or beat process had ever actually been started in this dev environment (confirmed live: zero `celery` processes running, and this section's own dev commands never listed them before now), so nothing had ever executed that schedule; every prior "verified live" ingestion in this document was a manual one-off script invocation, not the real scheduled path. Starting a real worker exposed a second, previously-latent bug: every task entrypoint (`ingest_fixtures`/`ingest_odds`/`ingest_live_scores`/`ingest_injuries`/`run_predictions`/`notify_users`/`backfill_predictions`) wrapped its async body in a bare `asyncio.run(...)` — harmless on Linux's default `prefork` pool (a fresh OS process per task), but Windows has no `prefork` at all, so `--pool=solo`/`--pool=threads` run every task in ONE long-lived process, and the SECOND task crashed with the identical `AttributeError: 'NoneType' object has no attribute 'send'` from the paragraph above — `app.core.database.engine` is a module-level singleton, and `asyncio.run()` tore down the first task's loop out from under its connections. Fixed with `app.workers.celery.run_task(coro)`, a shared helper every task now calls instead of `asyncio.run` directly — disposes `engine`/`app.core.redis._pool` on the same loop right before it closes, the identical fix `tests/conftest.py` already applies at the pytest boundary, just applied at the Celery task boundary instead. Regression-tested in `backend/tests/test_run_task.py` by calling the real synchronous `run_task` twice in one process (confirmed this fails with the exact live crash on the pre-fix bare-`asyncio.run` pattern). **Verified live end-to-end**: started a real worker + beat, manually enqueued `ingest_live_scores` through the actual Celery broker (not a direct function call), confirmed it settled the real Coritiba-Cruzeiro fixture (final score 0-1) and every other due fixture, and left both processes running so the 5-minute schedule now genuinely executes on its own going forward — this is a local-dev-process gap, not a deploy gap: Render.com's documented plan already includes a Celery worker and Celery beat as separate services (see "Intended architecture" above), so production was never at risk of this specific "nothing is running" failure mode, only this machine's dev setup.

**A real, separate bug found via the same session's follow-up report** ("14 MLS games... no prediction made at all; also same Sunday 2 scottish premiership games no prediction... Is this based on the prediction probability or a bug?"): confirmed via a direct DB query it was a real bug, not the 60%/1.50 threshold — only the ONE fixture manually spot-checked per league during the 3-leagues-addition work (see above) had ever gotten a real `Prediction` row; every other upcoming fixture in Scottish Premiership/MLS/CSL had none at all (6/6 EPL fixtures had one, for comparison — from an earlier one-off batch run, not any recurring mechanism). Root cause: `run_predictions` was ONLY ever triggered by `ingest_injuries.py`'s re-inference path (a real key-player status change within 3 hours of kickoff) — there was no step anywhere that generates an ordinary fixture's *first* prediction when it's newly ingested; every previously-"working" league had only ever gotten predictions from a manual one-off script call, never a real automatic path. Fixed in `ingest_fixtures.py`'s existing upcoming-fixtures loop: after `TeamFeatures` are written, any fixture with no `Prediction` row yet now gets `run_predictions.delay(...)` queued — deliberately gated on "no prediction exists at all", not re-queued every daily re-run, so this doesn't waste real H2H/moneyline API calls recomputing a prediction whose underlying features haven't meaningfully changed. Regression-tested in `backend/tests/test_ingest_fixtures.py::test_ingest_queues_a_prediction_for_a_fixture_that_has_none_yet` (queues once for a fresh fixture, confirmed does NOT re-queue once a real Prediction row exists). **Verified live**: re-ran ingestion across every football league with the now-running worker (see above) actually processing the queue in real time — Scottish Premiership went from 1/6 to 6/6 fixtures with a real prediction, MLS from 1/15 to 15/15, CSL from 1/9 to 9/9, confirmed via `GET /fixtures` that Sunday's MLS/Scottish Premiership fixtures the user specifically flagged now all carry a real `best_pick`.

### Not yet configured

- Mobile tests/lint: `Jest`, `ESLint` — not configured yet, though `mobile/` itself now exists (see "Mobile implementation status" above) and its `tsc` typecheck does run in CI (see below).
- Mobile build/release: EAS Build/Update/Submit — no `eas.json`/EAS project set up yet; only tested via `expo start --web`/`--android` so far, not a real EAS build. This is also why real push notification tokens can't be minted yet (see "Mobile implementation status").
- Deploy: Render.com for MVP (FastAPI web service, Celery worker, Celery beat, managed Postgres/Redis); AWS ECS/RDS/ElastiCache at Phase 2 scale — no `infra/` yet.

Check for the relevant files before assuming any of the above exists — this section will go stale the moment it's scaffolded.

### CI (`.github/workflows/ci.yml`)

Two jobs, both real:
- **backend**: Postgres 16 + Redis 7 as GitHub Actions services (matching `app/core/config.py`'s defaults exactly — `sportiq_user`/`password`/`sportiq` on the standard ports — so no env vars need setting in CI), Python 3.11 (matching the real local dev venv, not the TDD's 3.12), `alembic upgrade head`, then `ruff check .`, `black --check .`, `pytest`. All backend tests mock third-party HTTP calls (`httpx.MockTransport` / mocking the `exponent_server_sdk.PushClient` call itself) rather than hitting real external APIs, so none of the unprovisioned API keys block CI.
- **mobile**: Node 20, `npm ci`, `npx tsc --noEmit`. No Jest/ESLint step since neither is configured yet (see above) — this typecheck is genuinely the only automated mobile check that exists right now, not a placeholder standing in for a fuller suite.

Runs on push to `main` and on every PR. Real-verified via an actual GitHub Actions run (`gh run watch`) — and it caught a genuine bug on the first push: every auth-touching test failed with `jwt.exceptions.InvalidKeyError: HMAC key must not be empty`. `app/core/config.py`'s `jwt_secret_key` defaults to `""`, which is fine and correct (forces explicit configuration, matches the "never commit secrets" convention) — but PyJWT hard-rejects an empty HMAC key rather than just being insecure about it, and CI has no `backend/.env` (that's gitignored) the way local dev does. Fixed with a CI-only dummy `JWT_SECRET_KEY` set directly in the workflow's `env:` block — the same thing a real deployment would set via a secrets manager per TDD §4.3/§7. Second run was fully green.

## Git

This project's repo root is `C:\Users\User\IdeaProjects\SportIQ` (a proper, scoped git repo — do not run git commands expecting the repo root to be higher up in the directory tree). Remote `origin` points at the existing public repo `github.com/UsangR01/sportiq`.
