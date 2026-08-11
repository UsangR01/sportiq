"""Error reporting must cover the processes where the failures actually happen.

Sentry was initialised in create_app() and nowhere else, so the API was covered and the Celery
worker and beat were not — exactly backwards for this project. Every silent failure here has
been a worker or scheduler one: a beat that never dispatched snapshot_picks, an ingest run
killed by one league's 401, corrected scores overwritten every five minutes. None of it was
reportable.
"""

import weakref

from celery.signals import beat_init, celeryd_init

from app.core.observability import API, BEAT, WORKER, init_sentry


def _receiver_names(signal) -> set[str]:
    names = set()
    for _lookup_key, receiver in signal.receivers:
        if isinstance(receiver, weakref.ReferenceType):
            receiver = receiver()
        names.add(getattr(receiver, "__name__", ""))
    return names


def test_worker_and_beat_both_initialise_sentry():
    """Both, and separately. Beat is its own process and would otherwise inherit nothing."""
    import app.workers.celery  # noqa: F401  (importing is what connects the signals)

    assert "_init_worker_sentry" in _receiver_names(celeryd_init)
    assert "_init_beat_sentry" in _receiver_names(beat_init)


def test_no_dsn_is_a_supported_state_not_a_failure():
    """Local development has no DSN. Returning False quietly matters because the alternative —
    warning on every process start — trains people to ignore the startup logs, which is where
    the staleness fingerprint is also printed."""
    assert init_sentry(WORKER) is False


def test_every_component_goes_through_the_one_initialiser():
    """Three call sites drift: the API gains a tag the worker lacks, and an issue stream
    filtered on it silently reads as 'that component is fine'. Pinned by checking that no
    caller reimplements sentry_sdk.init itself."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    callers = [
        backend / "app" / "main.py",
        backend / "app" / "workers" / "celery.py",
    ]
    for path in callers:
        body = path.read_text(encoding="utf-8")
        assert "init_sentry" in body, f"{path.name} does not initialise Sentry"
        assert "sentry_sdk.init(" not in body, f"{path.name} initialises Sentry directly"


def test_the_components_are_distinct_labels():
    """They become the `component` tag, which is the only thing separating a worker stack
    trace from an API one in the issue stream."""
    assert len({API, WORKER, BEAT}) == 3
