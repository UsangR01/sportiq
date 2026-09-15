"""A request budget shared by every process that talks to one BallDontLie subscription.

WHY THIS EXISTS. BallDontLie's limits are per sport and per tier (docs, confirmed 2026-09-15):

    Free 5/min    ALL-STAR 60/min    GOAT 600/min

The ATP subscription is dropping from GOAT to ALL-STAR to save $30/month -- a 10x cut. One
fixture's pre-match features were MEASURED at 17 ATP requests, so a busy week (two ATP 500s,
~96 matches) is on the order of 1,500 requests on the daily ingest: a few minutes at 600/min,
roughly 25 minutes of saturation at 60/min. And the adapter gives up after five 429 retries,
so saturation does not merely slow tennis down -- it makes predictions fail silently, inside a
Celery task nobody reads, on a worker running one task at a time that football, NBA and live
scores are all queued behind.

PACING, NOT RETRYING. Backing off after a 429 means the limit has already been hit. Spacing
requests so it never is costs nothing extra and removes the failure mode rather than
recovering from it.

SHARED ACROSS PROCESSES, WHICH IS WHY IT LIVES IN REDIS. The API process calls BallDontLie too
(the fixture screen's H2H and stats panels), and an in-memory limiter in each process would let
the two spend the same 60/min budget independently. The worker also recycles its child every
20 tasks, which would reset any in-memory state. Redis is already a hard dependency, as the
Celery broker.

    SET <key> 1 PX <interval_ms> NX

is the whole mechanism: whoever sets the key owns the next slot, and it expires on its own, so a
crashed holder can never wedge the budget.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _redis():
    """A client that lives and dies inside the CURRENT event loop.

    NOT app.core.redis.get_redis(), and the reason is a failure caught while testing this module.
    That helper shares one module-level ConnectionPool, and redis-py 6.4's pool creates an
    asyncio.Lock at construction -- state that binds to whichever loop first contends for it. The
    Celery worker runs every task in a FRESH loop (run_task -> asyncio.run), so the pool outlives
    the loop it was bound to. Measured: two concurrent-pacing test cases in one session let three
    requests through at the same instant, while the identical code inside a single loop spaced
    them 203ms apart every time.

    Production does not trip it today -- the tennis adapter makes no concurrent requests, and the
    API holds one long-lived loop -- but a single asyncio.gather added later would silently turn
    the budget off. A connection per call costs a sub-millisecond handshake on Render's private
    network against a 1.1-second spacing, and removes the shared state entirely.
    """
    from redis.asyncio import Redis

    from app.core.config import get_settings

    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# How long a denial is remembered. Long enough that a downgraded tier stops wasting budget on
# endpoints it can no longer reach, short enough that an upgrade -- or a transient gateway 401,
# which has happened on another provider here -- heals itself within the hour.
DENIAL_TTL_SECONDS = 3600

_warned_fallback = False


def _slot_key(namespace: str) -> str:
    return f"balldontlie:{namespace}:slot"


def _denied_key(namespace: str, endpoint: str) -> str:
    return f"balldontlie:{namespace}:denied:{endpoint}"


async def acquire_slot(namespace: str, requests_per_minute: int) -> None:
    """Block until this caller holds the next request slot for `namespace`.

    `requests_per_minute <= 0` disables pacing entirely -- the test suite runs this way, since
    it drives the adapter through httpx.MockTransport and pacing would only add wall-clock time.

    FAILS OPEN to local spacing if Redis is unreachable. A Redis blip must never stop tennis
    ingest outright; spacing locally is still correct for the common single-process case and only
    loses coordination with the other process for as long as Redis is down.
    """
    if requests_per_minute <= 0:
        return
    interval_ms = max(1, int(60_000 / requests_per_minute))

    try:
        async with _redis() as redis:
            key = _slot_key(namespace)
            while True:
                if await redis.set(key, "1", px=interval_ms, nx=True):
                    return
                remaining = await redis.pttl(key)
                # pttl is -2 for a key that vanished between the two calls and -1 for one with no
                # expiry (which this module never writes). Either way the slot is effectively free.
                await asyncio.sleep(max(remaining, 5) / 1000)
    except Exception as exc:  # pragma: no cover - exercised by the Redis-down test via patching
        global _warned_fallback
        if not _warned_fallback:
            logger.warning(
                "BallDontLie rate limiter could not reach Redis (%s) - pacing locally instead",
                exc,
            )
            _warned_fallback = True
        await asyncio.sleep(interval_ms / 1000)


async def is_denied(namespace: str, endpoint: str) -> bool:
    """Whether this endpoint recently returned 401/403 for this subscription.

    Checked BEFORE a slot is acquired, so a request that cannot succeed spends no budget at all.
    """
    try:
        async with _redis() as redis:
            return bool(await redis.exists(_denied_key(namespace, endpoint)))
    except Exception:
        # Unknown is treated as "not denied": the worst case is one wasted request, which the
        # caller already handles.
        return False


async def remember_denied(namespace: str, endpoint: str) -> None:
    """Record that this subscription cannot reach `endpoint`.

    SELF-CONFIGURING BY DESIGN. The alternative was an environment variable naming the tier,
    set by hand on the day of the downgrade -- a coordination step that is easy to forget, and
    forgetting it would leave every fixture screen spending the worker's budget on requests that
    can only 401. Learning it from the first 401 needs nobody to remember anything, and it also
    stops the WTA, which has never had these endpoints, from wasting calls on them.
    """
    try:
        async with _redis() as redis:
            await redis.set(_denied_key(namespace, endpoint), "1", ex=DENIAL_TTL_SECONDS)
        logger.warning(
            "BallDontLie %s /%s returned not-entitled; skipping it for %ds",
            namespace,
            endpoint,
            DENIAL_TTL_SECONDS,
        )
    except Exception:
        pass
