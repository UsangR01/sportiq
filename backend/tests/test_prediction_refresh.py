"""An upcoming fixture's prediction must not outlive the features it was built from.

REPORTED: every MLS card showed 1X above 90%, and had shown the same three days earlier.

MEASURED CAUSE. ingest_fixtures rewrites TeamFeatures on every run, so form, Elo, streaks and
rest days all move daily — but a prediction was regenerated ONLY when it did not exist or when
the model version changed. A fixture predicted the moment it was first ingested kept that
number until kickoff, however much better its inputs became.

    Philadelphia Union v Inter Miami
      served                                   away 0.04
      same model, that fixture's CURRENT vector away 0.30

Nothing was wrong with the model or the features. Only with when the prediction had been taken.

The original "only if no prediction exists" guard was correct when API-Football allowed 7,500
requests a day. At 75,000 the arithmetic changed: one H2H call per upcoming fixture per day is
roughly 150 for football, or 0.2% of the allowance.
"""

from datetime import UTC, datetime, timedelta

from app.workers.ingest_fixtures import PREDICTION_MAX_AGE_HOURS


def should_requeue(existing_version, created_at, active_version, now):
    """The rule as implemented in _ingest_fixtures_for_league's upcoming loop.

    Kept here as a readable mirror rather than reaching into the worker's loop, which needs a
    live adapter, a database and a Celery broker to reach the branch at all."""
    if existing_version is None:
        return True
    cutoff = now - timedelta(hours=PREDICTION_MAX_AGE_HOURS)
    superseded = active_version is not None and existing_version != active_version
    outdated = created_at is not None and created_at < cutoff
    return superseded or outdated


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
ACTIVE = "football_xgb_v20260813153115"


def test_a_prediction_older_than_its_features_is_regenerated():
    """THE REGRESSION. Three days old, current model version, and previously left alone."""
    assert should_requeue(ACTIVE, NOW - timedelta(days=3), ACTIVE, NOW)


def test_a_fresh_prediction_is_left_alone():
    """A re-run within the same day must queue nothing — that is what keeps the cost bounded."""
    assert not should_requeue(ACTIVE, NOW - timedelta(hours=2), ACTIVE, NOW)


def test_a_fixture_with_no_prediction_is_still_queued():
    assert should_requeue(None, None, ACTIVE, NOW)


def test_a_superseded_model_version_still_forces_a_refresh():
    """The existing trigger, unchanged: a promotion is exactly when every served number is out
    of date, and it must keep firing regardless of age."""
    assert should_requeue("football_xgb_v20260101000000", NOW - timedelta(minutes=5), ACTIVE, NOW)


def test_the_window_survives_a_daily_job_that_drifts():
    """20 hours, not 24. A daily ingest whose start time wanders by an hour would otherwise
    skip a day: at exactly 24h the previous prediction is not yet older than the cutoff."""
    assert PREDICTION_MAX_AGE_HOURS < 24
    drifted = NOW - timedelta(hours=23)
    assert should_requeue(ACTIVE, drifted, ACTIVE, NOW)


def test_an_unknown_active_version_does_not_force_a_refresh_on_its_own():
    """A registry lookup that returns nothing must not stampede every fixture — age still
    governs."""
    assert not should_requeue(ACTIVE, NOW - timedelta(hours=1), None, NOW)
    assert should_requeue(ACTIVE, NOW - timedelta(days=2), None, NOW)
