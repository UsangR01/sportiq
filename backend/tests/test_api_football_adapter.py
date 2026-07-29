"""Unit tests for API-Football adapter's pure mapping/aggregation logic.

Sample dicts below are shaped from real /fixtures, /teams/statistics, and /injuries responses
captured during live research (see CLAUDE.md) — not fabricated shapes. No network, no DB.
"""

from datetime import UTC, datetime

from app.adapters.api_football import (
    LEAGUE_IDS,
    _compute_team_stats,
    _current_football_season,
    _map_fixture_to_payload,
    _map_injury_to_update,
    _map_status,
    _parse_form_points,
)


def make_fixture(**overrides):
    fixture = {
        "fixture": {
            "id": 1208021,
            "date": "2026-08-22T14:00:00+00:00",
            "status": {"long": "Not Started", "short": "NS"},
        },
        "league": {"id": 39, "season": 2026},
        "teams": {
            "home": {"id": 33, "name": "Manchester United"},
            "away": {"id": 42, "name": "Arsenal"},
        },
    }
    fixture.update(overrides)
    return fixture


def test_league_ids_match_therundown_slugs():
    # Must match app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys exactly or odds
    # ingestion silently resolves to nothing for a league.
    assert set(LEAGUE_IDS.keys()) == {"epl", "ligue1", "bundesliga", "laliga", "seriea"}
    assert LEAGUE_IDS["epl"] == 39


def test_map_status_not_started():
    assert _map_status("NS") == "scheduled"


def test_map_status_completed():
    assert _map_status("FT") == "completed"
    assert _map_status("AET") == "completed"
    assert _map_status("PEN") == "completed"


def test_map_status_live():
    assert _map_status("1H") == "live"
    assert _map_status("HT") == "live"


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


def test_map_fixture_to_payload_completed():
    fixture = make_fixture(
        fixture={"id": 1, "date": "2026-01-15T15:00:00+00:00", "status": {"short": "FT"}}
    )
    payload = _map_fixture_to_payload(fixture, "epl")
    assert payload.status == "completed"


def test_current_football_season_after_july_uses_current_year():
    assert _current_football_season(datetime(2026, 8, 1, tzinfo=UTC)) == 2026


def test_current_football_season_before_july_uses_previous_year():
    assert _current_football_season(datetime(2026, 3, 1, tzinfo=UTC)) == 2025


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
