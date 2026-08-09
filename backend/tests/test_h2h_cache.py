"""The head-to-head panel cache (app/fixtures/h2h_cache.py).

Exists because GET /fixtures/{id} was making up to SIX live API-Football calls on EVERY view
(2.0-3.1s measured) for facts that do not change until the two teams next meet. That coupled a
page render to a third party's quota -- when the daily allowance ran out the detail screen
stopped loading -- and made the API bill scale with user traffic.

Three properties are pinned here because breaking any of them is silent:
  * the key is the ORDERED pair, since the payload is oriented to the current home/away
  * "these teams never met" is cached too, or a null result re-triggers six calls per view
  * Redis failures fall through to the live call instead of taking the screen down
"""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from app.fixtures.h2h_cache import (
    H2H_CACHE_TTL_SECONDS,
    get_cached_h2h,
    h2h_cache_key,
    set_cached_h2h,
)


@dataclass
class _Detail:
    meetings_count: int
    home_wins: int


def test_key_is_the_ordered_pair_not_a_canonical_one():
    """Sorting the ids into one key would serve a fixture's panel to the REVERSE fixture with
    the sides transposed.

    Every avg_*_home/away field is relative to the CURRENT fixture's home/away assignment, so
    (A home, B away) and (B home, A away) are genuinely different payloads. Collapsing them is
    the same defect class as the inverted tennis odds, where a heavy favourite's price was
    displayed against the underdog."""
    assert h2h_cache_key("100", "200") != h2h_cache_key("200", "100")


@pytest.mark.asyncio
async def test_a_hit_returns_the_stored_panel_and_makes_no_call():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps({"meetings_count": 5, "home_wins": 3}))

    hit, detail = await get_cached_h2h(redis, "100", "200", _Detail)

    assert hit is True
    assert detail == _Detail(meetings_count=5, home_wins=3)


@pytest.mark.asyncio
async def test_never_met_is_cached_as_a_real_answer():
    """A null result is a FACT about these two teams, not a missing entry. Without storing it,
    every view of a first-ever meeting would re-trigger the full six calls."""
    redis = AsyncMock()
    redis.set = AsyncMock()
    await set_cached_h2h(redis, "100", "200", None)
    assert redis.set.await_args.args[1] == "null"

    redis.get = AsyncMock(return_value="null")
    hit, detail = await get_cached_h2h(redis, "100", "200", _Detail)
    assert hit is True and detail is None  # a HIT whose value is None, not a miss


@pytest.mark.asyncio
async def test_a_miss_is_distinguishable_from_a_cached_null():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    hit, detail = await get_cached_h2h(redis, "100", "200", _Detail)
    assert hit is False and detail is None


@pytest.mark.asyncio
async def test_redis_failure_falls_through_instead_of_breaking_the_screen():
    """The cache is an optimisation, never a dependency. A Redis outage must not take down a
    screen that worked before this module existed."""
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    hit, detail = await get_cached_h2h(redis, "100", "200", _Detail)
    assert hit is False and detail is None  # -> caller performs the live fetch

    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    await set_cached_h2h(redis, "100", "200", _Detail(1, 1))  # must not raise


@pytest.mark.asyncio
async def test_a_payload_from_an_older_shape_is_treated_as_a_miss():
    """H2HDetail has gained fields before (corners, shots, possession replaced a match list).
    A cached entry written by an older shape must not 500 someone's screen -- it is discarded
    and refetched."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps({"unexpected_field": 1}))
    hit, detail = await get_cached_h2h(redis, "100", "200", _Detail)
    assert hit is False and detail is None


@pytest.mark.asyncio
async def test_ttl_is_long_because_the_data_is_static_between_meetings():
    """Days, not minutes: a pair's record is fixed until they next play, typically months. A
    short TTL would leave most of the six-calls-per-view cost in place."""
    redis = AsyncMock()
    redis.set = AsyncMock()
    await set_cached_h2h(redis, "100", "200", _Detail(2, 1))
    assert redis.set.await_args.kwargs["ex"] == H2H_CACHE_TTL_SECONDS
    assert H2H_CACHE_TTL_SECONDS >= 24 * 60 * 60
