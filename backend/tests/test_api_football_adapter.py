"""Unit tests for API-Football adapter's pure mapping/aggregation logic.

Sample dicts below are shaped from real /fixtures, /teams/statistics, /injuries, and /odds
responses captured during live research (see CLAUDE.md) — not fabricated shapes. No network,
no DB.
"""

from datetime import UTC, datetime

import pytest

from app.adapters.api_football import (
    LEAGUE_IDS,
    MatchStats,
    _compute_team_stats,
    _current_football_season,
    _goals_from_home_side_perspective,
    _map_fixture_to_payload,
    _map_injury_to_update,
    _map_odds_response_to_payloads,
    _map_status,
    _parse_form_points,
    _parse_h2h_detail,
    _parse_match_stats_block,
    _parse_streaks,
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
    # Every league TheRundown's own _RUNDOWN_SPORT_IDS actually covers must also match its
    # slug here exactly, or odds ingestion silently resolves to nothing for that league.
    # brasileirao/scottish_prem/csl are deliberate exceptions — confirmed live that
    # TheRundown's own /sports list has no Brazil, Scotland, or China league entry at all
    # (see CLAUDE.md) — real odds for these three come from API-Football's own /odds instead.
    from app.adapters.therundown import _RUNDOWN_SPORT_IDS

    no_rundown_coverage = {"brasileirao", "scottish_prem", "csl"}
    assert set(LEAGUE_IDS.keys()) == {
        "epl",
        "ligue1",
        "bundesliga",
        "laliga",
        "seriea",
        "brasileirao",
        "scottish_prem",
        "mls",
        "csl",
    }
    for league_slug in LEAGUE_IDS:
        if league_slug in no_rundown_coverage:
            assert league_slug not in _RUNDOWN_SPORT_IDS
        else:
            assert league_slug in _RUNDOWN_SPORT_IDS

    assert LEAGUE_IDS["epl"] == 39
    assert LEAGUE_IDS["brasileirao"] == 71
    assert LEAGUE_IDS["scottish_prem"] == 179
    assert LEAGUE_IDS["mls"] == 253
    assert LEAGUE_IDS["csl"] == 169


def test_calendar_year_season_leagues_are_exactly_the_non_european_convention_ones():
    from app.adapters.api_football import CALENDAR_YEAR_SEASON_LEAGUES

    assert CALENDAR_YEAR_SEASON_LEAGUES == {"brasileirao", "mls", "csl"}
    assert _current_football_season("scottish_prem", datetime(2026, 1, 15, tzinfo=UTC)) == 2025
    assert _current_football_season("mls", datetime(2026, 1, 15, tzinfo=UTC)) == 2026
    assert _current_football_season("csl", datetime(2026, 1, 15, tzinfo=UTC)) == 2026


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
    # postponed fixture. Previously bucketed into "scheduled" (see git history) — which turned
    # out to be actively misleading, since a scheduled fixture still gets a normal market
    # prediction/odds badge in the Picks feed. Now its own real status instead.
    assert _map_status("PST") == "postponed"
    assert _map_status("CANC") == "postponed"
    assert _map_status("SUSP") == "postponed"
    assert _map_status("ABD") == "postponed"
    assert _map_status("INT") == "postponed"
    assert _map_status("TBD") == "postponed"
    assert _map_status("AWD") == "postponed"
    assert _map_status("WO") == "postponed"


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
    # "WWDLW" (most-recent last) -> most recent result is a win, immediately preceded by a
    # loss, so the current win streak is exactly 1.
    assert result.win_streak == 1.0
    assert result.losing_streak == 0.0


def test_compute_team_stats_no_fixtures_played_returns_none_rates():
    stats = {"form": None, "fixtures": {"played": {}, "wins": {}}, "goals": {}}
    result = _compute_team_stats("33", stats)
    assert result.home_win_rate is None
    assert result.away_win_rate is None
    assert result.attack_str is None
    assert result.form_pts_5 is None
    assert result.win_streak is None
    assert result.losing_streak is None


def test_parse_streaks_win_streak():
    assert _parse_streaks("LLWWW") == (3.0, 0.0)


def test_parse_streaks_losing_streak():
    assert _parse_streaks("WWLLL") == (0.0, 3.0)


def test_parse_streaks_most_recent_draw_breaks_both_streaks():
    assert _parse_streaks("WWWWD") == (0.0, 0.0)


def test_parse_streaks_none_or_empty():
    assert _parse_streaks(None) == (None, None)
    assert _parse_streaks("") == (None, None)


def test_goals_from_home_side_perspective_when_queried_team_was_away():
    fx = {
        "fixture": {"status": {"short": "FT"}},
        "teams": {"home": {"id": 50}, "away": {"id": 33}},
        "goals": {"home": 2, "away": 1},
    }
    # team 33 played AWAY in this historical meeting — its own goals are fx["goals"]["away"].
    assert _goals_from_home_side_perspective(fx, "33") == (1, 2)


def test_goals_from_home_side_perspective_when_queried_team_was_home():
    fx = {
        "fixture": {"status": {"short": "FT"}},
        "teams": {"home": {"id": 33}, "away": {"id": 50}},
        "goals": {"home": 2, "away": 1},
    }
    assert _goals_from_home_side_perspective(fx, "33") == (2, 1)


def test_goals_from_home_side_perspective_missing_goals_returns_none():
    fx = {
        "fixture": {"status": {"short": "FT"}},
        "teams": {"home": {"id": 33}, "away": {"id": 50}},
        "goals": {"home": None, "away": None},
    }
    assert _goals_from_home_side_perspective(fx, "33") is None


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
    # Bet365: Match Winner (h2h) + Goals Over/Under 2.5 (total). "Niche Book" has no Match
    # Winner market but DOES have a Goals Over/Under 2.5 line — also mapped, since this
    # function maps every supported bet type independently per bookmaker, not "all or nothing".
    assert len(payloads) == 3

    by_bookmaker_market = {(p.bookmaker, p.market): p for p in payloads}

    h2h = by_bookmaker_market[("Bet365", "h2h")]
    assert h2h.fixture_external_id == "1492316"
    assert h2h.home_odds == 1.80
    assert h2h.draw_odds == 3.50
    assert h2h.away_odds == 5.00
    assert h2h.updated_at == datetime(2026, 7, 29, 20, 3, 21, tzinfo=UTC)

    bet365_total = by_bookmaker_market[("Bet365", "total")]
    assert bet365_total.line == 2.5
    assert bet365_total.over_odds == 1.80
    assert bet365_total.under_odds == 2.00

    niche_total = by_bookmaker_market[("Niche Book", "total")]
    assert niche_total.line == 2.5
    assert niche_total.over_odds == 1.85
    assert niche_total.under_odds is None  # "Niche Book" never posted an Under 2.5 price


def test_map_odds_response_to_payloads_no_match_winner_anywhere():
    """A bookmaker with no Match Winner market still contributes whichever OTHER supported
    markets it does have (here, Goals Over/Under) — h2h absence doesn't suppress everything."""
    row = {**ODDS_ROW, "bookmakers": [ODDS_ROW["bookmakers"][1]]}  # only "Niche Book"
    payloads = _map_odds_response_to_payloads(row)
    assert len(payloads) == 1
    assert payloads[0].market == "total"
    assert payloads[0].bookmaker == "Niche Book"


def test_map_odds_response_to_payloads_double_chance():
    row = {
        **ODDS_ROW,
        "bookmakers": [
            {
                "id": 1,
                "name": "Bet365",
                "bets": [
                    {
                        "id": 12,
                        "name": "Double Chance",
                        "values": [
                            {"value": "Home/Draw", "odd": "1.18"},
                            {"value": "Home/Away", "odd": "1.22"},
                            {"value": "Draw/Away", "odd": "2.05"},
                        ],
                    }
                ],
            }
        ],
    }
    payloads = _map_odds_response_to_payloads(row)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.market == "double_chance"
    assert payload.home_odds == 1.18  # Home/Draw (1X)
    assert payload.away_odds == 2.05  # Draw/Away (X2)
    assert payload.draw_odds is None


def test_map_odds_response_to_payloads_corners_total_only_maps_supported_line():
    row = {
        **ODDS_ROW,
        "bookmakers": [
            {
                "id": 1,
                "name": "Unibet",
                "bets": [
                    {
                        "id": 45,
                        "name": "Corners Over Under",
                        "values": [
                            {"value": "Over 8.5", "odd": "1.50"},
                            {"value": "Under 8.5", "odd": "2.40"},
                            {"value": "Over 9.5", "odd": "1.81"},
                            {"value": "Under 9.5", "odd": "1.87"},
                        ],
                    }
                ],
            }
        ],
    }
    payloads = _map_odds_response_to_payloads(row)
    # Only the 9.5 line is mapped (CORNERS_LINES) — 8.5 isn't a line this product surfaces.
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.market == "corners_total"
    assert payload.line == 9.5
    assert payload.over_odds == 1.81
    assert payload.under_odds == 1.87


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


def _meeting(
    home_id, home_name, away_id, away_name, home_goals, away_goals, meeting_date, fixture_id=1000
):
    return {
        "fixture": {
            "id": fixture_id,
            "date": meeting_date,
            "status": {"long": "Match Finished", "short": "FT", "elapsed": 90},
        },
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {"home": home_goals, "away": away_goals},
    }


def test_parse_h2h_detail_basic_record():
    # Home side (id 33) won meeting 1, drew meeting 2, lost meeting 3 — all with 33 as the
    # HISTORICAL home team too, the simplest case.
    meetings = [
        _meeting(33, "Man United", 42, "Arsenal", 2, 0, "2025-01-01T15:00:00+00:00"),
        _meeting(33, "Man United", 42, "Arsenal", 1, 1, "2024-06-01T15:00:00+00:00"),
        _meeting(33, "Man United", 42, "Arsenal", 0, 3, "2024-01-01T15:00:00+00:00"),
    ]
    detail = _parse_h2h_detail(meetings, "33", {})

    assert detail.meetings_count == 3
    assert detail.home_wins == 1
    assert detail.draws == 1
    assert detail.away_wins == 1
    assert detail.avg_goals_home == pytest.approx((2 + 1 + 0) / 3)
    assert detail.avg_goals_away == pytest.approx((0 + 1 + 3) / 3)
    # No match_stats_by_fixture entries supplied — every corners/shots/possession average must
    # be None, never a fabricated value.
    assert detail.avg_corners_home is None
    assert detail.avg_shots_home is None
    assert detail.avg_possession_home is None


def test_parse_h2h_detail_flips_perspective_when_team_was_away_in_past_meeting():
    """home_external_id (33) played AWAY in this past meeting and won 3-1 — must count as a
    home_wins for 33 (the CURRENT fixture's home team), not an away_win, even though 33 was
    the historical away side. Same reasoning as _goals_from_home_side_perspective."""
    meetings = [_meeting(42, "Arsenal", 33, "Man United", 1, 3, "2025-01-01T15:00:00+00:00")]
    detail = _parse_h2h_detail(meetings, "33", {})

    assert detail.home_wins == 1
    assert detail.away_wins == 0
    assert detail.avg_goals_home == 3.0
    assert detail.avg_goals_away == 1.0


def test_parse_h2h_detail_none_with_no_meetings():
    assert _parse_h2h_detail([], "33", {}) is None


def test_parse_h2h_detail_skips_meetings_with_missing_goals():
    meetings = [
        _meeting(33, "Man United", 42, "Arsenal", None, None, "2025-01-01T15:00:00+00:00"),
        _meeting(33, "Man United", 42, "Arsenal", 2, 1, "2024-01-01T15:00:00+00:00"),
    ]
    detail = _parse_h2h_detail(meetings, "33", {})

    assert detail.meetings_count == 1
    assert detail.home_wins == 1


def test_parse_h2h_detail_averages_match_stats_per_side_across_meetings():
    """Real match-stats averaging, respecting perspective: meeting 1 has 33 as historical home
    (looked up directly by team id 33), meeting 2 has 33 as historical AWAY (looked up by
    whichever id ISN'T 33) — match_stats is keyed by real provider team id, not home/away, so
    no flip is needed the way goals needed, just a correct lookup either way."""
    meetings = [
        _meeting(33, "Man United", 42, "Arsenal", 2, 0, "2025-01-01T15:00:00+00:00", fixture_id=1),
        _meeting(42, "Arsenal", 33, "Man United", 1, 1, "2024-06-01T15:00:00+00:00", fixture_id=2),
    ]
    match_stats_by_fixture = {
        "1": {
            "33": MatchStats(corners=6, shots=14, shots_on_goal=5, possession_pct=55.0),
            "42": MatchStats(corners=4, shots=10, shots_on_goal=3, possession_pct=45.0),
        },
        "2": {
            "33": MatchStats(corners=8, shots=12, shots_on_goal=6, possession_pct=60.0),
            "42": MatchStats(corners=2, shots=8, shots_on_goal=2, possession_pct=40.0),
        },
    }
    detail = _parse_h2h_detail(meetings, "33", match_stats_by_fixture)

    assert detail.avg_corners_home == pytest.approx((6 + 8) / 2)
    assert detail.avg_corners_away == pytest.approx((4 + 2) / 2)
    assert detail.avg_shots_home == pytest.approx((14 + 12) / 2)
    assert detail.avg_shots_on_goal_away == pytest.approx((3 + 2) / 2)
    assert detail.avg_possession_home == pytest.approx((55.0 + 60.0) / 2)
    assert detail.avg_possession_away == pytest.approx((45.0 + 40.0) / 2)


def test_parse_h2h_detail_averages_only_meetings_with_real_match_stats():
    """A meeting whose /fixtures/statistics call never returned anything (missing from
    match_stats_by_fixture entirely, or missing one specific field) must be excluded from that
    specific average, not treated as a fabricated 0 — same convention as every other
    missing-data path in this file."""
    meetings = [
        _meeting(33, "Man United", 42, "Arsenal", 2, 0, "2025-01-01T15:00:00+00:00", fixture_id=1),
        _meeting(33, "Man United", 42, "Arsenal", 1, 1, "2024-06-01T15:00:00+00:00", fixture_id=2),
    ]
    match_stats_by_fixture = {
        "1": {
            "33": MatchStats(corners=6, shots=None, shots_on_goal=5, possession_pct=None),
            "42": MatchStats(corners=4, shots=None, shots_on_goal=3, possession_pct=None),
        },
        # fixture "2" missing entirely — its own /fixtures/statistics call failed/returned
        # nothing.
    }
    detail = _parse_h2h_detail(meetings, "33", match_stats_by_fixture)

    assert detail.avg_corners_home == 6.0  # only meeting 1 counted
    assert detail.avg_shots_home is None  # neither meeting had a real value
    assert detail.avg_possession_home is None


def test_parse_match_stats_block_real_shape():
    # Confirmed live (see CLAUDE.md): real /fixtures/statistics response shape, including
    # Ball Possession as a string percentage, not a number.
    team_block = {
        "team": {"id": 33, "name": "Manchester United"},
        "statistics": [
            {"type": "Shots on Goal", "value": 5},
            {"type": "Shots off Goal", "value": 7},
            {"type": "Total Shots", "value": 16},
            {"type": "Corner Kicks", "value": 2},
            {"type": "Ball Possession", "value": "27%"},
            {"type": "Red Cards", "value": None},
        ],
    }
    stats = _parse_match_stats_block(team_block)

    assert stats.corners == 2
    assert stats.shots == 16
    assert stats.shots_on_goal == 5
    assert stats.possession_pct == 27.0


def test_parse_match_stats_block_missing_fields_stay_none():
    stats = _parse_match_stats_block({"team": {"id": 33, "name": "X"}, "statistics": []})
    assert stats.corners is None
    assert stats.shots is None
    assert stats.shots_on_goal is None
    assert stats.possession_pct is None
