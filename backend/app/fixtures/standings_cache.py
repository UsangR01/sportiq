"""Cache for the fixture-detail Standings tab.

SAME ARGUMENT AS h2h_cache.py, and the same failure contract. Without it, every open of a
fixture screen would spend one live /standings call, so the API bill would scale with browsing
traffic rather than with the number of leagues — which is the coupling that took the detail
screen down once already when the daily allowance ran out.

KEYED ON THE LEAGUE, NOT THE FIXTURE. A table is a property of the competition, so every
fixture in a league shares one entry. That is the whole saving: one call per league per day
instead of one per view.

A DAY, NOT A WEEK. Unlike a head-to-head record — fixed until two clubs next meet — a table
moves every time any match in the league finishes. A stale table is a wrong table in a way a
slightly stale average is not, and a day bounds it to "correct as of this morning" while still
collapsing thousands of views into one call.

FAILURES ARE NON-FATAL IN BOTH DIRECTIONS, exactly as in h2h_cache: a read error falls through
to the live call, a write error is swallowed. The cache is an optimisation, never a dependency.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

STANDINGS_CACHE_TTL_SECONDS = 24 * 60 * 60


def standings_cache_key(league_slug: str) -> str:
    return f"standings:{league_slug}"


async def get_cached_standings(redis, league_slug: str) -> tuple[bool, list[dict]]:
    """Return (hit, rows). Rows are plain dicts, ready for StandingRowResponse(**row).

    An EMPTY table is cached as a real result rather than a miss — a pre-season league genuinely
    has no standings, and re-asking on every view is precisely the cost this exists to avoid.
    Returning (hit, value) rather than just the value is what makes that distinguishable.
    """
    try:
        raw = await redis.get(standings_cache_key(league_slug))
    except Exception:
        logger.warning("Standings cache read failed; falling through to the live call")
        return False, []
    if raw is None:
        return False, []
    try:
        rows = json.loads(raw)
    except ValueError:
        logger.warning("Discarding unreadable standings cache entry for %s", league_slug)
        return False, []
    if not isinstance(rows, list):
        return False, []
    return True, rows


async def set_cached_standings(redis, league_slug: str, rows: list[dict]) -> None:
    try:
        await redis.set(
            standings_cache_key(league_slug),
            json.dumps(rows),
            ex=STANDINGS_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Standings cache write failed; the table still rendered")
