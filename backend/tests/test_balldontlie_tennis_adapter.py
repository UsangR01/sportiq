"""Unit tests for the tennis (ATP/WTA) adapter's pure mapping/aggregation logic.

Sample match/tournament dicts below are shaped per the real OpenAPI specs
(https://www.balldontlie.io/openapi/atp.yml, .../wta.yml, fetched during planning) — NOT
live-verified against a real response, since /matches requires at least the ALL-STAR tier
(see module docstring in app/adapters/balldontlie_tennis.py). No network, no DB.
"""

from datetime import UTC, date, datetime

import httpx
import pytest

from app.adapters.balldontlie_tennis import (
    _compute_team_stats,
    _current_streak,
    _external_id,
    _fetch_all_pages,
    _get_with_retry,
    _latest_rank_points,
    _map_match_to_fixture_payload,
    _map_status,
    _match_winner_id,
    _sets_won,
    _strip_tour_prefix,
    _tournament_overlaps_window,
)

PLAYER_A = {"id": 101, "first_name": "A", "last_name": "One", "full_name": "A One"}
PLAYER_B = {"id": 202, "first_name": "B", "last_name": "Two", "full_name": "B Two"}
TOURNAMENT = {
    "id": 500,
    "name": "Example Open",
    "surface": "Hard",
    "season": 2026,
    "start_date": "2026-08-10",
    "end_date": "2026-08-17",
}


def make_match(**overrides) -> dict:
    match = {
        "id": 9001,
        "tournament": TOURNAMENT,
        "season": 2026,
        "round": "Final",
        "player1": PLAYER_A,
        "player2": PLAYER_B,
        "winner": PLAYER_A,
        "set_scores": [
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 6, "player2_games": 3},
        ],
        "match_status": "finished",
        "is_live": False,
    }
    match.update(overrides)
    return match


def test_map_status_completed_variants():
    assert _map_status("finished") == "completed"
    assert _map_status("walkover") == "completed"
    assert _map_status("retired") == "completed"
    assert _map_status("defaulted") == "completed"


def test_map_status_live_and_scheduled():
    assert _map_status("in_progress") == "live"
    assert _map_status("scheduled") == "scheduled"
    assert _map_status("") == "scheduled"


def test_sets_won_from_set_scores():
    assert _sets_won(make_match()) == (2, 0)


def test_sets_won_none_when_no_sets_played():
    assert _sets_won(make_match(set_scores=[])) == (None, None)


def test_match_winner_id():
    assert _match_winner_id(make_match()) == "101"


def test_external_id_and_strip_tour_prefix():
    assert _external_id("atp", 142) == "atp:142"
    assert _strip_tour_prefix("atp:142") == "142"
    assert _strip_tour_prefix("142") == "142"  # already raw, no prefix to strip


def test_map_match_to_fixture_payload_completed():
    payload = _map_match_to_fixture_payload(make_match(), "atp")

    assert payload.external_id == "atp:9001"
    assert payload.league_external_id == "atp"
    assert payload.home_team_external_id == "atp:101"
    assert payload.away_team_external_id == "atp:202"
    assert payload.home_team_name == "A One"
    assert payload.away_team_name == "B Two"
    assert payload.season == "2026"
    assert payload.status == "completed"
    assert payload.home_score == 2  # sets won
    assert payload.away_score == 0
    assert payload.kickoff_utc.tzinfo is not None


def test_map_match_to_fixture_payload_scheduled_has_no_score():
    payload = _map_match_to_fixture_payload(
        make_match(match_status="scheduled", set_scores=[]), "atp"
    )
    assert payload.status == "scheduled"
    assert payload.home_score is None
    assert payload.away_score is None


def test_map_match_to_fixture_payload_falls_back_to_tournament_start_date():
    # No scheduled_at/start_time/date field present on the match itself (unconfirmed field —
    # see module docstring) — must fall back to the tournament's own start_date rather than
    # crash or fabricate a value.
    payload = _map_match_to_fixture_payload(make_match(), "wta")
    assert payload.kickoff_utc == datetime(2026, 8, 10, tzinfo=UTC)


def test_current_streak_all_wins():
    matches = [
        make_match(id=1, winner=PLAYER_A),
        make_match(id=2, winner=PLAYER_A),
    ]
    win_streak, losing_streak = _current_streak(matches, "101")
    assert win_streak == 2.0
    assert losing_streak == 0.0


def test_current_streak_breaks_on_first_loss_walking_backward():
    # Most-recent-first order: win, win, then a loss further back — streak stops at 2.
    matches = [
        make_match(id=1, winner=PLAYER_A),
        make_match(id=2, winner=PLAYER_A),
        make_match(id=3, winner=PLAYER_B),
    ]
    win_streak, losing_streak = _current_streak(matches, "101")
    assert win_streak == 2.0
    assert losing_streak == 0.0


def test_latest_rank_points_picks_most_recent_by_ranking_date():
    rankings = [
        {"ranking_date": "2026-07-01", "points": 1000},
        {"ranking_date": "2026-07-29", "points": 1200},
    ]
    assert _latest_rank_points(rankings) == 1200.0


def test_latest_rank_points_none_when_empty():
    assert _latest_rank_points([]) is None


def test_compute_team_stats_excludes_scheduled_and_computes_form():
    matches = [
        make_match(id=1, winner=PLAYER_A, tournament={**TOURNAMENT, "start_date": "2026-08-01"}),
        make_match(id=2, winner=PLAYER_B, tournament={**TOURNAMENT, "start_date": "2026-08-05"}),
        make_match(id=3, match_status="scheduled", set_scores=[]),
    ]
    rankings = [{"ranking_date": "2026-08-06", "points": 900}]

    stats = _compute_team_stats("101", matches, rankings, n_matches=10)

    assert stats.team_external_id == "101"
    assert stats.form_pts_5 == pytest.approx(0.5)  # 1 win, 1 loss among completed matches
    assert stats.rank_points == 900.0
    assert stats.days_since_last_match is not None
    # No goals/home-court concept for tennis — must stay None, never fabricated.
    assert stats.attack_str is None
    assert stats.defence_str is None
    assert stats.home_win_rate is None
    assert stats.away_win_rate is None
    assert stats.elo_rating is None


def test_compute_team_stats_no_completed_matches_returns_nones():
    stats = _compute_team_stats(
        "101", [make_match(match_status="scheduled", set_scores=[])], [], n_matches=10
    )
    assert stats.form_pts_5 is None
    assert stats.win_streak is None
    assert stats.losing_streak is None
    assert stats.days_since_last_match is None
    assert stats.rank_points is None


def test_tournament_overlaps_window():
    assert _tournament_overlaps_window(TOURNAMENT, date(2026, 8, 15), date(2026, 8, 20))
    assert not _tournament_overlaps_window(TOURNAMENT, date(2026, 9, 1), date(2026, 9, 5))


@pytest.mark.asyncio
async def test_fetch_all_pages_follows_cursor_pagination():
    pages = [
        {"data": [{"id": 1}], "meta": {"next_cursor": 100}},
        {"data": [{"id": 2}], "meta": {"next_cursor": None}},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, json=pages[call_count["n"]])
        call_count["n"] += 1
        return response

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        results = await _fetch_all_pages(client, "/matches", {"per_page": 100})

    assert [r["id"] for r in results] == [1, 2]
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_get_with_retry_retries_on_429_then_succeeds():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": [], "meta": {"next_cursor": None}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        response = await _get_with_retry(client, "/matches", {"per_page": 100})

    assert call_count["n"] == 2
    assert response.status_code == 200
