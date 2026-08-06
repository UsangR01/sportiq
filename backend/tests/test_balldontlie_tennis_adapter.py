"""Unit tests for the tennis (ATP/WTA) adapter's pure mapping/aggregation logic.

Sample match/tournament dicts below are shaped per REAL, live-confirmed /atp/v1 responses
(fetched against the actual API once the user's BallDontLie plan was confirmed ALL-STAR for
ATP — see module docstring in app/adapters/balldontlie_tennis.py for what was verified live).
No network, no DB.
"""

from datetime import UTC, date, datetime

import httpx
import pytest

from app.adapters.balldontlie_tennis import (
    _compute_team_stats,
    _current_streak,
    _external_id,
    _fetch_all_pages,
    _filter_by_surface,
    _filter_meetings_vs_opponent,
    _get_with_retry,
    _home_away_players,
    _is_completed_set,
    _is_same_edition,
    _latest_rank_points,
    _map_match_to_fixture_payload,
    _map_odds_row_to_payload,
    _map_status,
    _match_kickoff_is_estimated,
    _match_result_type,
    _match_winner_id,
    _sets_won,
    _strip_tour_prefix,
    _tournament_overlaps_window,
    _upcoming_match_ids,
    _win_rate,
)

PLAYER_A = {"id": 101, "first_name": "A", "last_name": "One", "full_name": "A One"}
PLAYER_B = {"id": 202, "first_name": "B", "last_name": "Two", "full_name": "B Two"}
PLAYER_C = {"id": 303, "first_name": "C", "last_name": "Three", "full_name": "C Three"}
TOURNAMENT = {
    "id": 500,
    "name": "Example Open",
    "surface": "Hard",
    "season": 2026,
    "start_date": "2026-08-10",
    "end_date": "2026-08-17",
}
CLAY_TOURNAMENT = {**TOURNAMENT, "id": 501, "surface": "Clay"}


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
        # Real, live-confirmed field name (see module docstring) — not scheduled_at/start_time.
        "scheduled_time": "2026-08-15T14:30:00.000Z",
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
    assert payload.kickoff_utc == datetime(2026, 8, 15, 14, 30, tzinfo=UTC)


def test_map_match_to_fixture_payload_scheduled_has_no_score():
    payload = _map_match_to_fixture_payload(
        make_match(match_status="scheduled", set_scores=[]), "atp"
    )
    assert payload.status == "scheduled"
    assert payload.home_score is None
    assert payload.away_score is None


def test_map_match_to_fixture_payload_falls_back_to_tournament_start_date():
    # The rare case scheduled_time is genuinely absent — must fall back to the tournament's
    # own start_date rather than crash or fabricate a value.
    payload = _map_match_to_fixture_payload(make_match(scheduled_time=None), "wta")
    assert payload.kickoff_utc == datetime(2026, 8, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("p1", "p2", "expected"),
    [
        (6, 4, True),  # standard
        (6, 0, True),
        (7, 5, True),  # went to 7-5
        (7, 6, True),  # tiebreak
        (70, 68, True),  # long deciding set (real, e.g. Isner-Mahut)
        (6, 5, False),  # NOT over yet — must reach 7-5 or 7-6
        (2, 3, False),  # abandoned mid-set (the real retirement case)
        (0, 0, False),
        (5, 4, False),
    ],
)
def test_is_completed_set(p1, p2, expected):
    assert _is_completed_set(p1, p2) is expected


def test_sets_won_ignores_an_abandoned_set__real_retirement_regression():
    """The real, user-reported bug: Popyrin beat Kokkinakis 6-4, then Kokkinakis retired at
    2-3 in set two. Counting "whoever is ahead" scored this 1-1 — an impossible tennis
    scoreline (there are no draws), which ALSO inverted the verdict: Popyrin genuinely won,
    but a stored 1-1 made the feed mark a CORRECT prediction as failed."""
    match = make_match(
        winner=PLAYER_A,
        set_scores=[
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 2, "player2_games": 3},
        ],
    )
    assert _sets_won(match) == (1, 0)


def test_match_result_type_detects_retirement_despite_status_finished():
    """Live-confirmed: BallDontLie reports a genuine mid-match retirement as plain
    match_status="finished" with no retirement marker at all, so this MUST be inferred from
    the score structurally rather than read off match_status."""
    match = make_match(
        match_status="finished",
        winner=PLAYER_A,
        set_scores=[
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 2, "player2_games": 3},
        ],
    )
    assert _match_result_type(match) == "retired"


def test_match_result_type_none_for_a_normally_completed_match():
    match = make_match(
        match_status="finished",
        winner=PLAYER_A,
        set_scores=[
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 6, "player2_games": 3},
        ],
    )
    assert _match_result_type(match) is None


def test_match_result_type_walkover_when_no_sets_were_played():
    match = make_match(match_status="finished", winner=PLAYER_A, set_scores=[])
    assert _match_result_type(match) == "walkover"


def test_match_result_type_detects_retirement_after_a_single_completed_set():
    """A winner who only ever took one set can't have won any real tennis format outright,
    even when every listed set is itself complete."""
    match = make_match(
        match_status="finished",
        winner=PLAYER_A,
        set_scores=[{"set_number": 1, "player1_games": 6, "player2_games": 4}],
    )
    assert _match_result_type(match) == "retired"


def test_map_match_to_fixture_payload_carries_result_type_and_tournament():
    match = make_match(
        winner=PLAYER_A,
        set_scores=[
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 2, "player2_games": 3},
        ],
    )
    payload = _map_match_to_fixture_payload(match, "atp")
    assert payload.home_score == 1 and payload.away_score == 0
    assert payload.result_type == "retired"
    assert payload.tournament_name == "Example Open"
    assert payload.tournament_surface == "Hard"


def test_home_away_players_is_id_based_not_player1_player2_position():
    """The real, live-confirmed bug this guards against: BallDontLie always lists the eventual
    WINNER as player1 for a completed match (20/20 in a sampled batch) — naively trusting
    player1="home" bakes 100% target leakage into every completed match's label. "home" must
    instead be a stable, outcome-independent tiebreak (lower external player id), regardless of
    which slot the provider happens to put the winner in."""
    # PLAYER_B (id 202) wins but is listed as player2 — home must still resolve to PLAYER_A
    # (id 101, the lower id), not "whoever the provider calls player1".
    match = make_match(player1=PLAYER_A, player2=PLAYER_B, winner=PLAYER_B)
    home, away = _home_away_players(match)
    assert home["id"] == 101
    assert away["id"] == 202

    # Now swap which slot each player occupies — home/away must resolve identically regardless.
    swapped = make_match(player1=PLAYER_B, player2=PLAYER_A, winner=PLAYER_B)
    home2, away2 = _home_away_players(swapped)
    assert home2["id"] == 101
    assert away2["id"] == 202


def test_map_match_to_fixture_payload_home_away_survives_player1_player2_swap():
    """End-to-end version of the above through the real payload mapper: home/away and the
    sets-won scoreline attached to each must stay correctly paired to the actual player,
    regardless of which raw slot (player1/player2) the provider put them in."""
    # PLAYER_B (id 202) is player1 here and wins 2 sets to 0 — but home is still PLAYER_A
    # (id 101), so home_score must reflect PLAYER_A's (0) sets, not player1's.
    match = make_match(
        player1=PLAYER_B,
        player2=PLAYER_A,
        winner=PLAYER_B,
        set_scores=[
            {"set_number": 1, "player1_games": 6, "player2_games": 4},
            {"set_number": 2, "player1_games": 6, "player2_games": 3},
        ],
    )
    payload = _map_match_to_fixture_payload(match, "atp")
    assert payload.home_team_external_id == "atp:101"  # PLAYER_A
    assert payload.away_team_external_id == "atp:202"  # PLAYER_B
    assert payload.home_score == 0  # PLAYER_A (away/player2 in the raw data) lost
    assert payload.away_score == 2  # PLAYER_B (home/player1 in the raw data) won


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


def test_win_rate_basic():
    matches = [make_match(id=1, winner=PLAYER_A), make_match(id=2, winner=PLAYER_B)]
    assert _win_rate(matches, "101") == pytest.approx(0.5)


def test_win_rate_none_when_empty():
    assert _win_rate([], "101") is None


def test_filter_by_surface():
    hard = make_match(id=1, tournament=TOURNAMENT)
    clay = make_match(id=2, tournament=CLAY_TOURNAMENT)
    assert _filter_by_surface([hard, clay], "Hard") == [hard]
    assert _filter_by_surface([hard, clay], "Clay") == [clay]


def test_filter_by_surface_empty_when_no_surface():
    hard = make_match(id=1, tournament=TOURNAMENT)
    assert _filter_by_surface([hard], None) == []


def test_filter_meetings_vs_opponent():
    # Player A's own history: one match vs B, one vs C — filtering for B only keeps the first.
    vs_b = make_match(id=1, player1=PLAYER_A, player2=PLAYER_B, winner=PLAYER_A)
    vs_c = make_match(id=2, player1=PLAYER_A, player2=PLAYER_C, winner=PLAYER_C)
    assert _filter_meetings_vs_opponent([vs_b, vs_c], "101", "202") == [vs_b]


def test_filter_meetings_vs_opponent_handles_either_slot():
    # Player A may appear as player1 or player2 depending on the match — must match either.
    as_player1 = make_match(id=1, player1=PLAYER_A, player2=PLAYER_B, winner=PLAYER_A)
    as_player2 = make_match(id=2, player1=PLAYER_B, player2=PLAYER_A, winner=PLAYER_B)
    assert _filter_meetings_vs_opponent([as_player1, as_player2], "101", "202") == [
        as_player1,
        as_player2,
    ]


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


@pytest.mark.parametrize(
    ("scheduled_time", "expected_estimated"),
    [
        ("2026-08-15T14:30:00.000Z", False),  # a genuine kickoff time
        ("2026-08-15T00:00:00.000Z", True),  # midnight = a DATE, not a time
        (None, True),  # absent entirely -> falls back to tournament start
    ],
)
def test_match_kickoff_is_estimated(scheduled_time, expected_estimated):
    """Real tennis matches are never scheduled for 00:00 UTC, so a midnight timestamp encodes
    a date with no time of day and must be treated exactly like a missing one.

    This matters because the fallback assigns the TOURNAMENT'S START DATE to every untimed
    match. Measured on a real ATP tournament, 570 of 600 matches had no scheduled_time and 17
    more were midnight — so all of them collapsed onto one timestamp. That showed every match
    at the same wrong time AND made later-round matches appear on today's schedule, where users
    could not find them on any real platform."""
    match = make_match(scheduled_time=scheduled_time)
    assert _match_kickoff_is_estimated(match) is expected_estimated
    assert _map_match_to_fixture_payload(match, "atp").kickoff_is_estimated is expected_estimated


def test_is_same_edition_rejects_a_past_years_running_of_the_same_tournament():
    """A tournament id identifies the EVENT, not one year's running of it.

    /matches?tournament_ids[]=X returns every match ever played under that id. Measured live,
    the National Bank Open id returned 1,452 matches across 19 editions back to 2007 for a
    routine +/-2 day poll, of which 70 were current — so ingestion wrote thousands of fixtures
    dating to 2007 and the feed showed decade-old matches among today's. Each match carries its
    own edition's start_date on its embedded tournament object, which is the discriminator.
    """
    current = {"id": 264, "name": "National Bank Open", "start_date": "2026-08-02"}
    this_year = make_match()
    this_year["tournament"] = dict(current)
    last_year = make_match()
    last_year["tournament"] = {**current, "start_date": "2025-07-27"}

    assert _is_same_edition(this_year, current) is True
    assert _is_same_edition(last_year, current) is False


def test_is_same_edition_keeps_a_match_it_cannot_judge():
    """With no start_date on either side there is no basis to reject. Over-ingesting one match
    is recoverable; silently dropping a real upcoming fixture is not, so the ambiguous case
    keeps the match rather than guessing."""
    assert _is_same_edition(make_match(), {"id": 264}) is True
    match_without_dates = make_match()
    match_without_dates["tournament"] = {"id": 264}
    assert _is_same_edition(match_without_dates, {"id": 264, "start_date": "2026-08-02"}) is True


def test_surface_filter_ignores_whitespace_and_case():
    """The provider returns both "Grass" and "Grass     " as real values — 190 of 2,295 grass
    matches carry the padded form, measured across 17,273 completed ATP matches.

    An exact comparison splits one surface into two, so a player's grass record quietly omits
    those matches. The features still populate; they just draw on a smaller sample than they
    claim to, which is the kind of fault that never surfaces as an error."""
    matches = [
        {"tournament": {"surface": "Grass"}},
        {"tournament": {"surface": "Grass     "}},
        {"tournament": {"surface": "grass"}},
        {"tournament": {"surface": "Clay"}},
        {"tournament": {"surface": None}},
    ]

    assert len(_filter_by_surface(matches, "Grass")) == 3
    assert len(_filter_by_surface(matches, "  grass ")) == 3
    assert len(_filter_by_surface(matches, "Clay")) == 1
    assert _filter_by_surface(matches, None) == []


# --- /odds mapping (GOAT tier) -------------------------------------------------------------
# Shaped per a real DraftKings row confirmed live once the GOAT plan was activated:
# {"id":..., "match_id":4593192, "vendor":"draftkings", "player1":{...}, "player2":{...},
#  "player1_odds":-158, "player2_odds":124, "updated_at":"2026-08-04T23:05:36.102Z"}


def make_odds_row(*, p1_id: int, p2_id: int, p1_odds, p2_odds, vendor="draftkings") -> dict:
    return {
        "id": 269355464,
        "match_id": 4593192,
        "vendor": vendor,
        "player1": {"id": p1_id, "full_name": "Player One"},
        "player2": {"id": p2_id, "full_name": "Player Two"},
        "player1_odds": p1_odds,
        "player2_odds": p2_odds,
        "updated_at": "2026-08-04T23:05:36.102Z",
    }


def test_odds_are_oriented_by_player_id_not_by_player1_player2_position():
    """The single most important property of this mapping.

    home is the LOWER player id (_home_away_players), and the provider orders its own
    player1/player2 slots independently — for completed matches it demonstrably puts the
    WINNER in player1. Reading player1_odds as "home odds" positionally would show users an
    inverted price: a 1.17 favourite quoted at 8.00.

    Both rows below describe the SAME market with the SAME prices; only the slot order
    differs. The mapped payload must be identical.
    """
    # id 10 is home (lower). Its price is -400 -> heavy favourite -> 1.25 decimal.
    favourite_in_slot_1 = make_odds_row(p1_id=10, p2_id=99, p1_odds=-400, p2_odds=300)
    favourite_in_slot_2 = make_odds_row(p1_id=99, p2_id=10, p1_odds=300, p2_odds=-400)

    a = _map_odds_row_to_payload(favourite_in_slot_1, "atp")
    b = _map_odds_row_to_payload(favourite_in_slot_2, "atp")

    assert a.home_odds == b.home_odds == 1.25
    assert a.away_odds == b.away_odds == 4.0
    # The favourite must be home in both, never flipped by slot order.
    assert a.home_odds < a.away_odds


def test_odds_payload_uses_the_tour_prefixed_fixture_id_for_a_direct_join():
    """match_id is BallDontLie's own, and BallDontLie is also the fixtures provider — so this
    must carry OUR tour-prefixed form to match Fixture.external_id directly in
    ingest_odds._resolve_fixture. An unprefixed id would silently miss every fixture and fall
    through to the fuzzy name join this feed exists to avoid."""
    payload = _map_odds_row_to_payload(
        make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=124), "atp"
    )
    assert payload.fixture_external_id == "atp:4593192"
    assert (
        _map_odds_row_to_payload(
            make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=124), "wta"
        ).fixture_external_id
        == "wta:4593192"
    )


def test_odds_are_converted_from_american_to_decimal():
    payload = _map_odds_row_to_payload(
        make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=124), "atp"
    )
    assert payload.home_odds == 1.6329  # -158 -> 1 + 100/158
    assert payload.away_odds == 2.24  # +124 -> 1 + 124/100
    assert payload.market == "h2h"
    assert payload.draw_odds is None  # tennis has no draw
    assert payload.bookmaker == "draftkings"


def test_odds_row_missing_a_price_is_dropped_not_half_populated():
    """A row with only one side priced is not a real market. Emitting it would let a
    None-valued side reach the EV/pick math as if it were a real absence of odds."""
    assert (
        _map_odds_row_to_payload(
            make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=None), "atp"
        )
        is None
    )
    assert (
        _map_odds_row_to_payload(make_odds_row(p1_id=10, p2_id=99, p1_odds=0, p2_odds=124), "atp")
        is None
    )
    incomplete = make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=124)
    incomplete["match_id"] = None
    assert _map_odds_row_to_payload(incomplete, "atp") is None


def test_odds_updated_at_falls_back_rather_than_failing_the_row():
    """A real price with an unreadable timestamp is still a real price; this column only drives
    staleness display."""
    row = make_odds_row(p1_id=10, p2_id=99, p1_odds=-158, p2_odds=124)
    row["updated_at"] = "not-a-timestamp"
    payload = _map_odds_row_to_payload(row, "atp")
    assert payload is not None and payload.updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_upcoming_match_ids_excludes_finished_and_live_matches():
    """The guard against ingesting in-play prices as if they were pre-match markets.

    Measured on a real /odds?season=2026 response: 70 of 94 rows were for `finished` matches,
    2 `in_progress`, 22 `scheduled` — and all 24 implausible quotes (1.001/74.0, 1.005/51.0)
    sat on matches already decided. A finished match's last traded price effectively announces
    the winner, so admitting it would both display nonsense and hand moneyline_implied_prob a
    feature that has seen the outcome.
    """
    tournament = {"id": 900, "start_date": "2026-08-01", "end_date": "2026-08-20"}

    def match(mid, status):
        return {
            "id": mid,
            "match_status": status,
            "tournament": tournament,
            "scheduled_time": "2026-08-10T12:00:00Z",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tournaments"):
            return httpx.Response(200, json={"data": [tournament], "meta": {}})
        return httpx.Response(
            200,
            json={
                "data": [
                    match(1, "scheduled"),
                    match(2, "finished"),
                    match(3, "in_progress"),
                    match(4, "retired"),
                    match(5, "scheduled"),
                ],
                "meta": {},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://x/atp/v1"
    ) as client:
        ids = await _upcoming_match_ids(client, date(2026, 8, 5), date(2026, 8, 12))

    assert ids == {"1", "5"}
