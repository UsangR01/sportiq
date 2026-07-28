from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.auth.router import router as auth_router
from app.core.config import get_settings
from app.core.limiter import limiter
from app.fixtures.router import router as fixtures_router
from app.history.router import router as history_router
from app.picks.router import router as picks_router
from app.sports.router import router as sports_router
from app.users.router import router as users_router


def create_app() -> FastAPI:
    settings = get_settings()

    if settings.sentry_dsn_backend:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn_backend)

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
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(picks_router)
    app.include_router(sports_router)
    app.include_router(fixtures_router)
    app.include_router(users_router)
    app.include_router(history_router)

    return app


app = create_app()
