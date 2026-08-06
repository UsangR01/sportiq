"""AdapterFactory.get_odds_adapters — football queries both TheRundown and API-Football
(complementary per-league real odds coverage, see CLAUDE.md); every other sport still gets
only TheRundown, matching the original "odds is always TheRundown" design where it still
holds true."""

from app.adapters.api_football import APIFootballAdapter
from app.adapters.balldontlie_tennis import BallDontLieTennisAdapter
from app.adapters.factory import AdapterFactory
from app.adapters.therundown import TheRundownAdapter


def test_football_gets_both_odds_adapters():
    adapters = AdapterFactory.get_odds_adapters("football")
    types = {type(a) for a in adapters}
    assert types == {TheRundownAdapter, APIFootballAdapter}


def test_nba_gets_only_therundown():
    adapters = AdapterFactory.get_odds_adapters("nba")
    assert [type(a) for a in adapters] == [TheRundownAdapter]


def test_unknown_sport_defaults_to_therundown_only():
    adapters = AdapterFactory.get_odds_adapters("some-future-sport")
    assert [type(a) for a in adapters] == [TheRundownAdapter]


def test_tennis_gets_both_therundown_and_balldontlie_odds():
    """Tennis pairs two odds providers for a per-MARKET reason, not football's per-league one.

    BallDontLie (GOAT tier) carries moneyline only — no totals market at all — so TheRundown
    remains the sole source of game-totals lines. BallDontLie in turn is the tennis fixtures
    provider, so its match_id joins Fixture.external_id directly instead of going through the
    team-name fuzzy match that has already produced missed and inverted tennis prices. Order
    matters only for merge precedence, not correctness.
    """
    adapters = AdapterFactory.get_odds_adapters("tennis")
    assert [type(a) for a in adapters] == [TheRundownAdapter, BallDontLieTennisAdapter]


def test_tennis_stats_adapter_is_balldontlie_tennis():
    adapter = AdapterFactory.get_stats_adapter("tennis")
    assert isinstance(adapter, BallDontLieTennisAdapter)


def test_tennis_has_no_injury_adapter():
    assert AdapterFactory.get_injury_adapter("tennis") is None
