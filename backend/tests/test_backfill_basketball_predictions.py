"""Basketball retrodiction — the path that did not exist until 2026-08-15.

A fixture first seen already-finished never gets a pre-match prediction, and football and tennis
have had a retrodiction path for months. Basketball had none, so 23 completed NBA/WNBA fixtures
in production showed a blank card with no mechanism that could ever fill it.

The subtle part is not the model call, it is the game log: nba_features reads specific column
names and a specific MATCHUP string format, and getting either wrong fails SILENTLY -- the
feature comes back None and the prediction looks fine.
"""

from datetime import UTC, date, datetime

import pandas as pd

from app.models_ml.nba_features import _h2h_win_rate, _last10_stats
from app.workers.backfill_basketball_predictions import _build_game_log_df


class FakeFixture:
    def __init__(self, kickoff, season="2025"):
        self.kickoff_utc = kickoff
        self.season = season


class FakeLiveState:
    def __init__(self, home_score, away_score):
        self.home_score = home_score
        self.away_score = away_score


def rows(*specs):
    return [
        (FakeFixture(datetime(2026, 8, day, tzinfo=UTC)), FakeLiveState(hs, a_s), home, away)
        for day, hs, a_s, home, away in specs
    ]


def test_one_row_per_team_with_the_margin_from_each_side():
    df = _build_game_log_df(rows((1, 110, 100, "DET", "PHX")))

    assert len(df) == 2
    home = df[df["TEAM_ABBREVIATION"] == "DET"].iloc[0]
    away = df[df["TEAM_ABBREVIATION"] == "PHX"].iloc[0]
    assert home["WL"] == "W" and home["PLUS_MINUS"] == 10.0
    assert away["WL"] == "L" and away["PLUS_MINUS"] == -10.0
    assert home["GAME_DATE"] == date(2026, 8, 1)


def test_the_matchup_string_is_what_the_h2h_lookup_actually_matches_on():
    """THE SILENT ONE. nba_features._h2h_win_rate selects meetings with
    MATCHUP.str.endswith(opponent_abbr), so both sides' rows must END with the opponent's
    abbreviation. Get the format wrong and h2h_win_rate is None for every fixture -- no error,
    just a feature quietly missing from every prediction."""
    df = _build_game_log_df(
        rows(
            (1, 110, 100, "DET", "PHX"),
            (5, 95, 105, "PHX", "DET"),
        )
    )

    det = df[df["TEAM_ABBREVIATION"] == "DET"]
    phx = df[df["TEAM_ABBREVIATION"] == "PHX"]

    # DET beat PHX at home, then won again away: 2 meetings, both won.
    assert _h2h_win_rate(det, date(2026, 8, 20), "PHX") == 1.0
    assert _h2h_win_rate(phx, date(2026, 8, 20), "DET") == 0.0


def test_the_leakage_guard_still_bites_on_this_log():
    """assemble_from_game_log filters GAME_DATE < as_of_date. A game must never see itself."""
    df = _build_game_log_df(rows((1, 110, 100, "DET", "PHX")))
    det = df[df["TEAM_ABBREVIATION"] == "DET"]

    assert _h2h_win_rate(det, date(2026, 8, 1), "PHX") is None
    assert _last10_stats(det, date(2026, 8, 1)) == (None, None)


def test_a_tied_score_is_dropped_rather_than_recorded_as_a_loss():
    """Basketball goes to overtime, so a tie means the winner is NOT derivable -- the same
    reason "nba" is in ingest_fixtures.SPORTS_WITHOUT_DRAWS. WL has no honest value here, and
    guessing one would feed a fabricated result into every rolling stat."""
    df = _build_game_log_df(rows((1, 100, 100, "DET", "PHX")))

    assert df.empty


def test_a_fixture_with_no_score_is_dropped():
    """Real case: nine NBA fixtures in the dev database are COMPLETED with no FixtureLiveState
    at all, left by early exploration. They have no result, so they cannot contribute one."""
    df = _build_game_log_df(rows((1, None, None, "DET", "PHX")))

    assert df.empty


def test_the_frame_has_the_columns_nba_features_reads_even_when_empty():
    """An empty log must still be indexable -- assemble_from_game_log filters on these columns
    before it knows whether any rows exist."""
    df = _build_game_log_df([])

    for column in ("TEAM_ABBREVIATION", "GAME_DATE", "SEASON", "MATCHUP", "WL", "PLUS_MINUS"):
        assert column in df.columns
    assert isinstance(df, pd.DataFrame)
