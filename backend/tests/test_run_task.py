"""Regression test for a real Celery-worker crash found while running this project's own
scheduled ingestion tasks: on Windows, Celery has no `prefork` pool (one fresh OS process per
task), so `--pool=solo`/`--pool=threads` run every task in ONE long-lived worker process. Every
task previously wrapped its async body in a bare `asyncio.run(...)` — fine for a single task,
but the SECOND task run in that same process reused `app.core.database.engine`'s connections
(bound to the first task's now-closed event loop) and crashed with
`AttributeError: 'NoneType' object has no attribute 'send'` (confirmed live against a real
worker). `app.workers.celery.run_task` is the fix — every task now goes through it instead of a
bare `asyncio.run`, disposing the shared engine/Redis pool on the same loop before it closes,
mirroring conftest.py's own `_dispose_engine_after_test` fixture.

This test is deliberately NOT an async test (no pytest-asyncio event loop) — it calls the real,
synchronous `run_task` twice in a row, exactly like Celery's `--pool=solo` executor would,
which is the only way to actually reproduce the cross-loop reuse this bug depended on.
"""

from sqlalchemy import select

from app.core.database import async_session_factory
from app.sports.models import Sport
from app.workers.celery import run_task


async def _touch_db() -> None:
    async with async_session_factory() as db:
        await db.execute(select(Sport).limit(1))


def test_run_task_survives_two_sequential_invocations_in_one_process():
    """Would raise AttributeError/RuntimeError on the second call before the fix."""
    run_task(_touch_db())
    run_task(_touch_db())
