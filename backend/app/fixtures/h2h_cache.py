"""Cache for the fixture-detail Head-to-Head panel.

Every open of GET /fixtures/{id} was making up to SIX live API-Football calls -- one
/fixtures/headtohead plus one /fixtures/statistics per counted meeting -- with nothing cached.
Measured on a healthy quota that is 2.0-3.1 seconds per view, and it recurs on every single
open of the same fixture, refetching identical historical matches indefinitely.

Two consequences, both real:

  * A PAGE RENDER WAS COUPLED TO A THIRD PARTY'S QUOTA. When the daily allowance ran out the
    detail screen stopped loading entirely, and the workaround was upgrading the plan -- which
    widens the ceiling without removing the coupling.
  * THE API BILL SCALED WITH USER TRAFFIC. 100 users opening 10 fixtures a day is ~6,000
    requests/day for browsing alone, before any ingestion. That grows with exactly the thing
    the product wants to grow.

The data barely changes: a pair's head-to-head record is fixed until those two teams next
play each other, which is typically months apart.

KEYED ON THE ORDERED PAIR, NOT AN UNORDERED ONE. Every avg_*_home/away field is oriented to
the CURRENT fixture's home/away assignment, so (A home, B away) and (B home, A away) are
genuinely different payloads. Sorting the ids into a canonical key would serve one fixture's
panel to the reverse fixture with the sides silently transposed -- the same defect class as the
inverted tennis odds, where a favourite's price was shown against the underdog.

FAILURES ARE NON-FATAL IN BOTH DIRECTIONS. A Redis outage must not take down a screen that
worked before this module existed, so a read error falls through to the live call and a write
error is swallowed. The cache is an optimisation, never a dependency.
"""

from __future__ import annotations

import dataclasses
import json
import logging

logger = logging.getLogger(__name__)

# Long, because the underlying facts are static between meetings. The bound this trades away
# is narrow: if the two sides meet again within the window (a league fixture plus a cup tie),
# the panel can omit that newest meeting from its 5-match averages until the key expires. That
# is a slightly stale average, not a wrong one -- and far cheaper than six calls per view.
H2H_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def h2h_cache_key(home_external_id: str, away_external_id: str) -> str:
    return f"h2h:detail:{home_external_id}:{away_external_id}"


async def get_cached_h2h(redis, home_external_id: str, away_external_id: str, cls):
    """Return a cached H2HDetail, or None to mean "go and fetch it".

    A cached NULL result is stored as the literal string "null" so that two teams who have
    genuinely never met do not re-trigger six calls on every view -- the absence is itself a
    fact worth remembering. Distinguishing that from a cache MISS is why this returns a
    (hit, value) pair rather than just the value.
    """
    try:
        raw = await redis.get(h2h_cache_key(home_external_id, away_external_id))
    except Exception:
        # A Redis problem must never be visible to someone opening a fixture.
        logger.warning("H2H cache read failed; falling through to the live call", exc_info=True)
        return False, None
    if raw is None:
        return False, None
    if raw == "null":
        return True, None
    try:
        return True, cls(**json.loads(raw))
    except (ValueError, TypeError):
        # A payload written by an older shape of H2HDetail: treat as a miss and let the live
        # call overwrite it, rather than 500ing on someone's screen.
        logger.warning("Discarding unreadable H2H cache entry")
        return False, None


async def set_cached_h2h(redis, home_external_id: str, away_external_id: str, detail) -> None:
    """Store a fetched panel, including a genuine 'these teams have never met' result."""
    payload = "null" if detail is None else json.dumps(dataclasses.asdict(detail))
    try:
        await redis.set(
            h2h_cache_key(home_external_id, away_external_id),
            payload,
            ex=H2H_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.warning("H2H cache write failed; the panel still rendered", exc_info=True)
