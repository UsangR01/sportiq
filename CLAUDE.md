# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

- `SportIQ-PRD.docx` — Product Requirement Document v1.3
- `SportIQ-TDD.docx` — Technical Design Document v1.5
- `keys.docx` — **contains live API keys, a database connection string with plaintext password, Redis URL, Sentry DSNs, and an app secret key. Gitignored. Never read its contents into a commit, a generated file, or a tool call whose output could be persisted/logged. Never add it to `.gitignore` exceptions.**
- `backend/` — FastAPI backend scaffold (see "Backend implementation status" below).
- `ml/` — NBA model training pipeline (see "ML training" below).
- `mobile/` — Expo/React Native app scaffold (see "Mobile implementation status" below). No `infra/` yet.

The architecture section below is the *intended* design per the TDD/PRD. Where the real backend/mobile app now exist, prefer reading the source over the docs; the docs still lead for anything not yet built (ML training beyond NBA, infra).

## Backend implementation status

Real logic exists for: auth (`POST /auth/register|login|refresh`, Argon2id + JWT + rotating refresh tokens), guest sessions (`POST/PUT /guest/session`), `GET /picks` (the full TDD §4.2 algorithm — see `app/picks/service.py` for the DB-free EV/threshold/best-outcome math, unit-tested in `tests/test_picks.py`), `GET /sports`, `GET /fixtures*`, and `GET/PUT /user/preferences`. All SQLAlchemy models for the TDD §2.1 schema exist and migrate via Alembic.

**Two real, live-verified `DataSourceAdapter` methods exist**: `BallDontLieAdapter.fetch_fixtures`/`.fetch_team_stats` (NBA) and `TheRundownAdapter.fetch_odds` (all sports — odds is always TheRundown, per TDD §6.2). All make real HTTP calls and have been run against the live API end to end, including real odds landing against a real fixture (see "External API research findings" below). Everything else in `app/adapters/` is still stubbed. Before ingestion can do anything, run `PYTHONPATH=. python scripts/seed_sports.py` once — nothing seeds a `Sport`/`League` row otherwise, so `ingest_fixtures`/`ingest_odds` silently have zero sports to iterate.

**`NBAModel` is a real, trained model** — see "ML training" below. `run_predictions.py` produces genuine `Prediction` rows (verified against the real Pistons-vs-Suns fixture: home_prob 0.565, confidence MEDIUM); `GET /fixtures/{id}` shows real odds and a real prediction joined together. `/picks` itself still won't show that particular fixture — it's `completed` with a January kickoff (a historical fixture kept around for cross-task verification), and `/picks` correctly only returns `scheduled` fixtures in the next 7 days; that's the filter working, not a gap.

**Big3/Top5 key player availability (TDD §3.3, §2.1) is real and fully wired**, in two stages that must never be merged into one query (see `app/models_ml/nba_key_players.py`'s module docstring):
- **Stage 1** (season-level, backward-looking): `ml/training/compute_key_players.py` ranks each team's players by a trailing WS/48 approximation among a 26+ MPG pool (falling back to the 18-26 MPG band if fewer than 5 qualify), writing the Top 5 to `team_key_players` once per team/season. Re-runnable/idempotent (delete-then-insert per team+season). Run for real across all 6 seasons — e.g. Detroit Pistons 2025: Jalen Duren, Cade Cunningham, Tobias Harris, Ausar Thompson, Duncan Robinson.
- **Stage 2** (pre-game, forward-looking, live production): `get_key_player_availability(db, team_id, season_year)` reads **only** `player_injury_status` (joined to `team_key_players` by player name, case-insensitive — the two tables share no ID space, same cross-provider mismatch pattern as fixtures/odds) and writes `TeamFeatures.key_players_available`/`.key_players_per_combined`. A player with zero injury-status rows at all counts as *available* (not on any injury report is itself informative); only a team with zero `team_key_players` rows for the season produces `(None, None)`. Computed at ingest time (`ingest_fixtures.py`) and again whenever the re-inference trigger fires.
- **Re-inference trigger** (`ingest_injuries.py`) now checks `team_key_players` membership by name (not a salary-rank proxy) *and* an actual per-fixture 3-hour-before-tip-off window (`Fixture.kickoff_utc.between(now, now+3h)`, previously just "any fixture today") *and* actually dispatches `run_predictions.delay(...)` (previously logged only — dead code from before a trained model existed).
- **The historical-training-label counterpart is a deliberately separate, non-reusable function**: `ml/training/train_nba.py`'s `historical_key_player_availability` derives an availability label from **completed-game box-score presence** (`nba_api` player-game-logs, cached via `collect_nba_data.py`'s `collect_player_game_log`) — fine for backtest labels, explicitly documented as **never** to be imported into the live Stage 2 path (and kept in `train_nba.py`, outside `nba_key_players.py`, specifically so it can't be accidentally imported live). `backend/tests/test_nba_key_players.py::test_stage2_follows_injury_status_not_box_score` asserts the two diverge on the same underlying facts (a player marked `OUT` in `player_injury_status` who nonetheless has real minutes in the box score) — this is the regression guard against Stage 2 ever drifting into target leakage.
- `ws_48`/`per` are **explicitly simplified, documented approximations** (a PIE-based per-48 value formula; a Hollinger-style uPER rescaled to PER's 15.0 league-average convention) — not bit-exact Basketball-Reference/Hollinger reproductions.
- Manually verified end-to-end with a synthetic `player_injury_status` row (no real RotoWire/BallDontLie injury access — see below): marking a real Top-5 player `OUT` dropped `key_players_available` 5→4, flowed through `_maybe_trigger_reinference` into `TeamFeatures`, and changed the feature vector `_run_predictions` actually saw.
- A pre-existing, never-before-exercised bug was found and fixed while rebuilding `ingest_injuries.py`: `PlayerInjuryStatus(team_id=update.team_external_id, ...)` was assigning the injury provider's own external team ID directly into the internal UUID FK column (same bug class as earlier fixture/odds work) — fixed via a new `_resolve_team` helper that resolves through `Team.external_id` first. Never triggered before because both injury adapters (`RotoWireAdapter`, `BallDontLieAdapter.fetch_injuries`) were still `NotImplementedError` stubs.
- **TDD §7's NBA cost-breakdown table is stale** — it still describes a "salary-weighted historical injury-impact proxy," language from before this feature replaced that design. Flagging it, not editing the TDD.

Stubbed (signature + schema exist, body is `NotImplementedError`/`501`, pending real API keys or a trained model): `GET /history`, `GET /stats/model`, `fetch_odds` on every adapter except TheRundown, `fetch_injuries` on every adapter (`RotoWireAdapter` and `BallDontLieAdapter.fetch_injuries` — live-tested: BallDontLie's `/nba/v1/player_injuries` 401s on this key's plan, same paid-tier gate as `/season_averages`/`/standings`; no `ROTOWIRE_API_KEY` was ever provisioned), `TheRundownAdapter.fetch_fixtures`/`.fetch_team_stats`, `APIFootballAdapter`/`SportsDataIOAdapter` entirely, `FootballModel` (`app/models_ml/`), and the model-inference parts of the Celery workers for football. `ingest_injuries.py`'s re-inference trigger and DB-write logic are fully real and tested (see above) — only the underlying HTTP calls into RotoWire/BallDontLie are stubbed, so `player_injury_status` stays empty in practice until one of those exists.

**Known divergences from the TDD, introduced deliberately while building — check these before assuming the docs are authoritative:**
- `refresh_tokens` table and `users.expo_push_token` column exist in code but aren't in the TDD §2.1 schema listing (the TDD's own prose requires both — §4.3, §5.4).
- `fixtures.external_id` (indexed, unique with `sport_id`) exists in code but isn't in the TDD §2.1 schema listing either. Without it, ingest workers have no way to dedupe against a provider's own fixture ID — matching against the internal UUID PK (what the code did before this was added) can never hit, so every ingest run would insert duplicates forever.
- `FixturePayload` (`app/adapters/base.py`) carries `home_team_name`/`away_team_name`/`*_short_name` and a `status` string beyond the original TDD-derived shape — a first-seen team needs a display name to create its `Team` row, and a fixture ingested from a non-"upcoming" window (see BallDontLie note below) needs to say it's already `completed` rather than defaulting to `scheduled` forever.
- `app/fixtures/service.py:get_or_create_team` — a get-or-create-by-`(sport_id, external_id)` helper. Needed for the same reason as `fixtures.external_id`: fixture payloads carry provider IDs, not our internal UUIDs, and nothing else resolves that mapping.
- `AdapterFactory`/`ingest_fixtures.py` resolve a provider by `sport.slug` (football/nba/nfl/nhl/mlb), not by a `sports.data_source_slug` column — TDD §6.2 references that column but §2.1's schema doesn't define it.
- `fixtures.status` only has `scheduled|live|completed` (per the TDD §2.1 enum) even though TDD §2.3 talks about "cancelled/postponed" fixtures — no such enum values exist yet.
- Live score ingestion (`ingest_live_scores.py`) has no adapter method to call: the TDD's `DataSourceAdapter` ABC (§2.2) only defines `fetch_odds`/`fetch_fixtures`/`fetch_team_stats`/`fetch_injuries`, none of which map to TheRundown's scores endpoint. The upsert/transition logic is written but unreachable until this is resolved.
- No `watchlist` table exists, so the T-60-minute kickoff push reminder (TDD §5.4) can't be implemented yet — PICK-07 "save to watchlist" is a Phase 2 Could-Have in the PRD.
- Confidence-tier (`High`/`Medium`/`Low`) numeric thresholds are provisional guesses in `app/predictions/service.py` — neither doc defines them.
- `fixtures.odds_provider_external_id` (indexed, unique with `sport_id`, separate from `fixtures.external_id`) exists in code but isn't in the TDD §2.1 schema listing. The odds provider (always TheRundown) and the stats/fixtures provider for a sport (BallDontLie for NBA, API-Football for football) use **different ID spaces for the same real-world fixture** — there's no shared ID to join odds to a fixture on. Populated on first successful match via `app/fixtures/service.py:find_fixture_by_abbreviations_and_time` (team abbreviation + a kickoff-time tolerance window), then used as a fast path on later ingests.
- `DataSourceAdapter.fetch_odds`'s signature is `(sport, league, days_ahead)`, not `(fixture_ids: list[str])` as originally drafted — mirrors `fetch_fixtures`'s shape. Real odds providers are queried by sport+date-range, not by IDs the caller already knows (and, per the point above, the caller's fixture IDs aren't in the odds provider's ID space anyway). All 5 adapter stubs were updated to match; only `TheRundownAdapter`'s is implemented for real.
- `OddsPayload` (`app/adapters/base.py`) carries `home_team_short_name`/`away_team_short_name`/`kickoff_utc` beyond the original shape — needed for the fixture-matching above. `fixture_external_id` on this dataclass is the *odds* provider's event ID, not the stats provider's.
- Only the `h2h` (moneyline) market is ingested by `TheRundownAdapter.fetch_odds`. `spread`/`total` aren't mapped: the `odds` table's generic `home_odds`/`draw_odds`/`away_odds` columns model a two/three-way price, not a point-spread or an over/under line, and nothing downstream (`/picks`, the EV formula) consumes those markets yet.
- `TeamStats`/`team_features` carry a `season_point_diff` column (season-long average point differential, distinct from the last-10 `attack_str`/`defence_str`) beyond the TDD §2.1 shape — the NBA model's `net_rating_diff` feature needs a longer-window signal alongside the short-term form one, and there was nowhere to source it from.
- `app/adapters/balldontlie.py:fetch_h2h_win_rate` is a standalone function, **not** part of the `DataSourceAdapter` ABC — H2H is fixture-specific (needs both teams), not a generic per-team stat, so it doesn't fit `fetch_team_stats(team_id)`'s shape. Called directly by `app/models_ml/nba_features.py`, not through the adapter interface.
- `BaseModel.__init__` gained a second `version` parameter (`app/models_ml/base.py`) — `Prediction.model_version` was originally being set to `model.artefact_path` (a raw filesystem path) because that was the only thing `BaseModel` carried; fixed to carry the `models_registry.version` string too.
- The NBA model's feature set is **16, not TDD §3.3's 13** — see `app/models_ml/nba_features.py`'s module docstring for the full reasoning. It now includes the 4 key-player-availability features (`key_players_available_home/away`, `key_players_per_combined_home/away` — see the Big3/Top5 section above), which the TDD's own §2.1 design added later, replacing a "salary-weighted injury-impact proxy" idea. Pace differential is still omitted even though it's computable at *training* time from `nba_api`'s box-score columns — BallDontLie's live `/games` response has no shooting stats to derive it from at serving time, and a feature that's real in training but permanently `None` in production is worse than not having it. `moneyline_implied_prob_home` is included but genuinely sparse (see below).

**Known follow-ups, not yet fixed:**
- `ingest_fixtures.py`'s team-features loop now caches `fetch_team_stats` per-run and `app/adapters/balldontlie.py`'s `_get_with_retry` backs off on 429 — but BallDontLie's free tier is still tight enough that a large batch of genuinely distinct teams (not just repeats within one run) can exhaust the retry budget. Fine for NBA's 30-team universe; revisit if this pattern gets reused for a sport with far more teams.
- Fixture matching in `find_fixture_by_abbreviations_and_time` assumes team abbreviations are unique per sport and consistent across providers — confirmed true for BallDontLie vs TheRundown on NBA (both use "PHX", "DET", etc.) but not verified for any other provider pair.
- `models_registry.artefact_path` is stored as an absolute Windows path (`C:\Users\User\...`) — fine for this machine, not portable to a Linux container (the actual Render.com deploy target per TDD §7). Revisit before any real deployment — probably a relative path resolved against a configured models directory.
- The training script's flat-stake ROI metric only covers home-side picks with real odds (n=22-30 depending on the run) — the collected odds sample only captured `home_odds`, not `away_odds`, so away-side picks aren't included. A real but small-sample, directionally-positive result (+6.9% to +14.4% across two runs); not a statistically robust claim.

## Mobile implementation status

`mobile/` is a real Expo Router app (SDK 57, TypeScript), scaffolded and live-tested end to end against the running backend (registration, login, logout, guest-session creation/migration, and every §5.2 screen route) — not just created and left unverified.

**Real and wired to the live backend**: NativeWind v4 (Tailwind for RN), TanStack Query, Zustand, Expo SecureStore-backed JWT storage (`lib/tokenStore.ts` + `store/authStore.ts`), and a thin `lib/api/*.ts` client layer mirroring every real backend schema by hand (`lib/api/types.ts` — keep these in lock-step with `backend/app/*/schemas.py`, there's no shared codegen). All 9 TDD §5.2 routes exist: `(tabs)/index` (Home), `(tabs)/picks`, `(tabs)/live`, `(tabs)/profile`, `fixture/[id]`, `history/index`, `how-it-works/index`, `auth/login`, `auth/register`. Home/Picks/Live call real `GET /fixtures`/`GET /picks`/`GET /sports`; Profile calls real `GET/PUT /user/preferences` when authenticated; auth screens call real `POST /auth/register|login`; the odds-threshold slider (`@react-native-community/slider` + `expo-haptics`) drives a real `GET /picks?min_odds=` query.

**A real backend bug was found and fixed via this integration testing**: `RegisterRequest` (`app/auth/schemas.py`) has `model_config = ConfigDict(strict=True)`, and pydantic-core's strict UUID validator rejects a JSON string outright — it demands an actual `UUID` instance, which no JSON body can ever supply. Every real client sending a non-null `guest_session_id` (the TDD §2.1 guest-migration-on-register flow) hit a 422 until this was caught — fixed with a per-field `Field(strict=False)` override, regression-tested in `backend/tests/test_auth_schemas.py`. This had zero test coverage before (no prior test, curl check, or client ever exercised registration with a real, non-null guest session id) — same "first real caller finds a latent bug" pattern as the fixture/odds/injury ID-mapping bugs above.

**Deliberately deferred, not yet built** (still just `mobile/`'s own scaffold, not stubbed backend-style — there's no route/screen for these at all yet): the animated match tracker and Highlightly video embed (§5.3, both explicitly Phase-2/needs-a-license-or-WebSocket-feed anyway), push notifications (§5.4, needs `expo-notifications` + a real device/EAS build to test — meaningless on the web target used for verification), biometric login (`expo-local-authentication`), bottom sheets (`@gorhom/bottom-sheet` — the TDD's guest soft-gate is currently a plain banner + Link, not a modal sheet), and charts (Victory Native XL — the fixture-detail probability bar is a plain `View`-based bar, not a chart library). None of these packages are installed yet; add them when their screen is actually built, not preemptively.

**Real bugs found and fixed while building the scaffold itself** (both are general React Native/Expo Router gotchas, not SportIQ-specific, but cost real debugging time):
- A require cycle (`store/authStore.ts` → `lib/api/auth.ts` → `lib/api/client.ts` → `store/authStore.ts`, from the API client reading tokens out of the Zustand store directly) showed up as a Metro bundler warning. Fixed by extracting token state into `lib/tokenStore.ts` — a plain, non-React module with its own tiny pub-sub — so the API client depends on it one-directionally, and `authStore.ts` becomes a thin Zustand wrapper that subscribes to it for UI reactivity. The client's own refresh-on-401 flow writes through `tokenStore` directly (not through Zustand), which is exactly why the subscription is needed to keep the UI in sync with a rotation the UI itself didn't trigger.
- `SportFilterChips`' horizontal `ScrollView` visibly stretched to fill half the screen — but only on screens where the `FlatList` below it was empty (Picks/Live with no matching fixtures), never on Home. Root cause: RN's `ScrollView` bakes `flexGrow: 1` into its base style; two `flexGrow: 1` siblings in a column flex container split whatever leftover space exists 50/50, and an empty `FlatList` leaves a lot of leftover space where a populated one doesn't. Fixed with `grow-0` on the chips `ScrollView`'s own className (not just its content container) — a real cross-platform footgun any future horizontal-scroller-above-a-list layout in this app could hit again.

**Real, live-verified in a headless browser session (Playwright + the system's own Edge/Chromium install) against the actual running backend** — not just typechecked: Home renders real fixtures fetched from `GET /fixtures` (including the historical Pistons-vs-Suns fixture's real prediction probability bar and real BetMGM odds on `fixture/[id]`); Picks/Live correctly render their real empty states (no scheduled fixtures exist in the seed data, no fixtures are live); guest → register → authenticated Profile (real email, real `GET /user/preferences`) → logout → back to guest all round-tripped for real; `history/index` correctly surfaces the backend's real `501` as a friendly message instead of erroring. Zero console/runtime errors in the final pass.

### Mobile dev commands

```bash
cd mobile
cp .env.example .env              # EXPO_PUBLIC_API_URL — defaults to http://localhost:8000
npm install
npx expo start --web              # or --android / --ios (needs a device/emulator, untested so far)
npx tsc --noEmit                  # typecheck — no ESLint/Jest configured yet, see "Not yet configured"
```

The backend must be running (`docker compose up -d && cd backend && uvicorn app.main:app`) and its `CORS_ORIGINS` must include the Expo web dev server's origin (`http://localhost:8081` by default) — `backend/.env.example` doesn't ship this by default since it's a mobile-dev-only concern, add it to your local `backend/.env`.

## External API research findings — ground truth, not TDD assumptions

Live-tested against the real keys in `keys.docx` (minimal, quota-conscious calls) before implementing `BallDontLieAdapter`. Keep these in mind before touching any adapter — the TDD's own assumptions about at least one of these were wrong:

- **BallDontLie**: base URL `https://api.balldontlie.io/nba/v1`. Auth header is `Authorization: <raw key>` — **no `Bearer` prefix**. `GET /games` accepts `start_date`/`end_date` (`YYYY-MM-DD`), `seasons[]`, `team_ids[]`, `per_page` (max 100); pagination is **cursor-based** (`meta.next_cursor`), not offset. Each game embeds full `home_team`/`visitor_team` objects, so no separate `/teams` call is needed. `status` has no fixed enum: `"Final"` once done, a start-time string like `"7:00 pm ET"` before tip-off, a period string (`"1st Qtr"`, `"Halftime"`, ...) while live — see `_map_status` in `app/adapters/balldontlie.py`. `/season_averages/*` and `/standings` return 401 on this key's (free) plan — not used by the current implementation.
- **API-Football**: only reachable via the direct `v3.football.api-sports.io` host with an `x-apisports-key` header — **not RapidAPI**, despite TDD §2.2 saying "API-Football (via RapidAPI)". Its Free plan (confirmed via `GET /status`: Free, 100 req/day) blocks the `next` query param and any season after 2024 (`"Free plans do not have access to this season, try from 2022 to 2024"`) — so **no real upcoming/current-season football fixtures are reachable on this plan at all**. `/teams/statistics` does work for 2022-2024 seasons and returns rich real data (form string, home/away W-D-L, goals for/against) — no `elo_rating` or xG in the response at any tier tested. This is why `BallDontLieAdapter`, not `APIFootballAdapter`, became the first real adapter.
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
ml/training/                 # exists — collect_nba_data.py, train_nba.py (see "ML training" below)
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

### Backend tooling (real — see backend/requirements.txt, pyproject.toml)

Local venv on **Python 3.11** (3.12 targeted by the TDD/Dockerfile isn't installed on this machine; nothing in the code needs 3.12-only syntax, so this is a deliberate, tracked mismatch — not an oversight):

```bash
py -3.11 -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/requirements.txt

docker compose up -d                 # Postgres 16 + Redis 7
cd backend && alembic upgrade head    # apply migrations
PYTHONPATH=. python scripts/seed_sports.py   # one-time: inserts nba Sport + League rows

uvicorn app.main:app --reload         # then GET /health, /docs
pytest
ruff check . && black --check .
```

Note the `PYTHONPATH=.` on the seed script — run directly (`python scripts/seed_sports.py`) it fails with `ModuleNotFoundError: No module named 'app'`, since a script's own directory (`scripts/`), not the cwd, becomes `sys.path[0]`.

`tests/conftest.py` disposes `app.core.database`'s shared async engine after every test — without it, DB-touching tests fail intermittently (`AttributeError: 'NoneType' object has no attribute 'send'` on Windows, or `RuntimeError: ... attached to a different loop` elsewhere) because that engine is a module-level singleton bound to whichever event loop first used it, and pytest-asyncio gives each test its own loop by default. If a new DB-touching test starts failing this way, check that fixture is still wired up before assuming it's a real bug.

### Not yet configured

- Mobile tests/lint: `Jest`, `ESLint` (via GitHub Actions on every PR) — not configured yet, though `mobile/` itself now exists (see "Mobile implementation status" above).
- Mobile build/release: EAS Build/Update/Submit — no `eas.json`/EAS project set up yet; only tested via `expo start --web` so far, not a device/emulator/EAS build.
- Deploy: Render.com for MVP (FastAPI web service, Celery worker, Celery beat, managed Postgres/Redis); AWS ECS/RDS/ElastiCache at Phase 2 scale — no `infra/` yet.
- CI (`.github/workflows/`) — none yet.

Check for the relevant files before assuming any of the above exists — this section will go stale the moment it's scaffolded.

## Git

This project's repo root is `C:\Users\User\IdeaProjects\SportIQ` (a proper, scoped git repo — do not run git commands expecting the repo root to be higher up in the directory tree). Remote `origin` points at the existing public repo `github.com/UsangR01/sportiq`.
