import pytest

from app.core.database import engine


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    """app.core.database's async engine is a module-level singleton, but pytest-asyncio gives
    each test its own event loop by default. Without disposing the pool between tests, a
    later test's loop tries to reuse connections opened under an earlier (now-closed) loop —
    asyncpg/SQLAlchemy then fail with errors like "attached to a different loop" or (on
    Windows' ProactorEventLoop) "'NoneType' object has no attribute 'send'". Disposing after
    every test is cheap (a no-op if the test never touched the DB) and is the documented fix
    for this exact SQLAlchemy-async + pytest-asyncio interaction, not specific to this repo."""
    yield
    await engine.dispose()
