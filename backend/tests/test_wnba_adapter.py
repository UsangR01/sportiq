"""The WNBA shares a Sport row with the NBA, which makes id collisions the headline risk.

Both competitions live under Sport(slug="nba") so they share one trained model -- the same
arrangement that has 18 football leagues sharing one artefact. But BallDontLie numbers each
namespace's teams and games from 1, and Team/Fixture uniqueness is keyed on
(sport_id, external_id) rather than league_id, so an unprefixed WNBA team 1 would BE NBA team 1:
one row, two competitions, silently merged Elo and form. Identical hazard to ATP/WTA sharing one
tennis Sport, and fixed the same way.

The schemas also differ more than they appear to. Sampled live 2026-08-13:

    NBA      home_team_score / visitor_team_score   datetime   status "Final"
    WNBA     home_score      / away_score           date       status "post" + state "final"

A scheduled WNBA game reports period 0 and 0-0 rather than nulls, so a naive mapping shows a
live-looking 0-0 on a card for a game that has not tipped off.
"""

import pytest

from app.adapters.balldontlie import (
    _current_season,
    _map_game_to_fixture_payload,
    _normalise_game,
    league_external_id,
    strip_league_prefix,
)

# Shapes taken from real responses, not invented.
WNBA_FINAL = {
    "id": 3858,
    "date": "2025-05-16T23:30:00.000Z",
    "season": 2025,
    "postseason": False,
    "status": "post",
    "status_state": "final",
    "period": 4,
    "time": "0.0",
    "home_team": {"id": 5, "full_name": "Washington Mystics", "abbreviation": "WSH"},
    "visitor_team": {"id": 1, "full_name": "New York Liberty", "abbreviation": "NYL"},
    "home_score": 94,
    "away_score": 90,
}
WNBA_SCHEDULED = {
    "id": 25004,
    "date": "2026-08-13T23:00:00.000Z",
    "season": 2026,
    "postseason": False,
    "status": "pre",
    "status_state": "scheduled",
    "period": 0,
    "time": "0.0",
    "home_team": {"id": 11, "full_name": "Dallas Wings", "abbreviation": "DAL"},
    "visitor_team": {"id": 30, "full_name": "Toronto Tempo", "abbreviation": "TOR"},
    "home_score": 0,
    "away_score": 0,
}
NBA_FINAL = {
    "id": 3858,  # DELIBERATELY the same id as WNBA_FINAL -- that is the collision
    "datetime": "2025-05-16T23:30:00Z",
    "season": 2024,
    "status": "Final",
    "period": 4,
    "home_team": {"id": 5, "full_name": "Chicago Bulls", "abbreviation": "CHI"},
    "visitor_team": {"id": 1, "full_name": "Atlanta Hawks", "abbreviation": "ATL"},
    "home_team_score": 101,
    "visitor_team_score": 99,
}


def test_the_same_provider_id_in_both_leagues_does_not_collide():
    """THE GUARD. Both fixtures are id 3858 and both teams are ids 5 and 1. Under one Sport row
    that is one Team and one Fixture unless the WNBA side is namespaced."""
    nba = _map_game_to_fixture_payload(NBA_FINAL, "nba")
    wnba = _map_game_to_fixture_payload(WNBA_FINAL, "wnba")
    assert nba.external_id != wnba.external_id
    assert nba.home_team_external_id != wnba.home_team_external_id
    assert nba.away_team_external_id != wnba.away_team_external_id


def test_nba_ids_stay_bare_so_existing_rows_are_not_orphaned():
    """The asymmetry is deliberate. NBA teams, fixtures, predictions and Elo ratings are already
    stored against bare ids; prefixing them now would strand every one of them."""
    nba = _map_game_to_fixture_payload(NBA_FINAL, "nba")
    assert nba.external_id == "3858"
    assert nba.home_team_external_id == "5"
    assert league_external_id("nba", 7) == "7"
    assert league_external_id("wnba", 7) == "wnba:7"
    assert strip_league_prefix("wnba:7") == "7"
    assert strip_league_prefix("7") == "7"


def test_a_scheduled_wnba_game_reports_no_score_rather_than_0_0():
    """WNBA sends 0-0 for a game that has not started. Passing that through would render a
    live-looking 0-0 on the card before tip-off."""
    payload = _map_game_to_fixture_payload(WNBA_SCHEDULED, "wnba")
    assert payload.status == "scheduled"
    assert payload.home_score is None and payload.away_score is None


def test_a_completed_wnba_game_carries_its_real_score():
    payload = _map_game_to_fixture_payload(WNBA_FINAL, "wnba")
    assert payload.status == "completed"
    assert (payload.home_score, payload.away_score) == (94, 90)
    assert payload.league_external_id == "wnba"


def test_the_wnba_status_vocabulary_is_translated_not_guessed():
    """'post'/'final' and 'pre'/'scheduled' are the real values, sampled live. _map_status keys
    off the literal 'Final', so an untranslated WNBA game would read as scheduled forever --
    including ones that had already finished."""
    assert _normalise_game(WNBA_FINAL, "wnba")["status"] == "Final"
    assert _normalise_game(WNBA_SCHEDULED, "wnba")["status"] == "pre"


def test_normalisation_leaves_nba_untouched():
    """Adding this league must not change a single thing NBA computes."""
    assert _normalise_game(NBA_FINAL, "nba") is NBA_FINAL


@pytest.mark.parametrize(
    "league, month, expected",
    [
        ("wnba", 8, 2026),  # mid-season, and the NBA rule would say 2025
        ("wnba", 3, 2026),  # pre-season, still its own calendar year
        ("nba", 8, 2025),  # NBA's Oct boundary: August belongs to the season that started last
        ("nba", 11, 2026),
    ],
)
def test_the_season_conventions_differ(league, month, expected):
    """The WNBA runs May-September inside one calendar year. Applying the NBA's October rule
    would query the WRONG season for the whole of the WNBA's actual season -- the same mistake
    Brasileirao exposed in football."""
    from datetime import UTC, datetime

    assert _current_season(league, datetime(2026, month, 1, tzinfo=UTC)) == expected


# --- odds: the two providers disagree on four abbreviations ---------------------------------

from app.adapters.therundown import (  # noqa: E402
    _WNBA_ABBREVIATION_ALIASES,
    _apply_abbreviation_aliases,
    _map_event_to_odds_payloads,
)

WNBA_RUNDOWN_SPORT_ID = 8
NBA_RUNDOWN_SPORT_ID = 4


def test_the_four_measured_mismatches_are_translated():
    """Matching keys on the abbreviation string, so a fixture involving any of these could
    never be priced. Measured by joining both providers' full team lists on city + mascot:
    11 of 15 already agreed, these 4 did not."""
    for rundown, ours in _WNBA_ABBREVIATION_ALIASES.items():
        assert _apply_abbreviation_aliases(rundown, WNBA_RUNDOWN_SPORT_ID) == ours


def test_agreeing_abbreviations_pass_through_untouched():
    for abbr in ("ATL", "CHI", "DAL", "IND", "LA", "MIN", "PHX", "POR", "SEA", "TOR", "WSH"):
        assert _apply_abbreviation_aliases(abbr, WNBA_RUNDOWN_SPORT_ID) == abbr


def test_the_aliases_are_scoped_to_the_wnba():
    """LAS and NYL are free in the NBA today, but a blanket rename is the kind of thing that
    silently mis-prices a different league later."""
    assert _apply_abbreviation_aliases("LAS", NBA_RUNDOWN_SPORT_ID) == "LAS"
    assert _apply_abbreviation_aliases("NYL", NBA_RUNDOWN_SPORT_ID) == "NYL"


def test_a_real_wnba_event_maps_onto_our_abbreviations():
    """Shape taken from a real /sports/8/events response: Las Vegas at home to Washington."""
    event = {
        "event_id": "wnba-test-1",
        "event_date": "2026-08-14T23:00:00Z",
        "teams_normalized": [
            {"abbreviation": "WSH", "name": "Washington", "is_home": False},
            {"abbreviation": "LAS", "name": "Las Vegas", "is_home": True},
        ],
        "lines": {
            "1": {
                "affiliate": {"affiliate_name": "Bovada"},
                "moneyline": {"moneyline_home": -250, "moneyline_away": 200},
            }
        },
    }
    payloads = _map_event_to_odds_payloads(event, rundown_sport_id=WNBA_RUNDOWN_SPORT_ID)
    assert payloads, "a real moneyline event must produce at least one payload"
    assert payloads[0].home_team_short_name == "LV"  # not LAS
    assert payloads[0].away_team_short_name == "WSH"


# --- the two expansion teams the provider ships with no city ------------------------------


def test_the_expansion_teams_get_their_full_names():
    """BallDontLie ships Portland Fire and Toronto Tempo with an EMPTY city and a mascot-only
    full_name, so `full_name or name` correctly produced "Fire" and "Tempo" and that is what
    the cards read. Confirmed live 2026-08-16 against /wnba/v1/teams."""
    game = {
        **WNBA_FINAL,
        "home_team": {"id": 31, "full_name": "Fire", "name": "Fire", "abbreviation": "POR"},
        "visitor_team": {"id": 30, "full_name": "Tempo", "name": "Tempo", "abbreviation": "TOR"},
    }
    payload = _map_game_to_fixture_payload(game, "wnba")

    assert payload.home_team_name == "Portland Fire"
    assert payload.away_team_name == "Toronto Tempo"


def test_a_team_the_provider_names_properly_is_left_alone():
    game = {
        **WNBA_FINAL,
        "home_team": {
            "id": 4,
            "full_name": "Atlanta Dream",
            "name": "Dream",
            "abbreviation": "ATL",
        },
        "visitor_team": {
            "id": 9,
            "full_name": "Seattle Storm",
            "name": "Storm",
            "abbreviation": "SEA",
        },
    }
    payload = _map_game_to_fixture_payload(game, "wnba")

    assert payload.home_team_name == "Atlanta Dream"
    assert payload.away_team_name == "Seattle Storm"


def test_the_override_cannot_rename_an_nba_team():
    """POR is Portland Trail Blazers in the NBA namespace and Portland Fire in the WNBA one, and
    the two share a Sport row -- so the override is keyed on the provider id AND the league."""
    game = {
        **NBA_FINAL,
        "home_team": {"id": 31, "full_name": "Portland Trail Blazers", "abbreviation": "POR"},
    }
    payload = _map_game_to_fixture_payload(game, "nba")

    assert payload.home_team_name == "Portland Trail Blazers"
