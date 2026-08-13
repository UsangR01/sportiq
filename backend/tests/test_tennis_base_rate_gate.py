"""Tennis abstains from the base-rate gate, so the pick is the model's favourite.

The gate exists to reject a pick that says nothing beyond a trivially available alternative. In
football that alternative is real: "back the home team" is a strategy a user could run, and the
base rate measures it.

In tennis "home" is not a venue. _home_away_players assigns home = the LOWER BallDontLie player
id, purely so a fixture's sides cannot flip between an early scheduled ingest and a later
completed one. Gating against that ordering had two consequences, both measured on 2026-08-13:

  - the lower-id player is the higher-ranked one 69% of the time, so the base rate encoded "the
    stronger player usually wins" — a fact rank_diff, the model's primary feature, already
    prices in, meaning it was charged twice;
  - two base rates summing to 1 put the bars at 0.672 (home) and 0.428 (away), so against a
    model whose probabilities cluster in 0.44-0.69 the away slot almost always cleared and the
    home slot almost never did. 167 of 669 tennis predictions (25.0%) came out INVERTED: the
    product recommended the player the model rated lower.

These tests pin the fix and, more importantly, the property that made it necessary.
"""

import pytest

from app.fixtures.router import (
    _TENNIS_BASE_RATES,
    MIN_EDGE_OVER_BASE_RATE,
    _base_rate,
    _MarketCandidate,
)


def rate(sport, market, selection, line=None, probability=0.5):
    """_base_rate takes a candidate, not loose fields — build one so the test exercises the
    real call signature rather than a convenient reimplementation of it."""
    return _base_rate(
        _MarketCandidate(
            selection=selection, probability=probability, odds=None, market=market, line=line
        ),
        sport,
    )


def test_tennis_h2h_has_no_base_rate():
    """The gate must ABSTAIN, not evaluate against zero — _base_rate returning None is what
    makes _pick_best skip the check rather than treat every pick as infinitely above bar."""
    assert rate("tennis", "h2h", "home") is None
    assert rate("tennis", "h2h", "away") is None
    assert _TENNIS_BASE_RATES == {}


def test_tennis_abstains_for_markets_it_does_not_have_either():
    """Unchanged behaviour, asserted alongside so the reason stays visible: home/away was moved
    into the same category as these, not given a special case."""
    for market, selection in [
        ("h2h", "draw"),
        ("double_chance", "home_or_draw"),
        ("goals_total", "over"),
        ("corners_total", "under"),
    ]:
        assert rate("tennis", market, selection, 2.5) is None


def test_football_still_gates_on_a_real_home_advantage():
    """The change is tennis-specific. Football's home advantage is a genuine causal effect and
    "back the home team" is a strategy a user could actually run, so its base rate stays."""
    assert rate("football", "h2h", "home") is not None


def test_the_inversion_that_motivated_this_can_no_longer_happen():
    """THE regression, from a real fixture: Brandon Nakashima v Rafael Jodar, 2026-08-12.

    The model called home at 0.5616 and away at 0.4384, and the match finished HOME_WIN. Under
    the old rates home needed 0.6717 and away needed 0.4283, so the ONLY offerable pick was away
    — the side the model rated lower, which duly lost. With the gate abstaining, neither side is
    rejected on these grounds and the favourite is free to win on expected value.
    """
    home_prob, away_prob = 0.5616, 0.4384
    old_home_bar = 0.6217 + MIN_EDGE_OVER_BASE_RATE
    old_away_bar = 0.3783 + MIN_EDGE_OVER_BASE_RATE
    # The old behaviour, reproduced from the constants so the test states what it prevents.
    assert home_prob < old_home_bar and away_prob >= old_away_bar

    assert rate("tennis", "h2h", "home") is None
    assert rate("tennis", "h2h", "away") is None


@pytest.mark.parametrize("selection,probability", [("home", 0.5616), ("away", 0.4384)])
def test_neither_side_is_rejected_by_a_base_rate_any_more(selection, probability):
    base = rate("tennis", "h2h", selection, probability=probability)
    rejected = base is not None and probability < base + MIN_EDGE_OVER_BASE_RATE
    assert not rejected
