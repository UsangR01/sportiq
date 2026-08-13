"""A managed Postgres URL must resolve onto the async driver this project installs.

THE FAILURE THIS PINS, from the first real Render deploy. Render's `fromDatabase:
connectionString` yields "postgresql://user:pass@host/db" -- correct, and naming no driver.
requirements.txt carries asyncpg and no sync driver, so SQLAlchemy resolved that to its default
psycopg2 dialect and raised "The asyncio extension requires an async driver to be used". That
happened inside `alembic upgrade head` in preDeployCommand, so the web service never started at
all, while the worker and beat reported success -- container start never touches Postgres, so
they would only have failed on their first real task.

Rewriting in Settings rather than asking the operator to hand-set DATABASE_URL is deliberate:
the value is auto-wired from the database resource in the blueprint, so overriding it by hand
means giving up that wiring and re-pasting a password on every rotation.
"""

import pytest

from app.core.config import Settings


@pytest.mark.parametrize("scheme", ["postgresql://", "postgres://"])
def test_a_driverless_url_is_normalised_onto_asyncpg(scheme, monkeypatch):
    """Both spellings a managed provider might hand us. 'postgres://' is the legacy Heroku-style
    form; Render emits 'postgresql://'. Neither names a driver."""
    monkeypatch.setenv("DATABASE_URL", f"{scheme}u:p@host:5432/db")
    assert Settings().database_url == "postgresql+asyncpg://u:p@host:5432/db"


def test_credentials_and_query_string_survive_the_rewrite(monkeypatch):
    """Only the scheme is replaced. A password containing '://' or a sslmode parameter must come
    through untouched -- mangling either would fail at connect time, far from here."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p%40ss@host:5432/db?sslmode=require")
    assert Settings().database_url == ("postgresql+asyncpg://u:p%40ss@host:5432/db?sslmode=require")


def test_an_explicit_driver_is_left_alone(monkeypatch):
    """Already-async URLs pass through, and so does a deliberate SYNC one -- a future script may
    legitimately want psycopg. Only the ambiguous, driverless case is rewritten."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    assert Settings().database_url == "postgresql+asyncpg://u:p@host/db"
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@host/db")
    assert Settings().database_url == "postgresql+psycopg://u:p@host/db"


def test_the_local_default_is_already_async():
    """Nothing about the local dev default changes."""
    assert Settings().database_url.startswith("postgresql+asyncpg://")
