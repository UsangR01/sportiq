"""predictions.kind — the provenance flag GET /history depends on.

Both prediction paths write to the same table, and nothing distinguished them. A performance
measurement built over `predictions` would therefore have mixed genuine forecasts with
retrodictions produced after the result was known, and reported the result as a track record.

Timestamps cannot substitute for the column, which is the reason it exists. `created_at <
kickoff_utc` looks like a safe proxy and breaks on REGENERATION: 91 football predictions were
regenerated on 2026-08-10 after a model change, resetting created_at to well after those
fixtures had kicked off. Those rows are indistinguishable from real retrodictions, which is why
UNKNOWN exists and why the historical backfill is deliberately one-directional.
"""

import pytest

from app.predictions.models import PredictionKind


def test_the_three_kinds_are_distinct_and_named_for_what_they_mean():
    assert {k.value for k in PredictionKind} == {"pre_match", "retrodiction", "unknown"}


def test_unknown_is_the_column_default_so_a_new_path_fails_safe():
    """A future write path that forgets to set kind must not silently claim to be a forecast.
    Defaulting to UNKNOWN means it is excluded from skill measurement until someone looks;
    defaulting to PRE_MATCH would quietly inflate the track record instead."""
    from app.predictions.models import Prediction

    default = Prediction.__table__.c.kind.server_default
    assert default is not None
    assert "UNKNOWN" in str(default.arg)


def test_the_live_path_marks_its_rows_pre_match():
    """run_predictions is the only path that runs before kickoff, so it is the only source of
    rows that evidence forecasting skill."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "workers"
        / "run_predictions.py"
    ).read_text(encoding="utf-8")
    assert "kind=PredictionKind.PRE_MATCH" in source
    assert "kind=PredictionKind.RETRODICTION" not in source


def test_the_retrodiction_path_marks_its_rows_retrodiction():
    """backfill_predictions may legitimately read real lineups and completed-match history, so
    its output is a display feature and never evidence of skill."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "workers"
        / "backfill_predictions.py"
    ).read_text(encoding="utf-8")
    assert "kind=PredictionKind.RETRODICTION" in source
    assert "kind=PredictionKind.PRE_MATCH" not in source


@pytest.mark.parametrize(
    "created_before_kickoff, expected",
    [(True, PredictionKind.PRE_MATCH), (False, PredictionKind.UNKNOWN)],
)
def test_the_historical_backfill_is_one_directional(created_before_kickoff, expected):
    """Pins the rule the migration applies, and the asymmetry that makes it honest.

    created_at < kickoff PROVES a forecast: nothing regenerates a prediction backwards in time.
    The reverse proves nothing, because regeneration moves created_at forward — so those rows
    are left UNKNOWN rather than assumed to be retrodictions. Guessing would move rows into
    whichever bucket the reader trusts, and an inflated track record costs far more than a
    smaller honest one.
    """
    classified = PredictionKind.PRE_MATCH if created_before_kickoff else PredictionKind.UNKNOWN
    assert classified is expected
