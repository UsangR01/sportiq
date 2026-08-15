"""Cache for the fixture-detail Match Stats panel.

SEPARATE FROM h2h_cache BECAUSE THE INVALIDATION ARGUMENT IS DIFFERENT, not because the code
is. H2H is cached for a week and accepts a known staleness cost: if the two sides meet again
inside the window, the panel's averages omit that newest meeting until the key expires.

A COMPLETED MATCH'S STATISTICS ARE IMMUTABLE. The match has been played; its corner count will
never change. So there is no staleness trade-off here at all and the TTL exists purely to stop
the keyspace growing without bound.

The panel is only ever built for a COMPLETED fixture, which is what makes that true -- caching
this for a live fixture would freeze a score mid-match, so the caller gates on status before it
gets here.

WHAT IS CACHED IS THE PROVIDER'S RESPONSE, NOT THE RENDERED PANEL, and that distinction is
load-bearing. Goals and corners are read from our OWN fixture_live_state, which keeps changing
after the match: `_backfill_corners_from_thestatsapi` fills corner counts for up to SEVEN DAYS
afterwards, and for whole leagues (Veikkausliiga has 0% API-Football corner coverage) that is
the only source there will ever be. Caching the finished rows for 30 days would freeze the
absence in place and permanently hide a count that arrived on day three.

KEYED ON SPORT AS WELL AS EXTERNAL ID. Providers number fixtures independently -- an
API-Football id and a BallDontLie id are both bare integers -- so a football fixture and a
basketball one can genuinely collide on the id alone, and the collision would serve one sport's
payload to the other. Same hazard the WNBA prefixing exists to prevent.

FAILURES ARE NON-FATAL IN BOTH DIRECTIONS, matching h2h_cache: a read error falls through to
the live call and a write error is swallowed. The cache is an optimisation, never a dependency.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 30 days. The facts never change, so this is a keyspace bound rather than a freshness one --
# and it comfortably covers the window in which anyone reviews a past result.
MATCH_STATS_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


def match_stats_cache_key(sport_slug: str, fixture_external_id: str) -> str:
    return f"fixture:matchstats:{sport_slug}:{fixture_external_id}"


async def get_cached_match_stats(redis, sport_slug: str, fixture_external_id: str):
    """Return (hit, payload) — whatever JSON the caller stored for this fixture.

    An EMPTY payload is a real, cacheable answer: a walkover, or a competition the provider
    publishes no statistics for. Treating it as a miss would re-request on every single view
    exactly the fixtures the provider has already declined to answer for, which is the
    expensive case rather than the cheap one. Distinguishing that from a genuine MISS is why
    this returns a (hit, value) pair rather than just the value.
    """
    try:
        raw = await redis.get(match_stats_cache_key(sport_slug, fixture_external_id))
    except Exception:
        logger.warning("Match-stats cache read failed; falling through", exc_info=True)
        return False, None
    if raw is None:
        return False, None
    try:
        return True, json.loads(raw)
    except ValueError:
        logger.warning("Discarding unreadable match-stats cache entry")
        return False, None


async def set_cached_match_stats(redis, sport_slug: str, fixture_external_id: str, payload) -> None:
    try:
        await redis.set(
            match_stats_cache_key(sport_slug, fixture_external_id),
            json.dumps(payload),
            ex=MATCH_STATS_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Match-stats cache write failed; the panel still rendered", exc_info=True)
