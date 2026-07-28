# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

- `SportIQ-PRD.docx` — Product Requirement Document v1.3
- `SportIQ-TDD.docx` — Technical Design Document v1.5
- `keys.docx` — **contains live API keys, a database connection string with plaintext password, Redis URL, Sentry DSNs, and an app secret key. Gitignored. Never read its contents into a commit, a generated file, or a tool call whose output could be persisted/logged. Never add it to `.gitignore` exceptions.**
- `backend/` — FastAPI backend scaffold (see "Backend implementation status" below). No `mobile/`, `ml/`, or `infra/` yet.

The architecture section below is the *intended* design per the TDD/PRD. Where the real backend now exists, prefer reading the source over the docs; the docs still lead for anything not yet built (mobile, ML training, infra).

## Backend implementation status

Real logic exists for: auth (`POST /auth/register|login|refresh`, Argon2id + JWT + rotating refresh tokens), guest sessions (`POST/PUT /guest/session`), `GET /picks` (the full TDD §4.2 algorithm — see `app/picks/service.py` for the DB-free EV/threshold/best-outcome math, unit-tested in `tests/test_picks.py`), `GET /sports`, `GET /fixtures*`, and `GET/PUT /user/preferences`. All SQLAlchemy models for the TDD §2.1 schema exist and migrate via Alembic.

**`BallDontLieAdapter` (NBA) is the one real, live-verified `DataSourceAdapter`** — `fetch_fixtures` and `fetch_team_stats` make real HTTP calls and have been run against the live API (see "External API research findings" below). Everything else in `app/adapters/` is still stubbed. Before ingestion can do anything, run `PYTHONPATH=. python scripts/seed_sports.py` once — nothing seeds a `Sport`/`League` row otherwise, so `ingest_fixtures` silently has zero sports to iterate.

Stubbed (signature + schema exist, body is `NotImplementedError`/`501`, pending real API keys or a trained model): `GET /history`, `GET /stats/model`, `fetch_odds`/`fetch_injuries` on every adapter, `TheRundownAdapter`/`APIFootballAdapter`/`RotoWireAdapter`/`SportsDataIOAdapter` entirely, `FootballModel`/`NBAModel` (`app/models_ml/`), and the model-inference parts of the Celery workers. `ingest_injuries.py` is a partial exception — its `ROTOWIRE_API_KEY`-absent/present branching and BallDontLie fallback are fully implemented per TDD §2.3, only the underlying HTTP calls are stubbed.

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

**Known follow-up, not yet fixed:** `ingest_fixtures.py`'s team-features loop calls `fetch_team_stats` once per `(fixture, team)` pair with no in-run caching or 429 backoff. BallDontLie's free tier has a tight per-minute rate limit — a batch of ~18 distinct teams reliably 429s partway through (confirmed live, see below). The fixture-upsert half of that same function is unaffected (it doesn't call `fetch_team_stats`). Needs either a per-run team-stats cache or retry-with-backoff before relying on this loop to complete a large batch unattended.

## External API research findings — ground truth, not TDD assumptions

Live-tested against the real keys in `keys.docx` (minimal, quota-conscious calls) before implementing `BallDontLieAdapter`. Keep these in mind before touching any adapter — the TDD's own assumptions about at least one of these were wrong:

- **BallDontLie**: base URL `https://api.balldontlie.io/nba/v1`. Auth header is `Authorization: <raw key>` — **no `Bearer` prefix**. `GET /games` accepts `start_date`/`end_date` (`YYYY-MM-DD`), `seasons[]`, `team_ids[]`, `per_page` (max 100); pagination is **cursor-based** (`meta.next_cursor`), not offset. Each game embeds full `home_team`/`visitor_team` objects, so no separate `/teams` call is needed. `status` has no fixed enum: `"Final"` once done, a start-time string like `"7:00 pm ET"` before tip-off, a period string (`"1st Qtr"`, `"Halftime"`, ...) while live — see `_map_status` in `app/adapters/balldontlie.py`. `/season_averages/*` and `/standings` return 401 on this key's (free) plan — not used by the current implementation.
- **API-Football**: only reachable via the direct `v3.football.api-sports.io` host with an `x-apisports-key` header — **not RapidAPI**, despite TDD §2.2 saying "API-Football (via RapidAPI)". Its Free plan (confirmed via `GET /status`: Free, 100 req/day) blocks the `next` query param and any season after 2024 (`"Free plans do not have access to this season, try from 2022 to 2024"`) — so **no real upcoming/current-season football fixtures are reachable on this plan at all**. `/teams/statistics` does work for 2022-2024 seasons and returns rich real data (form string, home/away W-D-L, goals for/against) — no `elo_rating` or xG in the response at any tier tested. This is why `BallDontLieAdapter`, not `APIFootballAdapter`, became the first real adapter.

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
mobile/app/                 # not yet created — Expo Router file-based routes: (tabs)/, fixture/[id], auth/
mobile/components/          # not yet created
mobile/lib/, mobile/store/  # not yet created
ml/{notebooks,training,evaluation}/   # not yet created
infra/{render.yaml,aws/}    # not yet created
.github/workflows/          # not yet created
```

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

### Not yet configured

- Mobile tests/lint: `Jest`, `ESLint` (via GitHub Actions on every PR) — no `mobile/` directory yet.
- Mobile build/release: EAS Build/Update/Submit — no Expo project yet.
- Deploy: Render.com for MVP (FastAPI web service, Celery worker, Celery beat, managed Postgres/Redis); AWS ECS/RDS/ElastiCache at Phase 2 scale — no `infra/` yet.
- CI (`.github/workflows/`) — none yet.

Check for the relevant files before assuming any of the above exists — this section will go stale the moment it's scaffolded.

## Git

This project's repo root is `C:\Users\User\IdeaProjects\SportIQ` (a proper, scoped git repo — do not run git commands expecting the repo root to be higher up in the directory tree). Remote `origin` points at the existing public repo `github.com/UsangR01/sportiq`.
