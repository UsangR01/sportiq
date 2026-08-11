"""Error reporting, wired identically for every long-lived process.

Sentry was initialised in `create_app()` and nowhere else, so it covered the API and NOTHING
the Celery worker or beat did. That is the wrong way round for this project: the API fails
loudly in front of a user, while the workers are where every silent failure has actually
happened — a stale scheduler that never dispatched a task, an ingest run killed mid-way by one
league's 401, corrected scores overwritten every five minutes. None of it was reportable.

WHY A SHARED FUNCTION RATHER THAN THREE init() CALLS

Three call sites drift. The API would gain a release tag that the worker lacks, or the worker
a tag the beat lacks, and the first time someone filters the issue stream by a tag half the
processes never set, the absent events read as "that component is fine". One function, one
shape, one place to change.

WHAT IS TAGGED, AND WHY IT IS THESE THINGS

  component    which process raised it — the whole point of this module
  fingerprint  which code that process actually loaded

The fingerprint is here deliberately. This project's recurring failure is a process serving
code older than the files on disk, and it produces errors that are genuinely impossible to
reproduce from the current source. Seeing the loaded fingerprint on the event turns "this
stack trace makes no sense" into "that process was stale", which is a two-second read instead
of an afternoon. See app/core/code_version.py.

BEAT IS MONITORED, NOT JUST INSTRUMENTED

`monitor_beat_tasks=True` registers every beat_schedule entry as a Sentry cron monitor, so a
scheduled task that STOPS ARRIVING raises an alert. Ordinary error reporting cannot do this:
the failure it catches produces no exception, no failed task and no log line — the work simply
never happens. snapshot_picks silently collected one row in twenty-four hours that way, and
would have reached its five-week measurement date with nothing in it.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)

API = "api"
WORKER = "worker"
BEAT = "beat"


def init_sentry(component: str) -> bool:
    """Initialise error reporting for this process. Returns whether it was actually enabled.

    A missing DSN is a normal, supported state — local development has none — so this returns
    False quietly rather than warning on every start. Any other failure is swallowed and
    logged: a process must never fail to boot because its error reporter would not start,
    which would turn a diagnostic into an outage.
    """
    settings = get_settings()
    dsn = settings.sentry_dsn_backend
    if not dsn:
        return False

    try:
        import sentry_sdk

        integrations = []
        if component in (WORKER, BEAT):
            from sentry_sdk.integrations.celery import CeleryIntegration

            # Only beat registers cron monitors. Enabling it on the worker too would have
            # every worker re-report check-ins for a schedule it does not own.
            integrations.append(CeleryIntegration(monitor_beat_tasks=(component == BEAT)))

        from app.core.code_version import loaded_code_version

        version = loaded_code_version()

        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            # Identifies the deployed build in production, where the source is baked into an
            # image. In development it is the same across processes and the fingerprint below
            # is the tag that actually distinguishes them.
            release=version.git_sha,
        )
        sentry_sdk.set_tag("component", component)
        sentry_sdk.set_tag("code_fingerprint", version.fingerprint)
    except Exception:  # noqa: BLE001 - never let a diagnostic stop a process starting
        logger.warning("could not initialise Sentry for %s", component, exc_info=True)
        return False

    logger.info("Sentry initialised for %s (release=%s)", component, version.git_sha)
    return True
