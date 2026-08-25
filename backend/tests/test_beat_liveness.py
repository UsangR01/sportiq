"""Guards on the scheduler itself, from a failure that cost a day of measurement.

snapshot_picks.py and its beat_schedule entry were written three hours AFTER beat was launched,
so the running scheduler held a schedule with no snapshot entry. Nothing errored. The task
simply never ran, and a measurement designed to accumulate for five weeks collected one row in
twenty-four hours.

A stale WORKER applies old logic to work it receives. A stale BEAT never dispatches the work at
all — quieter, and worse, because there is no failed task to find.
"""

import weakref

from celery.signals import beat_init, worker_ready

from app.workers.celery import BEAT_VERSION_KEY, WORKER_VERSION_KEY, celery_app


def test_beat_publishes_its_own_code_version():
    """check_stale.py reported "ok" for a stale scheduler because only the worker published.
    Beat is a separate process with its own import, so it needs its own key — sharing one
    would mean whichever started last silently overwrote the other."""
    assert BEAT_VERSION_KEY != WORKER_VERSION_KEY

    def receiver_names(signal):
        names = set()
        for _lookup_key, receiver in signal.receivers:
            if isinstance(receiver, weakref.ReferenceType):
                receiver = receiver()
            names.add(getattr(receiver, "__name__", ""))
        return names

    assert "_publish_beat_code_version" in receiver_names(beat_init)
    assert "_publish_worker_code_version" in receiver_names(worker_ready)


def test_every_scheduled_task_is_one_the_worker_can_actually_route():
    """Beat dispatches by NAME. A task scheduled but missing from the worker's `include` list
    is accepted by the broker and then dies as "Received unregistered task" — which looks like
    a worker error rather than a wiring mistake, and only on the schedule's own interval.

    This has already bitten here once: backfill_predictions and backfill_tennis_predictions
    relied on another module's lazy import having registered them as a side effect.

    Checked against `include` rather than against celery_app.tasks, because the live registry
    is only populated once a worker imports those modules — in a plain pytest process it is
    empty, so asserting on it would fail for every task and prove nothing. `include` is what
    the worker actually loads, which is the real contract."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    loaded_modules = set(celery_app.conf.include)

    unroutable = {task for task in scheduled if task.rsplit(".", 1)[0] not in loaded_modules}
    assert not unroutable, f"scheduled but the worker never imports it: {sorted(unroutable)}"


def test_the_snapshot_task_is_actually_scheduled():
    """The whole pre-registered measurement (docs/history-metrics-spec.md §9) rests on this one
    entry existing. It is asserted by name because its absence is silent."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "app.workers.snapshot_picks.snapshot_shown_picks" in scheduled


def test_tennis_has_two_odds_schedules_at_different_cadences():
    """The two tennis odds providers cost completely different things, so they cannot share a
    schedule:

        BallDontLie   free (600 req/MINUTE) but prices few matches   -> hourly
        TheRundown    metered (5,000 req/MONTH) but far broader      -> 2-hourly

    Collapsing them onto one entry would either drag the free, hourly refresh down to the
    metered cadence — the six-hour staleness gap this closes — or push the metered one to
    ~1,440 calls/month, which does not fit alongside football's expected ~3,600.
    """
    schedule = celery_app.conf.beat_schedule
    free = schedule["ingest-tennis-odds-hourly"]
    metered = schedule["ingest-tennis-rundown-odds-every-2-hours"]

    from tests.test_ingest_odds_quota import schedule_runs_per_day

    assert free["task"] != metered["task"]
    assert schedule_runs_per_day(metered["schedule"]) == 12
    # The metered job must never run more often than the free one.
    assert schedule_runs_per_day(metered["schedule"]) < schedule_runs_per_day(free["schedule"])


def test_long_cadence_tasks_use_wall_clock_schedules():
    """An interval schedule counts from BEAT PROCESS START, and beat restarts on every deploy.
    On a deploy-heavy day the 6-hour odds interval never came due at all — beat's own logs
    showed every short task dispatching while ingest-odds-every-6-hours was simply absent —
    and the weekly market-signal audit had NEVER fired in production. Anything of an hour or
    longer must be a crontab (absolute wall clock, survives restarts)."""
    for name, entry in celery_app.conf.beat_schedule.items():
        schedule = entry["schedule"]
        if isinstance(schedule, int | float):
            assert float(schedule) < 3600, (
                f"{name} uses a {schedule}s interval; a deploy resets it before it comes due — "
                "use crontab"
            )
