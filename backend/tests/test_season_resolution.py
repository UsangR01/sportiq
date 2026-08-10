"""resolve_current_season: prefer the provider's own `current: true` over our guesses.

Three season-convention bugs have shipped, each silently. Brasileirão runs Jan-Dec rather than
Aug-May; four Tier-1 leagues likewise; the J1 League labels its season by the year it ENDS
(2026-08-07 -> 2027-06-06 is "2027", while the EPL's near-identical window is "2026"). None
raised: the wrong season returns HTTP 200 with an empty `errors` object and results=0, which
reads exactly like "no matches scheduled this week". The J1 League ingested nothing at all
until its computed season was diffed against this field.

The hardcoded conventions are kept as the fallback, so this can only improve on the previous
behaviour and never take ingestion down.
"""

import httpx
import pytest

from app.adapters import api_football
from app.adapters.api_football import (
    _current_football_season,
    resolve_current_season,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    api_football._SEASON_CACHE.clear()
    yield
    api_football._SEASON_CACHE.clear()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


def _leagues_payload(seasons):
    return {"response": [{"seasons": seasons}], "errors": []}


@pytest.mark.asyncio
async def test_the_providers_current_season_wins_over_the_hardcoded_convention():
    """The J1 case, which no convention produced: both rules say 2026, the provider says 2027."""

    async def handler(request):
        return httpx.Response(
            200,
            json=_leagues_payload(
                [
                    {"year": 2026, "current": False},
                    {"year": 2027, "current": True},
                ]
            ),
        )

    async with _client(handler) as client:
        assert await resolve_current_season(client, "j1_league") == 2027


@pytest.mark.asyncio
async def test_a_provider_failure_falls_back_instead_of_breaking_ingestion():
    """THE safety property. A season lookup is an optimisation over a guess, never a new
    dependency -- if it could fail hard it would be strictly worse than the guess it replaced."""

    async def handler(request):
        return httpx.Response(500)

    async with _client(handler) as client:
        assert await resolve_current_season(client, "epl") == _current_football_season("epl")


@pytest.mark.asyncio
async def test_a_response_with_no_current_flag_falls_back():
    """Real data will not always mark one season current -- an off-season league may mark none."""

    async def handler(request):
        return httpx.Response(200, json=_leagues_payload([{"year": 2025, "current": False}]))

    async with _client(handler) as client:
        assert await resolve_current_season(client, "epl") == _current_football_season("epl")


@pytest.mark.asyncio
async def test_an_api_football_error_object_falls_back_rather_than_being_read_as_data():
    """This provider signals failure with HTTP 200 plus a populated `errors` object -- the
    silent-failure shape _api_response exists to catch. A rate-limited lookup must not be
    mistaken for 'no current season'."""

    async def handler(request):
        return httpx.Response(
            200, json={"response": [], "errors": {"rateLimit": "Too many requests."}}
        )

    async with _client(handler) as client:
        assert await resolve_current_season(client, "epl") == _current_football_season("epl")


@pytest.mark.asyncio
async def test_an_unmapped_league_never_calls_the_provider():
    calls = []

    async def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=_leagues_payload([{"year": 2027, "current": True}]))

    async with _client(handler) as client:
        assert await resolve_current_season(client, "not_a_league") == _current_football_season(
            "not_a_league"
        )
    assert calls == []


@pytest.mark.asyncio
async def test_the_result_is_cached_so_every_fixture_pull_does_not_re_ask():
    """Called from fetch_fixtures/fetch_odds/fetch_team_stats/fetch_injuries, the last of which
    loops every league every 30 minutes. Uncached, that is a real per-run cost for a value that
    changes a handful of times a year."""
    calls = []

    async def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=_leagues_payload([{"year": 2027, "current": True}]))

    async with _client(handler) as client:
        assert await resolve_current_season(client, "j1_league") == 2027
        assert await resolve_current_season(client, "j1_league") == 2027
        assert await resolve_current_season(client, "j1_league") == 2027

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_cached_so_it_can_recover():
    """Caching a fallback would freeze a transient outage in for the whole TTL."""
    responses = [
        httpx.Response(500),
        httpx.Response(200, json=_leagues_payload([{"year": 2027, "current": True}])),
    ]

    async def handler(request):
        return responses.pop(0)

    async with _client(handler) as client:
        assert await resolve_current_season(client, "j1_league") == _current_football_season(
            "j1_league"
        )
        assert await resolve_current_season(client, "j1_league") == 2027
