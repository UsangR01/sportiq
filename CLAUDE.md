# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository currently contains **no application code** — only planning documents:

- `SportIQ-PRD.docx` — Product Requirement Document v1.3
- `SportIQ-TDD.docx` — Technical Design Document v1.5
- `keys.docx` — **contains live API keys, a database connection string with plaintext password, Redis URL, Sentry DSNs, and an app secret key. Gitignored. Never read its contents into a commit, a generated file, or a tool call whose output could be persisted/logged. Never add it to `.gitignore` exceptions.**

Everything below is the *intended* architecture as specified in the TDD/PRD, not yet-implemented fact. When code starts landing, update this file to describe what actually exists, and prefer reading the real source over these documents once they diverge.

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

### Planned repository layout (TDD §9 — not yet created)

```
backend/app/{auth,fixtures,odds,picks,predictions,history,sports,users,core,workers,adapters,models_ml}/
backend/alembic/            # DB migrations
mobile/app/                 # Expo Router file-based routes: (tabs)/, fixture/[id], auth/
mobile/components/          # fixtures/, picks/, tracker/, highlights/, ui/
mobile/lib/, mobile/store/  # API client + SecureStore auth / Zustand stores
ml/{notebooks,training,evaluation}/
infra/{render.yaml,aws/}
.github/workflows/          # ci.yml, deploy-backend.yml, ota-update.yml, app-release.yml
```

### Planned tooling (not yet configured — TDD §7.3, §10)

- Backend tests/lint: `pytest`, `ruff`, `black`.
- Mobile tests/lint: `Jest`, `ESLint` (via GitHub Actions on every PR).
- Mobile build/release: EAS Build (native binaries), EAS Update (OTA JS-only pushes), EAS Submit (App Store / Google Play).
- Deploy: Render.com for MVP (FastAPI web service, Celery worker, Celery beat, managed Postgres/Redis); AWS ECS/RDS/ElastiCache at Phase 2 scale.

Do not invent commands beyond these until the corresponding `package.json` / `pyproject.toml` / CI config actually exists — check for those files first, as this section will go stale the moment real tooling is scaffolded.

## Git

This project's repo root is `C:\Users\User\IdeaProjects\SportIQ` (a proper, scoped git repo — do not run git commands expecting the repo root to be higher up in the directory tree). Remote `origin` points at the existing public repo `github.com/UsangR01/sportiq`.
