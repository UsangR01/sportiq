"""AdapterFactory.get_odds_adapters — football queries both TheRundown and API-Football
(complementary per-league real odds coverage, see CLAUDE.md); every other sport still gets
only TheRundown, matching the original "odds is always TheRundown" design where it still
holds true."""

from app.adapters.api_football import APIFootballAdapter
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
