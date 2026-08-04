"""Run the Celery worker (or beat) in development with auto-restart on code changes.

WHY THIS EXISTS

Celery has no equivalent of `uvicorn --reload` — the option was removed in 4.x — so a worker
serves whatever code it imported at launch, forever. Editing an adapter or a task changes
nothing until someone remembers to restart, and the failure is silent: the worker keeps
succeeding, just with old logic. That asymmetry (the API reloads, the worker does not) is why
the API rarely goes stale while the worker constantly does.

This is not a theoretical tidy-up. In a single day it caused, among others:
  - a fixed kickoff-refresh looking broken for eight hours, because the worker predated the fix
  - corrected tennis scores being silently overwritten every five minutes by the buggy copy
  - an injury-ingest fix appearing not to work until the worker was restarted by hand

Documentation did not prevent any of them — it was written down in CLAUDE.md and in the
start-up runbook, and still recurred. Hence tooling rather than another note.

PRODUCTION IS DIFFERENT, DELIBERATELY

This wrapper is for development only. In production a deploy replaces the container, so the
worker cannot lag its own image — provided the deploy restarts the worker and beat services
and not just the web service, which is a real and easy misconfiguration to make. Do not run
watchmedo in production: restarting on file change is exactly the wrong behaviour there.

USAGE (from backend/)
    ../backend/.venv/Scripts/python scripts/dev_worker.py            # worker, auto-restarting
    ../backend/.venv/Scripts/python scripts/dev_worker.py --beat     # beat, auto-restarting
"""

import argparse
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
WATCHMEDO = BACKEND_DIR / ".venv" / "Scripts" / "watchmedo.exe"
CELERY = BACKEND_DIR / ".venv" / "Scripts" / "celery.exe"

# Only app/ — watching the whole backend would restart on every test edit, and a worker that
# restarts mid-task is worse than one that lags a test file.
WATCH_DIR = BACKEND_DIR / "app"


def build_command(beat: bool) -> list[str]:
    celery_args = ["-A", "app.workers.celery"]
    if beat:
        celery_args += ["beat", "--loglevel=info"]
    else:
        # --pool=solo is mandatory on Windows: there is no prefork pool, and the other pools
        # run every task in one long-lived process, which is what makes app.workers.celery's
        # run_task() engine-disposal necessary in the first place.
        celery_args += ["worker", "--loglevel=info", "--pool=solo"]

    return [
        str(WATCHMEDO),
        "auto-restart",
        f"--directory={WATCH_DIR}",
        "--pattern=*.py",
        "--recursive",
        # Without this, watchmedo signals the child but does not wait for the port/broker
        # connection to drop, and the replacement worker can fail to register its queues.
        "--signal=SIGTERM",
        "--",
        str(CELERY),
        *celery_args,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--beat", action="store_true", help="run beat instead of the worker")
    args = parser.parse_args()

    for path in (WATCHMEDO, CELERY):
        if not path.exists():
            print(f"missing {path} — run: .venv/Scripts/python -m pip install -r requirements.txt")
            return 1

    command = build_command(args.beat)
    role = "beat" if args.beat else "worker"
    print(f"celery {role}: auto-restarting on changes under {WATCH_DIR}")
    print(f"  {' '.join(command)}\n")
    try:
        return subprocess.call(command, cwd=BACKEND_DIR)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
