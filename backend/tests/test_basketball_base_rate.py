"""Basketball uses its OWN measured base rates, not football's three-way split.

The gate exists to reject a pick that says nothing beyond a trivially available alternative.
For basketball that alternative is real -- home-court advantage is a genuine causal effect and
"back the home team" is a strategy someone could run -- so unlike tennis, which abstains because
its "home" is only an id tiebreak, basketball's rates are CORRECTED rather than removed.

What they were corrected FROM is the point of these tests. Basketball had no override, so it
fell back to football's table, and football's h2h is a THREE-WAY market whose home share is
0.4582 only because a quarter of its outcomes are draws:

    borrowed   home 0.4582 -> bar 0.5082     away 0.2879 -> bar 0.3379
    measured   home 0.5506 -> bar 0.6006     away 0.4494 -> bar 0.4994

So a basketball home pick at 0.52 PASSED while sitting below the 0.5506 that backing every home
team gets for free. That is the gate admitting picks that say less than nothing -- the identical
fault _TENNIS_BASE_RATES was emptied for, reached by a different route.

Measured from real completed games: NBA 0.5518 (n=7,220, six seasons), WNBA 0.5393 (n=777,
2022-2024), pooled 0.5506.
"""

from app.fixtures.router import (
    BASE_RATES_BY_SPORT,
    MARKET_BASE_RATES,
    MIN_EDGE_OVER_BASE_RATE,
    _base_rate,
    _MarketCandidate,
)


def rate(sport, market, selection, line=None, probability=0.5):
    """_base_rate takes a candidate, so build one -- exercising the real call signature rather
    than a convenient reimplementation of it. Mirrors test_tennis_base_rate_gate.py."""
    return _base_rate(
        _MarketCandidate(
            selection=selection, probability=probability, odds=None, market=market, line=line
        ),
        sport,
    )


def test_basketball_does_not_borrow_footballs_home_rate():
    """The regression this file exists for. Both must be strictly above football's, or the gate
    is judging a two-way market against a three-way split."""
    assert rate("nba", "h2h", "home") == 0.5506
    assert rate("nba", "h2h", "away") == 0.4494
    assert rate("nba", "h2h", "home") > MARKET_BASE_RATES[("h2h", "home", None)]
    assert rate("nba", "h2h", "away") > MARKET_BASE_RATES[("h2h", "away", None)]


def test_basketball_home_and_away_sum_to_one_because_there_is_no_draw():
    """The structural property that makes football's table inapplicable. Football's three h2h
    rates sum to ~1 across home/draw/away; basketball has only two outcomes, so any pair that
    does NOT sum to 1 is describing a different sport."""
    home = rate("nba", "h2h", "home")
    away = rate("nba", "h2h", "away")
    assert abs(home + away - 1.0) < 1e-9
    football = MARKET_BASE_RATES
    assert abs(football[("h2h", "home", None)] + football[("h2h", "away", None)] - 1.0) > 0.2


def test_a_sub_base_rate_home_pick_is_now_correctly_below_bar():
    """The concrete case: 0.52 home, which real production cards were showing.

    Under the borrowed rates it cleared 0.5082 and was displayed. It is BELOW the rate that
    backing every home team achieves, so it carried no information at all.
    """
    borrowed_bar = MARKET_BASE_RATES[("h2h", "home", None)] + MIN_EDGE_OVER_BASE_RATE
    measured_bar = rate("nba", "h2h", "home") + MIN_EDGE_OVER_BASE_RATE
    assert 0.52 > borrowed_bar, "would not have been a bug if it had failed the old bar"
    assert 0.52 < measured_bar
    # And it is genuinely uninformative, not merely below an arbitrary bar.
    assert 0.52 < rate("nba", "h2h", "home")


def test_markets_basketball_does_not_have_stay_absent_rather_than_zeroed():
    """Absent keys make _base_rate return None, which makes the gate ABSTAIN. Zeroing them
    would instead make every such pick look infinitely above bar. Basketball has no draw, so
    double_chance cannot exist for it, and goals/corners are football markets."""
    for market, selection, line in [
        ("h2h", "draw", None),
        ("double_chance", "1X", None),
        ("goals_total", "over", 2.5),
        ("corners_total", "under", 9.5),
    ]:
        assert rate("nba", market, selection, line) is None


def test_the_wnba_shares_the_rate_because_both_leagues_share_one_sport_row():
    """BASE_RATES_BY_SPORT has no league dimension and the WNBA lives under Sport(slug="nba"),
    so one rate serves both. That is only defensible because they were checked against each
    other and agree to 1.25pp -- NBA 0.5518, WNBA 0.5393.

    It would NOT be defensible for the European leagues, which span 0.571 to 0.649, so this
    asserts the single-rate assumption is a measured choice and not an oversight.
    """
    assert "nba" in BASE_RATES_BY_SPORT
    assert abs(0.5518 - 0.5393) < 0.02
    assert abs(rate("nba", "h2h", "home") - 0.5518) < 0.02
    assert abs(rate("nba", "h2h", "home") - 0.5393) < 0.02


def test_football_and_tennis_are_untouched():
    """Scoping. A sport-specific override must not reach into the shared table."""
    assert rate("football", "h2h", "home") == MARKET_BASE_RATES[("h2h", "home", None)]
    assert rate("football", "h2h", "away") == MARKET_BASE_RATES[("h2h", "away", None)]
    assert rate("tennis", "h2h", "home") is None
    assert rate(None, "h2h", "home") == MARKET_BASE_RATES[("h2h", "home", None)]
