import pytest

from app.core.database import engine
from app.core.redis import _pool as redis_pool


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


@pytest.fixture(autouse=True)
async def _dispose_redis_pool_after_test():
    """Same root cause as the DB engine above, for app.core.redis's module-level
    ConnectionPool: latent until a test file first made two separate async test functions
    both exercise a real Redis call (GET /picks's caching) in the same session — surfaced as
    "RuntimeError: Event loop is closed" from a later test reusing a connection opened under an
    earlier, now-closed loop."""
    yield
    await redis_pool.disconnect()
