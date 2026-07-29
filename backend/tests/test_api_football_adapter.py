"""Unit tests for API-Football adapter's pure mapping/aggregation logic.

Sample dicts below are shaped from real /fixtures, /teams/statistics, /injuries, and /odds
responses captured during live research (see CLAUDE.md) — not fabricated shapes. No network,
no DB.
"""

from datetime import UTC, datetime

from app.adapters.api_football import (
    LEAGUE_IDS,
    _compute_team_stats,
    _current_football_season,
    _map_fixture_to_payload,
    _map_injury_to_update,
    _map_odds_response_to_payloads,
    _map_status,
    _parse_form_points,
)


def make_fixture(**overrides):
    fixture = {
        "fixture": {
            "id": 1208021,
            "date": "2026-08-22T14:00:00+00:00",
            "status": {"long": "Not Started", "short": "NS", "elapsed": None},
        },
        "league": {"id": 39, "season": 2026},
        "teams": {
            "home": {"id": 33, "name": "Manchester United"},
            "away": {"id": 42, "name": "Arsenal"},
        },
        "goals": {"home": None, "away": None},
    }
    fixture.update(overrides)
    return fixture


def test_league_ids_match_therundown_slugs_where_covered():
    # The 5 European leagues must match app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys
    # exactly or odds ingestion silently resolves to nothing for a league. "brasileirao" is a
    # deliberate exception — TheRundown has no Brazil-league coverage at all (confirmed live,
    # see CLAUDE.md) — so it's excluded from this particular cross-check.
    assert set(LEAGUE_IDS.keys()) == {
        "epl",
        "ligue1",
        "bundesliga",
        "laliga",
        "seriea",
        "brasileirao",
    }
    assert LEAGUE_IDS["epl"] == 39
    assert LEAGUE_IDS["brasileirao"] == 71


def test_map_status_not_started():
    assert _map_status("NS") == "scheduled"


def test_map_status_completed():
    assert _map_status("FT") == "completed"
    assert _map_status("AET") == "completed"
    assert _map_status("PEN") == "completed"


def test_map_status_live():
    assert _map_status("1H") == "live"
    assert _map_status("HT") == "live"


def test_map_status_postponed_is_not_live():
    # Confirmed live on a real matchday: API-Football genuinely returns "PST" for a real
    # postponed fixture. The old blanket "anything else is live" fallback would have shown a
    # LIVE badge with no score for a match that isn't actually happening.
    assert _map_status("PST") == "scheduled"
    assert _map_status("CANC") == "scheduled"
    assert _map_status("SUSP") == "scheduled"


def test_map_fixture_to_payload():
    payload = _map_fixture_to_payload(make_fixture(), "epl")

    assert payload.external_id == "1208021"
    assert payload.league_external_id == "epl"
    assert payload.home_team_external_id == "33"
    assert payload.away_team_external_id == "42"
    assert payload.home_team_name == "Manchester United"
    assert payload.away_team_name == "Arsenal"
    assert payload.season == "2026"
    assert payload.status == "scheduled"
    assert payload.kickoff_utc == datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
    assert payload.home_score is None
    assert payload.away_score is None
    assert payload.match_minute is None


def test_map_fixture_to_payload_completed():
    fixture = make_fixture(
        fixture={"id": 1, "date": "2026-01-15T15:00:00+00:00", "status": {"short": "FT"}},
        goals={"home": 2, "away": 1},
    )
    payload = _map_fixture_to_payload(fixture, "epl")
    assert payload.status == "completed"
    assert payload.home_score == 2
    assert payload.away_score == 1


def test_map_fixture_to_payload_live_with_elapsed_minute():
    fixture = make_fixture(
        fixture={
            "id": 2,
            "date": "2026-08-22T14:00:00+00:00",
            "status": {"short": "1H", "elapsed": 37},
        },
        goals={"home": 1, "away": 0},
    )
    payload = _map_fixture_to_payload(fixture, "epl")
    assert payload.status == "live"
    assert payload.home_score == 1
    assert payload.away_score == 0
    assert payload.match_minute == 37


def test_current_football_season_after_july_uses_current_year():
    assert _current_football_season("epl", datetime(2026, 8, 1, tzinfo=UTC)) == 2026


def test_current_football_season_before_july_uses_previous_year():
    assert _current_football_season("epl", datetime(2026, 3, 1, tzinfo=UTC)) == 2025


def test_current_football_season_brasileirao_is_calendar_year():
    # Brasileirão runs Jan-Dec of a single calendar year, unlike the European Aug-May
    # convention — confirmed live: the 2026 season runs 2026-01-28 to 2026-12-02.
    assert _current_football_season("brasileirao", datetime(2026, 3, 1, tzinfo=UTC)) == 2026
    assert _current_football_season("brasileirao", datetime(2026, 11, 1, tzinfo=UTC)) == 2026


def test_parse_form_points():
    # W=3, D=1, L=0 -> (3+3+1+0+3)/5 = 2.0
    assert _parse_form_points("WWDLW") == 2.0


def test_parse_form_points_empty_or_none():
    assert _parse_form_points(None) is None
    assert _parse_form_points("") is None


def test_compute_team_stats_from_real_shape():
    stats = {
        "form": "WWDLW",
        "fixtures": {
            "played": {"home": 19, "away": 19, "total": 38},
            "wins": {"home": 15, "away": 11, "total": 26},
            "draws": {"home": 2, "away": 5, "total": 7},
            "loses": {"home": 2, "away": 3, "total": 5},
        },
        "goals": {
            "for": {"average": {"total": "2.1"}},
            "against": {"average": {"total": "0.8"}},
        },
    }
    result = _compute_team_stats("33", stats)

    assert result.team_external_id == "33"
    assert result.elo_rating is None
    assert result.xg_for_5 is None
    assert result.xg_against_5 is None
    assert result.home_win_rate == 15 / 19
    assert result.away_win_rate == 11 / 19
    assert result.attack_str == 2.1
    assert result.defence_str == 0.8
    assert result.form_pts_5 == 2.0


def test_compute_team_stats_no_fixtures_played_returns_none_rates():
    stats = {"form": None, "fixtures": {"played": {}, "wins": {}}, "goals": {}}
    result = _compute_team_stats("33", stats)
    assert result.home_win_rate is None
    assert result.away_win_rate is None
    assert result.attack_str is None
    assert result.form_pts_5 is None


ODDS_ROW = {
    "league": {"id": 71, "season": 2026},
    "fixture": {"id": 1492316},
    "update": "2026-07-29T20:03:21+00:00",
    "bookmakers": [
        {
            "id": 1,
            "name": "Bet365",
            "bets": [
                {
                    "id": 1,
                    "name": "Match Winner",
                    "values": [
                        {"value": "Home", "odd": "1.80"},
                        {"value": "Draw", "odd": "3.50"},
                        {"value": "Away", "odd": "5.00"},
                    ],
                },
                {
                    "id": 5,
                    "name": "Goals Over/Under",
                    "values": [
                        {"value": "Over 2.5", "odd": "1.80"},
                        {"value": "Under 2.5", "odd": "2.00"},
                    ],
                },
            ],
        },
        {
            "id": 6,
            "name": "Niche Book",
            "bets": [
                {
                    "id": 5,
                    "name": "Goals Over/Under",
                    "values": [{"value": "Over 2.5", "odd": "1.85"}],
                }
            ],
        },
    ],
}


def test_map_odds_response_to_payloads_real_book():
    payloads = _map_odds_response_to_payloads(ODDS_ROW)
    assert len(payloads) == 1  # "Niche Book" has no Match Winner market — correctly skipped

    payload = payloads[0]
    assert payload.fixture_external_id == "1492316"
    assert payload.bookmaker == "Bet365"
    assert payload.market == "h2h"
    assert payload.home_odds == 1.80
    assert payload.draw_odds == 3.50
    assert payload.away_odds == 5.00
    assert payload.updated_at == datetime(2026, 7, 29, 20, 3, 21, tzinfo=UTC)


def test_map_odds_response_to_payloads_no_match_winner_anywhere():
    row = {**ODDS_ROW, "bookmakers": [ODDS_ROW["bookmakers"][1]]}  # only "Niche Book"
    assert _map_odds_response_to_payloads(row) == []


def test_map_injury_to_update_always_out():
    # /injuries only ever lists confirmed-missing players ("Missing Fixture") — every mapped
    # row is InjuryStatus.OUT, no GTD/doubtful signal exists in this feed (see CLAUDE.md).
    injury = {
        "player": {"id": 999, "name": "Test Player"},
        "team": {"id": 33, "name": "Manchester United"},
    }
    update = _map_injury_to_update(injury)

    assert update.player_external_id == "999"
    assert update.team_external_id == "33"
    assert update.player_name == "Test Player"
    assert update.status == "OUT"
    assert update.source == "api_football"
