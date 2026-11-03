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

    # THE SERVER'S OWN LINES, not a copy. Using a local dict here is what let the first
    # version pass while production disagreed: the count tallied every goals line (49) and
    # tapping the chip asked for 2.5 (43).
    lines = counts["lines"]
    for family, expected in counts["counts"].items():
        query = f"/fixtures?limit=200&market={family}"
        if family in lines:
            query += f"&line={lines[family]}"
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


def test_a_frozen_card_honours_the_market_filter():
    """THE BUG THE END-TO-END CHECK MISSED, and why this one is a unit test.

    The feed applied `market` to LIVE candidates only. A past-kickoff card is served from its
    frozen list instead, which was never filtered -- so it appeared under every market. The
    cross-check above passed locally because the dev database had no frozen multi-market
    fixtures; production disagreed by 5 cards (chips said 50 h2h, filtering returned 55).

    The filter is the user's own control, like min_probability, so it CHOOSES AMONG the
    frozen candidates rather than vetoing the winner -- it can narrow a list fixed at
    kickoff, never invent a pick that was not in it.
    """
    from datetime import UTC, datetime

    from app.fixtures.schemas import BestPick
    from app.predictions.pick_freeze import apply_frozen

    class _Frozen:
        prediction_created_at = datetime(2026, 11, 1, tzinfo=UTC)
        candidates = [
            {
                "selection": "home",
                "probability": 0.71,
                "odds": 1.6,
                "market": "h2h",
                "line": None,
                "feature_completeness": 0.8,
            },
            {
                "selection": "1X",
                "probability": 0.88,
                "odds": 1.2,
                "market": "double_chance",
                "line": None,
                "feature_completeness": 0.8,
            },
        ]

    frozen = _Frozen()

    # Unfiltered, the better-value candidate wins -- whichever that is.
    assert apply_frozen(BestPick, frozen) is not None

    # Narrowed, each market returns ITS OWN candidate, not the overall winner.
    h2h = apply_frozen(BestPick, frozen, market="h2h")
    dc = apply_frozen(BestPick, frozen, market="double_chance")
    assert h2h is not None and h2h.market == "h2h"
    assert dc is not None and dc.market == "double_chance"

    # A market this card never had returns nothing, rather than falling back to the winner.
    assert apply_frozen(BestPick, frozen, market="corners_total") is None


# ---------------------------------------------------------------------------------------------
# Which chips a SPORT is offered at all -- distinct from how many picks a market has today.
# ---------------------------------------------------------------------------------------------


def test_basketball_and_tennis_are_offered_only_the_market_they_can_produce():
    """A chip for a market a sport can never fill is a dead end on every day, not a quiet one.

    Measured in production 2026-11-03 at the default 60% / 1.01: nba and tennis return 0 for
    double chance, goals and corners while h2h returns 128 and 102. Those zeros are structural
    -- _all_market_candidates derives double chance from draw_prob, goals from xg_home/away and
    corners from corners_xg_*, and all three are NULL for both sports.
    """
    from app.fixtures.router import _applicable_markets_by_sport

    applicable = _applicable_markets_by_sport({})
    assert applicable["nba"] == ["h2h"]
    assert applicable["tennis"] == ["h2h"]
    # Football keeps all four, corners included: its corners count is 0 on a quiet day, which
    # is the OTHER case entirely and still earns a dimmed, tappable chip.
    assert applicable["football"] == list(MARKET_FAMILIES)


def test_an_empty_day_still_says_which_chips_a_sport_should_have():
    """Applicability is a property of the SPORT, so a day with no fixtures must not collapse
    the row -- chips that come and go between days are the thing that got them removed once."""
    from app.fixtures.router import MARKETS_BY_SPORT, _applicable_markets_by_sport

    assert set(_applicable_markets_by_sport({})) >= set(MARKETS_BY_SPORT)


def test_a_market_with_real_picks_is_offered_even_though_it_is_not_declared(caplog):
    """THE FAIL-SAFE, and the reason this is a union rather than a lookup.

    MARKETS_BY_SPORT is a hand-maintained declaration, so it will eventually be wrong -- the
    day a basketball totals model lands, nothing would otherwise prompt anyone to update it.
    A stale map must be able to show a useless chip and must NEVER hide a market holding real
    picks, so anything that actually counted is offered regardless, and logged so the staleness
    is visible rather than silent.
    """
    import logging

    from app.fixtures.router import _applicable_markets_by_sport

    with caplog.at_level(logging.WARNING):
        applicable = _applicable_markets_by_sport({"nba": {"h2h": 7, "goals_total": 3}})

    assert applicable["nba"] == ["h2h", "goals_total"]
    assert any("market_chip_map_stale" in record.message for record in caplog.records)


def test_a_declared_market_with_no_picks_today_is_still_offered():
    """The whole point of the split: 0 means "nothing today" and keeps its chip, dimmed."""
    from app.fixtures.router import _applicable_markets_by_sport

    applicable = _applicable_markets_by_sport({"football": {"h2h": 4, "corners_total": 0}})
    assert applicable["football"] == list(MARKET_FAMILIES)


def test_an_unknown_sport_is_offered_every_market():
    """Matching offers_corners' own default for an unknown league: withholding a market from a
    newly-added sport is a worse failure than showing it and finding out."""
    from app.fixtures.router import MARKETS_BY_SPORT, _applicable_markets_by_sport

    assert "cricket" not in MARKETS_BY_SPORT
    applicable = _applicable_markets_by_sport({"cricket": {}})
    assert applicable["cricket"] == list(MARKET_FAMILIES)


def test_the_pooled_counts_are_the_breakdown_summed():
    """`counts` is published and predates the breakdown, so it must keep meaning exactly what
    it meant -- every sport's picks added together."""
    from app.fixtures.router import _pooled_counts

    pooled = _pooled_counts({"football": {"h2h": 3, "goals_total": 5}, "tennis": {"h2h": 4}})
    assert pooled["h2h"] == 7
    assert pooled["goals_total"] == 5
    assert pooled["corners_total"] == 0


@pytest.mark.asyncio
async def test_the_breakdown_a_sport_filter_reports_matches_that_sport_alone(api_client):
    """The feed is fetched for a whole day WITHOUT a sport param and narrowed on the client, so
    the breakdown is the only thing that can describe what is on screen.

    Checked against the server narrowing itself, which is a different code path: asking for one
    sport must report the same numbers as that sport's slice of the unnarrowed breakdown. Before
    this existed the client used the pooled figure, and with Tennis selected the double-chance
    chip showed football's count and delivered nothing.
    """
    everything = (await api_client.get("/fixtures/market-counts?limit=200")).json()
    breakdown = everything.get("counts_by_sport") or {}
    for sport, expected in breakdown.items():
        if not sport:  # a fixture whose sport did not resolve; no slug to narrow by
            continue
        narrowed = (
            await api_client.get(f"/fixtures/market-counts?limit=200&sport_slug={sport}")
        ).json()
        for family, count in expected.items():
            assert narrowed["counts"].get(family, 0) == count, (
                f"{sport}/{family}: breakdown says {count}, narrowing says "
                f"{narrowed['counts'].get(family, 0)}"
            )
