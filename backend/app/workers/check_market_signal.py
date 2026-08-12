"""Weekly check of the pre-registered trigger for re-admitting `goals_total`.

A decision that depends on a number crossing a threshold should not depend on someone
remembering to look. This project has already lost time to exactly that: `/history` sat
described as a 501 stub for weeks while it was live and serving a contaminated figure, and the
snapshot task collected one row in a day because nothing was watching it.

So the trigger from app/predictions/market_signal.py is evaluated on a schedule and reported.
It does NOT lift the bar by itself, deliberately — re-admitting a market to the headline pick
is a product decision, and an automated change to what users are told to bet on is the wrong
thing to do quietly. The task's job is to make the moment impossible to miss, not to act on it.

Costs one query and no API calls.
"""

import logging

from app.core.database import async_session_factory
from app.predictions.market_signal import measure_goals_total_signal
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)


async def _check_market_signals() -> None:
    async with async_session_factory() as db:
        signal = await measure_goals_total_signal(db)

    if signal.meets_trigger:
        # WARNING rather than INFO because this one needs a human: the pre-registered condition
        # for a product change has been met and the change has deliberately not been made.
        logger.warning(
            "PRE-REGISTERED TRIGGER MET — %s. goals_total may now be removed from "
            "NO_DEMONSTRATED_SIGNAL_MARKETS in app/fixtures/router.py. See "
            "app/predictions/market_signal.py for the thresholds and why they were chosen.",
            signal.describe(),
        )
        try:
            import sentry_sdk

            if sentry_sdk.get_client().is_active():
                sentry_sdk.capture_message(
                    f"goals_total signal trigger met: {signal.describe()}", level="info"
                )
        except Exception:  # noqa: BLE001 - a reporting failure must not fail the check
            logger.debug("could not report the trigger to Sentry", exc_info=True)
    else:
        logger.info("goals_total signal check — %s", signal.describe())


@celery_app.task(name="app.workers.check_market_signal.check_market_signals")
def check_market_signals() -> None:
    """Weekly. Settled fixtures accumulate at roughly 90/week, so a shorter cadence would
    re-report the same figure; a longer one would sit on a met trigger for too long."""
    run_task(_check_market_signals())
