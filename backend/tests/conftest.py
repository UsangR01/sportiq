"""Test-session setup.

THE DATABASE REDIRECT BELOW MUST RUN BEFORE ANY `app.` IMPORT. app/core/database.py builds
its engine at import time from get_settings(), and get_settings() is lru_cached, so the first
call decides which database the whole session talks to. Anything imported above the redirect
pins the dev database for good.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from dotenv import dotenv_values

BACKEND_DIR = Path(__file__).resolve().parents[1]

DEFAULT_DATABASE_URL = "postgresql+asyncpg://sportiq_user:password@localhost:5432/sportiq"
TEST_DB_SUFFIX = "_test"


def _configured_database_url() -> str:
    """The URL the app would normally use — read WITHOUT constructing Settings.

    Instantiating Settings here would populate the lru_cache with the dev URL, which is the one
    thing this module exists to prevent, so .env is read directly instead.
    """
    from_env = os.environ.get("DATABASE_URL")
    if from_env:
        return from_env
    from_dotenv = dotenv_values(BACKEND_DIR / ".env").get("DATABASE_URL")
    return from_dotenv or DEFAULT_DATABASE_URL


def _as_test_url(url: str) -> str:
    """Point the same server/credentials at `<database>_test`. Idempotent."""
    parsed = urlparse(url)
    name = parsed.path.lstrip("/")
    if name.endswith(TEST_DB_SUFFIX):
        return url
    return urlunparse(parsed._replace(path=f"/{name}{TEST_DB_SUFFIX}"))


_CONFIGURED_URL = _configured_database_url()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _as_test_url(_CONFIGURED_URL)

# The guard that makes this worth having. Until now the suite ran against the DEV database, and
# it left real damage there: 164 user rows carrying a fixture push token (a broad notify run
# would have fired 164 sends that could only fail), fake Sport rows that reached the app's own
# dropdown, and thousands of exploratory fixtures. Several tests still clean up after
# themselves with explicit teardown written specifically because of that. Refusing to start is
# better than discovering the pollution later.
if urlparse(TEST_DATABASE_URL).path == urlparse(_CONFIGURED_URL).path:
    raise RuntimeError(
        f"Refusing to run the test suite against the configured application database "
        f"({urlparse(_CONFIGURED_URL).path.lstrip('/')!r}). Set TEST_DATABASE_URL to a "
        f"dedicated database, or leave it unset to use "
        f"{urlparse(TEST_DATABASE_URL).path.lstrip('/')!r}."
    )

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Only now is it safe to import anything that reads settings.
from app.core.database import engine  # noqa: E402
from app.core.redis import _pool as redis_pool  # noqa: E402


def _server_url_for_admin(url: str) -> str:
    """Same server, but connected to `postgres` — CREATE DATABASE cannot run from inside the
    database being created."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))


def _ensure_test_database_exists() -> None:
    """asyncpg rather than psycopg — it is already a dependency, and this needs no new one.

    Its own connection, not app.core.database's engine: CREATE DATABASE cannot run inside a
    transaction block, and this must not touch the shared engine before the tests do.
    """
    import asyncio

    import asyncpg

    name = urlparse(TEST_DATABASE_URL).path.lstrip("/")

    async def create_if_missing() -> None:
        conn = await asyncpg.connect(_server_url_for_admin(TEST_DATABASE_URL))
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{name}"')
                print(f"\ncreated test database {name!r}")
        finally:
            await conn.close()

    asyncio.run(create_if_missing())


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the test database if absent and migrate it to head, once per session.

    Migrations rather than Base.metadata.create_all, because the two genuinely diverge here:
    this schema carries enum values added by ALTER TYPE in their own autocommit blocks
    (FixtureStatus.POSTPONED, InjurySource.API_FOOTBALL). create_all would build an enum from
    the current Python definition and quietly hide any migration that fails to reproduce it,
    which is exactly the class of bug a migration test should catch.

    Run as a subprocess so alembic's own event loop cannot collide with pytest-asyncio's.
    """
    _ensure_test_database_exists()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed against the test database:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    yield


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
