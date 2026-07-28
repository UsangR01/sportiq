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

Stubbed (signature + schema exist, body is `NotImplementedError`/`501`, pending real API keys or a trained model): `GET /history`, `GET /stats/model`, all 5 `DataSourceAdapter` implementations (`app/adapters/`), `FootballModel`/`NBAModel` (`app/models_ml/`), and the parts of the Celery workers (`app/workers/`) that call those adapters/models. `ingest_injuries.py` is the exception — its `ROTOWIRE_API_KEY`-absent/present branching and BallDontLie fallback are fully implemented per TDD §2.3, only the underlying HTTP calls are stubbed.

**Known divergences from the TDD, introduced deliberately during scaffolding — check these before assuming the docs are authoritative:**
- `refresh_tokens` table and `users.expo_push_token` column exist in code but aren't in the TDD §2.1 schema listing (the TDD's own prose requires both — §4.3, §5.4).
- `AdapterFactory`/`ingest_fixtures.py` resolve a provider by `sport.slug` (football/nba/nfl/nhl/mlb), not by a `sports.data_source_slug` column — TDD §6.2 references that column but §2.1's schema doesn't define it.
- `fixtures.status` only has `scheduled|live|completed` (per the TDD §2.1 enum) even though TDD §2.3 talks about "cancelled/postponed" fixtures — no such enum values exist yet.
- Live score ingestion (`ingest_live_scores.py`) has no adapter method to call: the TDD's `DataSourceAdapter` ABC (§2.2) only defines `fetch_odds`/`fetch_fixtures`/`fetch_team_stats`/`fetch_injuries`, none of which map to TheRundown's scores endpoint. The upsert/transition logic is written but unreachable until this is resolved.
- No `watchlist` table exists, so the T-60-minute kickoff push reminder (TDD §5.4) can't be implemented yet — PICK-07 "save to watchlist" is a Phase 2 Could-Have in the PRD.
- Confidence-tier (`High`/`Medium`/`Low`) numeric thresholds are provisional guesses in `app/predictions/service.py` — neither doc defines them.

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

uvicorn app.main:app --reload         # then GET /health, /docs
pytest
ruff check . && black --check .
```

### Not yet configured

- Mobile tests/lint: `Jest`, `ESLint` (via GitHub Actions on every PR) — no `mobile/` directory yet.
- Mobile build/release: EAS Build/Update/Submit — no Expo project yet.
- Deploy: Render.com for MVP (FastAPI web service, Celery worker, Celery beat, managed Postgres/Redis); AWS ECS/RDS/ElastiCache at Phase 2 scale — no `infra/` yet.
- CI (`.github/workflows/`) — none yet.

Check for the relevant files before assuming any of the above exists — this section will go stale the moment it's scaffolded.

## Git

This project's repo root is `C:\Users\User\IdeaProjects\SportIQ` (a proper, scoped git repo — do not run git commands expecting the repo root to be higher up in the directory tree). Remote `origin` points at the existing public repo `github.com/UsangR01/sportiq`.
