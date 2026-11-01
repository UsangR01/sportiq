"""The filter chips must never promise a pick the filter does not return.

THE COUNT IS THE WHOLE FEATURE. Market chips existed in this app once and were removed as
clutter -- a row of filters that mostly led to empty feeds. They come back only because each
one carries a count, so "Corners (0)" tells a user not to tap it. A count that disagreed with
what tapping returns would be worse than no chip at all.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.fixtures.router import MARKET_FAMILIES
from app.main import app


@pytest.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def test_the_families_are_exactly_the_markets_the_feed_can_filter_to():
    """Exhaustive on purpose: a market the feed can serve but no chip offers is invisible, and
    a chip for a market the backend rejects is a dead button."""
    from app.fixtures.router import _VALID_LINES_BY_MARKET

    assert MARKET_FAMILIES == ("h2h", "double_chance", "goals_total", "corners_total")
    # Every totals market needs a line, and the client supplies a default for exactly those.
    assert set(_VALID_LINES_BY_MARKET) <= set(MARKET_FAMILIES)


def test_draw_is_not_offered_as_its_own_chip():
    """A "Draw" chip could never return anything, which is why the families are grouped rather
    than split per selection.

    MIN_EDGE_OVER_BASE_RATE requires a draw to clear its base rate by 5pp -- 0.3038 for
    football -- while the model gives 92% of fixtures a draw probability between 0.2 and 0.3.
    Measured over the whole settled card history: ZERO draw picks have ever reached a card.
    """
    from app.fixtures.router import MARKET_BASE_RATES, MIN_EDGE_OVER_BASE_RATE

    assert "draw" not in MARKET_FAMILIES
    draw_bar = MARKET_BASE_RATES[("h2h", "draw", None)] + MIN_EDGE_OVER_BASE_RATE
    assert draw_bar > 0.30, "if this ever drops, a Draw chip becomes worth reconsidering"


@pytest.mark.asyncio
async def test_the_count_equals_what_filtering_actually_returns(api_client):
    """THE CONTRACT, checked end to end rather than by reading the code.

    For every market the chips report, filtering the feed to it must return exactly that many
    fixtures carrying a pick. They are computed by different code paths -- the count re-ranks
    per family inside the bulk loop, the filter restricts candidates up front -- so this is a
    real cross-check, not a tautology.
    """
    counts = (await api_client.get("/fixtures/market-counts?limit=200")).json()
    assert "total" in counts and "counts" in counts

    default_line = {"goals_total": 2.5, "corners_total": 9.5}
    for family, expected in counts["counts"].items():
        query = f"/fixtures?limit=200&market={family}"
        if family in default_line:
            query += f"&line={default_line[family]}"
        feed = (await api_client.get(query)).json()
        actual = sum(1 for fixture in feed if fixture.get("best_pick"))
        assert actual == expected, (
            f"chip says {family}={expected} but filtering returns {actual} -- a chip that "
            "overstates is worse than no chip"
        )


@pytest.mark.asyncio
async def test_the_counts_ignore_an_active_market_filter(api_client):
    """The chips exist to say what the markets you are NOT looking at contain, so the endpoint
    must refuse a `market` param rather than silently honour it and zero every other chip."""
    with_market = await api_client.get("/fixtures/market-counts?limit=200&market=h2h")
    plain = await api_client.get("/fixtures/market-counts?limit=200")
    # An unknown query param is ignored by FastAPI, which is the behaviour we want here:
    # the counts are identical either way.
    assert with_market.json() == plain.json()
