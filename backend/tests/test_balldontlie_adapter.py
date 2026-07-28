"""Unit tests for the BallDontLie adapter's pure mapping/aggregation logic.

Sample game dicts below are trimmed from real /nba/v1/games responses captured during live
research against the actual API (see CLAUDE.md) — not fabricated shapes. No network, no DB.
"""

from datetime import UTC, datetime

import httpx
import pytest

from app.adapters.balldontlie import (
    _compute_team_stats,
    _current_nba_season,
    _fetch_all_games,
    _get_with_retry,
    _map_game_to_fixture_payload,
    _map_status,
)

MAGIC = {
    "id": 22,
    "city": "Orlando",
    "name": "Magic",
    "full_name": "Orlando Magic",
    "abbreviation": "ORL",
}
GRIZZLIES = {
    "id": 15,
    "city": "Memphis",
    "name": "Grizzlies",
    "full_name": "Memphis Grizzlies",
    "abbreviation": "MEM",
}


def make_game(**overrides):
    game = {
        "id": 18447392,
        "date": "2026-01-15",
        "season": 2025,
        "status": "Final",
        "period": 4,
        "postseason": False,
        "home_team_score": 118,
        "visitor_team_score": 111,
        "datetime": "2026-01-15T19:00:00.000Z",
        "home_team": MAGIC,
        "visitor_team": GRIZZLIES,
    }
    game.update(overrides)
    return game


def test_map_status_completed():
    assert _map_status("Final", 4) == "completed"


def test_map_status_scheduled_before_tipoff():
    assert _map_status("7:00 pm ET", None) == "scheduled"
    assert _map_status("7:00 pm ET", 0) == "scheduled"


def test_map_status_live_in_progress():
    assert _map_status("Halftime", 2) == "live"


def test_map_game_to_fixture_payload():
    payload = _map_game_to_fixture_payload(make_game())

    assert payload.external_id == "18447392"
    assert payload.league_external_id == "nba"
    assert payload.home_team_external_id == "22"
    assert payload.away_team_external_id == "15"
    assert payload.home_team_name == "Orlando Magic"
    assert payload.away_team_name == "Memphis Grizzlies"
    assert payload.home_team_short_name == "ORL"
    assert payload.away_team_short_name == "MEM"
    assert payload.season == "2025"
    assert payload.status == "completed"
    assert payload.kickoff_utc == datetime(2026, 1, 15, 19, 0, tzinfo=UTC)


def test_map_game_to_fixture_payload_scheduled():
    game = make_game(
        status="7:00 pm ET", period=None, home_team_score=None, visitor_team_score=None
    )
    payload = _map_game_to_fixture_payload(game)
    assert payload.status == "scheduled"


def test_compute_team_stats_home_and_away_split():
    # Magic (id 22): win at home 118-111, loss away 100-110, win away 105-95.
    games = [
        make_game(
            id=1, home_team_score=118, visitor_team_score=111, datetime="2026-01-15T19:00:00.000Z"
        ),
        make_game(
            id=2,
            home_team=GRIZZLIES,
            visitor_team=MAGIC,
            home_team_score=110,
            visitor_team_score=100,
            datetime="2026-01-10T19:00:00.000Z",
        ),
        make_game(
            id=3,
            home_team=GRIZZLIES,
            visitor_team=MAGIC,
            home_team_score=95,
            visitor_team_score=105,
            datetime="2026-01-05T19:00:00.000Z",
        ),
    ]

    stats = _compute_team_stats("22", games, n_matches=5)

    assert stats.team_external_id == "22"
    assert stats.elo_rating is None
    assert stats.xg_for_5 is None
    assert stats.xg_against_5 is None
    assert stats.home_win_rate == 1.0  # 1/1 home games won
    assert stats.away_win_rate == 0.5  # 1/2 away games won
    assert stats.form_pts_5 == pytest.approx(2 / 3)  # 2 wins out of 3 total
    assert stats.days_since_last_match is not None


def test_compute_team_stats_no_completed_games_returns_nones():
    stats = _compute_team_stats("22", [make_game(status="7:00 pm ET", period=None)], n_matches=5)
    assert stats.form_pts_5 is None
    assert stats.home_win_rate is None
    assert stats.away_win_rate is None
    assert stats.days_since_last_match is None


def test_current_nba_season_after_october_uses_current_year():
    assert _current_nba_season(datetime(2026, 10, 15, tzinfo=UTC)) == 2026


def test_current_nba_season_before_october_uses_previous_year():
    assert _current_nba_season(datetime(2026, 7, 28, tzinfo=UTC)) == 2025


@pytest.mark.asyncio
async def test_fetch_all_games_follows_cursor_pagination():
    pages = [
        {"data": [make_game(id=1)], "meta": {"next_cursor": 100}},
        {"data": [make_game(id=2)], "meta": {"next_cursor": None}},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, json=pages[call_count["n"]])
        call_count["n"] += 1
        return response

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        games = await _fetch_all_games(client, {"per_page": 100})

    assert [g["id"] for g in games] == [1, 2]
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_get_with_retry_retries_on_429_then_succeeds():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": [make_game(id=1)], "meta": {"next_cursor": None}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        response = await _get_with_retry(client, "/games", {"per_page": 100})

    assert call_count["n"] == 2
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_with_retry_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _get_with_retry(client, "/games", {"per_page": 100})
