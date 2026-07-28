"""Unit tests for TheRundownAdapter's pure mapping logic.

The event sample below is trimmed from a real /sports/4/events/2026-01-16 response captured
during live research (see CLAUDE.md) — Phoenix Suns @ Detroit Pistons, the same real game
BallDontLie's fixture 18447393 covers (cross-checked: both providers agree on team
abbreviations PHX/DET, confirming the abbreviation-matching strategy is sound). Real odds
values (BetMGM) and real masked-sentinel values (DraftKings, FanDuel) are both preserved
verbatim. No network, no DB.
"""

from datetime import UTC, datetime

from app.adapters.therundown import (
    _american_to_decimal,
    _map_event_to_odds_payloads,
    _rundown_sport_id_for,
)

PHX_DET_EVENT = {
    "event_id": "98397058344a4bbe3a8fb4b7d484b99e",
    "event_date": "2026-01-16T00:00:00Z",
    "teams_normalized": [
        {
            "team_id": 24,
            "name": "Phoenix",
            "mascot": "Suns",
            "abbreviation": "PHX",
            "is_away": True,
            "is_home": False,
        },
        {
            "team_id": 9,
            "name": "Detroit",
            "mascot": "Pistons",
            "abbreviation": "DET",
            "is_away": False,
            "is_home": True,
        },
    ],
    "lines": {
        "22": {
            "moneyline": {
                "moneyline_home": 135,
                "moneyline_away": -190,
                "moneyline_draw": 0,
                "format": "American",
                "date_updated": "2026-02-20T01:03:36.290275Z",
            },
            "affiliate": {"affiliate_id": 22, "affiliate_name": "BetMGM"},
        },
        "19": {
            "moneyline": {
                "moneyline_home": 0.0001,
                "moneyline_away": 0.0001,
                "moneyline_draw": 0.0001,
                "format": "American",
                "date_updated": "2026-01-16T17:10:08.293614Z",
            },
            "affiliate": {"affiliate_id": 19, "affiliate_name": "Draftkings"},
        },
        "23": {
            "moneyline": {
                "moneyline_home": 0.0001,
                "moneyline_away": 0.0001,
                "moneyline_draw": 0.0001,
                "format": "American",
                "date_updated": "2026-01-16T17:10:04.688738Z",
            },
            "affiliate": {"affiliate_id": 23, "affiliate_name": "Fanduel"},
        },
    },
}


def test_rundown_sport_id_for_nba():
    assert _rundown_sport_id_for("nba", "nba") == 4


def test_rundown_sport_id_for_football_league_takes_precedence():
    assert _rundown_sport_id_for("football", "epl") == 11


def test_rundown_sport_id_for_unknown_raises():
    import pytest

    with pytest.raises(ValueError):
        _rundown_sport_id_for("cricket", "ipl")


def test_american_to_decimal_positive():
    # +135 -> 1 + 135/100 = 2.35
    assert _american_to_decimal(135) == 2.35


def test_american_to_decimal_negative():
    # -190 -> 1 + 100/190 ≈ 1.5263
    assert _american_to_decimal(-190) == 1.5263


def test_american_to_decimal_masked_sentinel_is_none():
    assert _american_to_decimal(0.0001) is None


def test_american_to_decimal_zero_is_none():
    assert _american_to_decimal(0) is None


def test_american_to_decimal_missing_is_none():
    assert _american_to_decimal(None) is None


def test_map_event_to_odds_payloads_skips_fully_masked_books():
    payloads = _map_event_to_odds_payloads(PHX_DET_EVENT)

    # Draftkings and Fanduel are fully masked (0.0001 on both sides) — no usable price, so
    # they must not produce a payload at all.
    bookmakers = {p.bookmaker for p in payloads}
    assert "Draftkings" not in bookmakers
    assert "Fanduel" not in bookmakers


def test_map_event_to_odds_payloads_real_book():
    payloads = _map_event_to_odds_payloads(PHX_DET_EVENT)
    betmgm = next(p for p in payloads if p.bookmaker == "BetMGM")

    assert betmgm.fixture_external_id == "98397058344a4bbe3a8fb4b7d484b99e"
    assert betmgm.market == "h2h"
    assert betmgm.home_odds == 2.35  # +135
    assert betmgm.away_odds == round(1 + 100 / 190, 4)  # -190
    assert betmgm.draw_odds is None  # NBA never has a real draw price
    assert betmgm.home_team_short_name == "DET"
    assert betmgm.away_team_short_name == "PHX"
    assert betmgm.kickoff_utc == datetime(2026, 1, 16, 0, 0, tzinfo=UTC)
    assert betmgm.updated_at == datetime(2026, 2, 20, 1, 3, 36, 290275, tzinfo=UTC)
