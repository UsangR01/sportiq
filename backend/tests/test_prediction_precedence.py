"""A retrodiction must never overwrite a real pre-kickoff forecast on the card.

REPORTED 2026-08-16: "On friday - when we added the wnba - 3 predictions were made. All were
wins, still visible in local. But now, in prod just 2 predictions are there - one win one loss."

Measured cause: the feed picked a fixture's prediction purely by created_at, so running the
retrodiction backfill rewrote the pick on every past card that already had a genuine forecast.
Las Vegas Aces v Washington Mystics 83-76 had been HOME 0.64 (a win) and became AWAY 0.85 (a
loss) -- same fixture, same score, a pick nobody had ever been shown. That league held 16
PRE_MATCH predictions against 22 RETRODICTION ones, and the newer rows won every time.

Retrodiction exists to fill fixtures that never had a forecast. It is not a restatement of one.
"""

from datetime import UTC, datetime, timedelta

from app.fixtures.router import _prediction_precedence
from app.predictions.models import PredictionKind

KICKOFF = datetime(2026, 8, 14, 2, 0, tzinfo=UTC)


class FakePrediction:
    def __init__(self, kind, created_at):
        self.kind = kind
        self.created_at = created_at


def best(*predictions):
    return max(predictions, key=_prediction_precedence)


def test_a_later_retrodiction_never_displaces_a_real_forecast():
    """THE REGRESSION. The retrodiction is two days newer and must still lose."""
    forecast = FakePrediction(PredictionKind.PRE_MATCH, KICKOFF - timedelta(hours=3))
    retrodiction = FakePrediction(PredictionKind.RETRODICTION, KICKOFF + timedelta(days=2))

    assert best(forecast, retrodiction) is forecast
    assert best(retrodiction, forecast) is forecast


def test_a_retrodiction_is_used_when_there_was_no_forecast():
    """The case retrodiction exists for: a fixture first seen already finished."""
    older = FakePrediction(PredictionKind.RETRODICTION, KICKOFF + timedelta(days=1))
    newer = FakePrediction(PredictionKind.RETRODICTION, KICKOFF + timedelta(days=2))

    assert best(older, newer) is newer


def test_the_newest_forecast_wins_among_forecasts():
    """A forecast revised as injuries and odds landed keeps its FINAL pre-kickoff value."""
    early = FakePrediction(PredictionKind.PRE_MATCH, KICKOFF - timedelta(days=2))
    late = FakePrediction(PredictionKind.PRE_MATCH, KICKOFF - timedelta(minutes=30))

    assert best(early, late) is late


def test_an_unknown_kind_does_not_outrank_a_forecast():
    """UNKNOWN is the column default, so a write path that forgets to set kind must not be
    able to take over a card from a genuine forecast."""
    forecast = FakePrediction(PredictionKind.PRE_MATCH, KICKOFF - timedelta(hours=3))
    unknown = FakePrediction(PredictionKind.UNKNOWN, KICKOFF + timedelta(days=1))

    assert best(forecast, unknown) is forecast
