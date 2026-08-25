"""Block until the database schema matches the migrations in THIS image, then exit.

WHY THIS EXISTS. `preDeployCommand: alembic upgrade head` runs on the WEB service only, and a
Render blueprint brings services up in parallel — so the worker and beat can start against a
database that has not been migrated yet. Their ORM then selects a column the database does not
have, every query touching that table raises, and the per-league try/except in ingest swallows
it: the run completes, changes nothing, and logs no error. That failure shape has already cost
this project a day of diagnosis once.

DELIBERATELY A WAIT AND NOT A MIGRATION. Adding preDeployCommand to all three services would
race three alembic processes against one database; alembic takes no lock by default, and two
concurrent upgrades on the same revision is a worse failure than starting late. Exactly one
service migrates; the others wait for it.

FAILS RATHER THAN PROCEEDING. If the schema has not caught up within the timeout, this exits
non-zero so the container is restarted rather than left running against a schema it does not
match — a loud restart loop is far easier to diagnose than a worker that silently changes
nothing.

    python scripts/wait_for_schema.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from sqlalchemy import text

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Long enough for a real migration on a cold start, short enough that a genuinely stuck deploy
#: surfaces as a restart loop rather than a service that looks healthy and does nothing.
TIMEOUT_SECONDS = int(os.environ.get("SCHEMA_WAIT_TIMEOUT", "180"))
POLL_SECONDS = 3


def _expected_head() -> str:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        # Two heads means an un-merged migration branch. Refuse rather than pick one.
        raise SystemExit(f"expected exactly one alembic head, found {heads}")
    return heads[0]


async def _current_head() -> str | None:
    from app.core.database import async_session_factory

    try:
        async with async_session_factory() as db:
            return (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception:
        # An unmigrated database may not even have alembic_version yet, and the database itself
        # may still be accepting connections. Both are "not ready", not "broken".
        return None


async def main() -> None:
    expected = _expected_head()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    seen = None
    while time.monotonic() < deadline:
        seen = await _current_head()
        if seen == expected:
            print(f"schema is at {expected}; starting")
            return
        print(f"waiting for schema: database at {seen}, image expects {expected}", flush=True)
        await asyncio.sleep(POLL_SECONDS)
    print(
        f"TIMED OUT after {TIMEOUT_SECONDS}s: database at {seen}, image expects {expected}. "
        "Refusing to start against a mismatched schema.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
