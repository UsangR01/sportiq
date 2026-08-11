from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.auth.router import router as auth_router
from app.core.code_version import current_code_version, loaded_code_version
from app.core.config import get_settings
from app.core.limiter import limiter
from app.core.observability import API, init_sentry
from app.fixtures.router import router as fixtures_router
from app.history.router import router as history_router
from app.picks.router import router as picks_router
from app.sports.router import router as sports_router
from app.users.router import router as users_router


def create_app() -> FastAPI:
    settings = get_settings()

    init_sentry(API)

    app = FastAPI(title="SportIQ API")

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        """Liveness, plus which code this process actually loaded.

        `code.loaded` is captured at startup; `code.current` is read from disk on every call.
        When they differ, this process is serving stale code — uvicorn's --reload watcher has
        silently stopped twice in this project, and the symptom is never an error, just a fix
        that appears not to work. `stale` makes that a check rather than an inference.

        Note this endpoint does NOT touch the database, so a 200 here says nothing about
        Postgres being reachable — see scripts/check_stale.py and the runbook."""
        loaded = loaded_code_version()
        current = current_code_version()
        return {
            "status": "ok",
            "code": {
                "loaded": loaded.as_dict(),
                "current": current.as_dict(),
                "stale": loaded.fingerprint != current.fingerprint,
            },
        }

    app.include_router(auth_router)
    app.include_router(picks_router)
    app.include_router(sports_router)
    app.include_router(fixtures_router)
    app.include_router(users_router)
    app.include_router(history_router)

    return app


app = create_app()
