import asyncio
from collections.abc import Coroutine
from typing import Any

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "sportiq",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.ingest_odds",
        "app.workers.ingest_fixtures",
        "app.workers.ingest_live_scores",
        "app.workers.ingest_injuries",
        "app.workers.run_predictions",
        "app.workers.notify_users",
        # Both were previously omitted, relying on ingest_fixtures.py's own lazy
        # in-function import (for the same module) to have already registered them as a
        # side effect by the time anyone invokes the standalone task directly — real,
        # live-confirmed latent bug: a freshly-started worker that hasn't yet processed a
        # football/tennis ingest_fixtures run raises `KeyError` ("Received unregistered
        # task") the first time backfill_predictions/backfill_tennis_predictions.delay()
        # is called on it, defeating their own docstrings' "standalone entry point" promise.
        "app.workers.backfill_predictions",
        "app.workers.backfill_tennis_predictions",
    ],
)

celery_app.conf.timezone = "UTC"


def run_task(coro: Coroutine[Any, Any, None]) -> None:
    """Every Celery task entrypoint must call this instead of a bare `asyncio.run(coro)`.

    `app.core.database.engine` (and `app.core.redis._pool`) are module-level singletons whose
    underlying connections are bound to whichever event loop first used them. `asyncio.run()`
    creates a brand-new loop per call and closes it on return — harmless on Linux's default
    `prefork` pool (a fresh OS process per task, so the singleton is never reused across
    loops), but Celery has no prefork on Windows: `--pool=solo`/`--pool=threads` run every
    task in ONE long-lived worker process. Confirmed live: a real worker's second task
    (`run_predictions` after an earlier `ingest_odds`) crashed with `AttributeError: 'NoneType'
    object has no attribute 'send'` — the exact Windows ProactorEventLoop symptom
    `tests/conftest.py` already works around for pytest-asyncio, for the identical reason.
    Disposing both singletons here, on the SAME loop right before it closes, is that same
    fix applied at the Celery task boundary instead of the pytest boundary.
    """
    from app.core.database import engine
    from app.core.redis import _pool as redis_pool

    async def _run() -> None:
        try:
            await coro
        finally:
            await engine.dispose()
            await redis_pool.disconnect()

    asyncio.run(_run())


# run_predictions and notify_users are triggered by other tasks (new odds/injury data, late
# injury re-inference) rather than run on a fixed cadence — not scheduled here (TDD §2.3/§5.4).
celery_app.conf.beat_schedule = {
    "ingest-odds-every-6-hours": {
        "task": "app.workers.ingest_odds.ingest_odds",
        # Every 6 hours, NOT every 5 minutes. The 5-minute cadence was inherited from
        # ingest_live_scores, but odds are quota-metered per request in a way live scores are
        # not: 7 leagues x 8 dates x 288 runs/day is ~16,000 requests/day against TheRundown's
        # 1,000-request MONTHLY allowance, which it duly exhausted in ~90 minutes and then 429'd
        # for weeks. Combined with _dates_with_fixtures (only real fixture dates) and a
        # 3-day lookahead, this brings a run down to roughly the number of match days actually
        # scheduled.
        #
        # Honest caveat: at PEAK season (most leagues playing, several match days each) even
        # this may exceed a 1,000/month free allowance. If odds coverage matters more than the
        # subscription cost, the fix is a plan upgrade, not a further cadence cut - dropping
        # below daily would make prices too stale to rank on.
        "schedule": 21600.0,
    },
    "ingest-live-scores-every-5-minutes": {
        "task": "app.workers.ingest_live_scores.ingest_live_scores",
        "schedule": 300.0,
    },
    "ingest-fixtures-daily": {
        "task": "app.workers.ingest_fixtures.ingest_fixtures",
        "schedule": crontab(hour=2, minute=0),
    },
    "ingest-injuries-every-30-minutes": {
        "task": "app.workers.ingest_injuries.ingest_injuries",
        "schedule": 1800.0,
    },
}
