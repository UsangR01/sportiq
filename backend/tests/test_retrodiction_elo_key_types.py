"""Elo lookups in retrodiction must survive the parquet/DB id-type split.

The retrodiction game log is a concat of two sources that disagree on type:
ml/data/football_game_log_*.parquet stores FIXTURE_ID as int64, while _build_game_log_df
stores Fixture.external_id, a str. compute_elo_history keys its output on whatever each row
carries, and _retrodict_league looks it up with the str external_id -- so every fixture that
came from the parquet returned elo_diff=None even though its Elo had been computed correctly.

Nothing raises. A missing elo_diff silently drops the strongest team-strength feature and
leaves a confident-looking prediction in its place. It surfaced only when nine leagues had
their 2026 history collected: those fixtures moved from DB-sourced to parquet-sourced, lost
Elo, and their predicted away-win probability collapsed to 5.7% against an actual 36.6%.
"""

import pandas as pd

from app.models_ml.elo import compute_elo_history


def _game_log(fixture_id):
    """One completed match, in collect_football_data.py's shape."""
    return pd.DataFrame(
        [
            {
                "FIXTURE_ID": fixture_id,
                "SEASON": 2026,
                "GAME_DATE": pd.Timestamp("2026-08-01").date(),
                "TEAM_ID": "100",
                "OPPONENT_ID": "200",
                "HOME_AWAY": "home",
                "GF": 2,
                "GA": 1,
                "WDL": "W",
            },
            {
                "FIXTURE_ID": fixture_id,
                "SEASON": 2026,
                "GAME_DATE": pd.Timestamp("2026-08-01").date(),
                "TEAM_ID": "200",
                "OPPONENT_ID": "100",
                "HOME_AWAY": "away",
                "GF": 1,
                "GA": 2,
                "WDL": "L",
            },
        ]
    )


def test_elo_keys_follow_the_fixture_id_type_they_were_built_from():
    """Characterises the trap rather than asserting it is acceptable: an int-keyed history is
    genuinely unreachable with the str external_id every caller has."""
    int_keyed = compute_elo_history(_game_log(1494234))
    assert int_keyed.get((1494234, "100")) is not None
    assert int_keyed.get(("1494234", "100")) is None  # <- the silent miss


def test_normalising_fixture_id_to_str_makes_the_lookup_resolve():
    """The fix _retrodict_league applies to the concatenated frame before computing Elo."""
    log = _game_log(1494234)
    log["FIXTURE_ID"] = log["FIXTURE_ID"].astype(str)
    assert compute_elo_history(log).get(("1494234", "100")) is not None


def test_a_mixed_type_frame_resolves_for_both_sources_after_normalising():
    """The real shape: parquet rows (int) concatenated with DB rows (str). Before normalising,
    only the DB-sourced fixture resolves -- which is exactly why the bug looked league-specific
    instead of universal. Whether a league was affected depended purely on whether its recent
    fixtures had been collected into the parquet yet."""
    mixed = pd.concat([_game_log(1494234), _game_log("1494235")], ignore_index=True)
    before = compute_elo_history(mixed)
    assert before.get(("1494234", "100")) is None  # parquet-sourced: missed
    assert before.get(("1494235", "100")) is not None  # DB-sourced: found

    mixed["FIXTURE_ID"] = mixed["FIXTURE_ID"].astype(str)
    after = compute_elo_history(mixed)
    assert after.get(("1494234", "100")) is not None
    assert after.get(("1494235", "100")) is not None
