import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_ready

from app.core.config import get_settings

logger = logging.getLogger(__name__)

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
        "app.workers.snapshot_picks",
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
        # Every 3 hours, NOT every 5 minutes. The 5-minute cadence was inherited from
        # ingest_live_scores, but odds are quota-metered per request in a way live scores are
        # not: 7 leagues x 8 dates x 288 runs/day is ~16,000 requests/day, which against the
        # old 1,000-request MONTHLY allowance was exhausted in ~90 minutes and then 429'd for
        # weeks. Combined with _dates_with_fixtures (only real fixture dates) and a 3-day
        # lookahead, a run costs roughly the number of match days actually scheduled.
        #
        # STAYS AT 6h even though TheRundown Pro raised the allowance 1,000 -> 5,000/month.
        # Tightening to 3h was tried and reverted: measured cost today is only 7 requests/run
        # (34% of quota at 3h), but that reflects an off-season day where most league-days have
        # no fixtures. The worst case this schedule must survive is every league playing on
        # every lookahead day:
        #     every 6h = 112 req/day =  3,360/month ( 67%)   <- fits
        #     every 4h = 168 req/day =  5,040/month (101%)   <- over
        #     every 3h = 224 req/day =  6,720/month (134%)   <- over
        # Sizing a quota-metered job off an off-season measurement is what caused the original
        # outage, so the worst case governs. test_ingest_odds_quota.py enforces this.
        #
        # Tennis freshness is handled separately and does NOT consume this quota at all -- see
        # ingest-tennis-odds-hourly below.
        "schedule": 21600.0,
    },
    "capture-closing-odds-every-15-minutes": {
        "task": "app.workers.ingest_odds.capture_closing_odds",
        # Frequent but nearly free: the task returns immediately without an API call unless a
        # fixture kicks off in the next 10-45 minutes, and then asks only for that fixture's
        # own date. Cost tracks the match calendar, not the clock.
        #
        # It exists because CLV needs the market's FINAL pre-kickoff price and the 6-hourly job
        # cannot reliably supply one - only 72 of 2,369 settled fixtures had any pre-kickoff
        # price at all. Without it there is no way to tell a model with an edge from one that
        # just backs short favourites.
        "schedule": 900.0,
    },
    "ingest-tennis-odds-hourly": {
        "task": "app.workers.ingest_odds.ingest_tennis_odds",
        # Tennis only, BallDontLie only. Exempt from the 6-hourly cadence above because it
        # spends none of TheRundown's monthly allowance — GOAT is 600 req/MINUTE and a refresh
        # costs a couple of calls. Books price a tennis match close to its start, so the
        # 6-hour job left freshly-priced matches showing no odds on the card.
        "schedule": 3600.0,
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
    "snapshot-shown-picks-hourly": {
        "task": "app.workers.snapshot_picks.snapshot_shown_picks",
        # Hourly against a 4-hour window, so a fixture cannot slip through between runs.
        # Makes no external API calls — it reads predictions and odds already stored — and
        # returns immediately when nothing sits in the window.
        "schedule": 3600.0,
    },
    "check-push-receipts-every-30-minutes": {
        "task": "app.workers.notify_users.check_push_receipts",
        # An Expo push ticket only means ACCEPTED. The delivery outcome arrives later in a
        # receipt, and that is the only place a wrong FCM credential shows up — it fails every
        # send while every ticket still reports success. Cheap: one Expo call per batch, and it
        # returns immediately when no ticket is old enough to have a receipt yet.
        "schedule": 1800.0,
    },
}


# --- stale-worker detection -------------------------------------------------------------
# A Celery worker serves whatever it imported at launch and has no --reload of its own, so it
# can silently fall behind the files on disk. scripts/dev_worker.py prevents that in
# development by restarting on change, but nothing protects a worker started by hand, and in
# production the risk is a deploy that restarts the web service without restarting the worker.
#
# The worker cannot answer HTTP, so it publishes what it loaded to Redis at startup instead —
# giving scripts/check_stale.py a single place to compare every process against disk.
WORKER_VERSION_KEY = "sportiq:worker:code_version"

# Beat is a SEPARATE long-lived process with its own import of this module, and it was the
# blind spot: the worker published its version while beat published nothing, so check_stale.py
# reported "ok" for a scheduler running a schedule that predated the code.
#
# That is not hypothetical. snapshot_picks.py and its beat_schedule entry were written three
# hours AFTER beat was launched, so the running scheduler held a schedule with no snapshot
# entry at all. Nothing errored; the task simply never ran, and a measurement meant to
# accumulate for five weeks collected one row in twenty-four hours. A stale WORKER applies old
# logic to work it receives; a stale BEAT never dispatches the work at all, which is quieter
# and worse.
BEAT_VERSION_KEY = "sportiq:beat:code_version"


def _publish_code_version_to(key: str, role: str, sender=None) -> None:
    """Record a process's loaded code version, best-effort.

    Never allowed to stop the process starting: a diagnostic that can take the queue or the
    schedule down is worse than the staleness it reports."""
    import json

    from redis import Redis

    from app.core.code_version import loaded_code_version

    version = loaded_code_version()
    payload = {
        **version.as_dict(),
        "hostname": getattr(getattr(sender, "hostname", None), "__str__", lambda: "?")(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        client = Redis.from_url(get_settings().redis_url)
        client.set(key, json.dumps(payload))
        client.close()
    except Exception:  # noqa: BLE001 - diagnostics must never break startup
        logger.warning("could not publish %s code version to Redis", role, exc_info=True)
    logger.info(
        "celery " + role + " code version: fingerprint=%s git=%s dirty=%s",
        version.fingerprint,
        version.git_sha,
        version.git_dirty,
    )


@worker_ready.connect
def _publish_worker_code_version(sender=None, **_kwargs) -> None:
    _publish_code_version_to(WORKER_VERSION_KEY, "worker", sender)


@beat_init.connect
def _publish_beat_code_version(sender=None, **_kwargs) -> None:
    _publish_code_version_to(BEAT_VERSION_KEY, "beat", sender)
