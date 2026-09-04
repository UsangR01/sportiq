import logging

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

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    init_sentry(API)

    app = FastAPI(title="SportIQ API")

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.on_event("startup")
    async def _repair_serving_models() -> None:
        """Make sure every sport's active model can actually be LOADED from this image.

        THE API RUNS THIS TOO, not only the worker, and that redundancy is the point. When
        football went dark in September 2026 the API redeployed within three minutes of the
        push while the worker had not restarted forty minutes later -- so a repair that lived
        only in the worker would not have run when it was most needed. Whichever process comes
        up first fixes it; the second finds nothing to do.

        Free of Celery by construction (see app/models_ml/registry_repair.py): this process
        runs two gunicorn workers on a 512MB instance and has already been OOM-killed once by
        an import it did not need. The catch-up queueing stays on the worker side, which owns
        the broker.

        NEVER FATAL. An API that will not boot because a reconciliation failed is worse than
        the drift it was checking for.
        """
        try:
            from app.core.database import async_session_factory
            from app.models_ml.registry_repair import reconcile

            async with async_session_factory() as db:
                await reconcile(db)
        except Exception:  # pragma: no cover - startup must survive anything here
            logger.exception("serving-model reconciliation failed at API startup")

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
