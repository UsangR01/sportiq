"""Is anything running stale code?

The recurring failure in this project is a long-lived process serving whatever it imported at
launch. It never errors — it just applies old logic, which looks like a fix that did not work.
In one day that cost hours across four separate incidents, despite being documented.

scripts/dev_worker.py prevents it for the worker started through it. This answers the wider
question for every process at once, including uvicorn (whose --reload watcher has silently
stopped twice here) and any worker started by hand.

    python scripts/check_stale.py

Exit code 0 when everything is current, 1 when something is stale — so it can gate a
verification step rather than being read by eye.
"""

import json  # noqa: F401  (used by the Redis branch below)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from redis import Redis  # noqa: E402

from app.core.code_version import current_code_version  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.workers.celery import WORKER_VERSION_KEY  # noqa: E402

API_URL = "http://localhost:8000/health"


def _report(name: str, loaded: str | None, current: str, detail: str = "") -> bool:
    """Returns True when this process is stale."""
    if loaded is None:
        print(f"  {name:<22} not running or not reporting  {detail}")
        return False
    stale = loaded != current
    mark = "STALE" if stale else "ok"
    print(f"  {name:<22} {mark:<6} loaded={loaded}  {detail}")
    return stale


def main() -> int:
    current = current_code_version()
    print(
        f"on disk now: fingerprint={current.fingerprint} "
        f"git={current.git_sha} dirty={current.git_dirty}\n"
    )

    stale_any = False
    down_any = False

    # --- API ---
    # Three outcomes, deliberately distinguished. A process that ANSWERS but reports no
    # version predates this check entirely, which is itself proof of staleness — collapsing
    # that into "not running" would hide the exact case this was built for.
    try:
        body = httpx.get(API_URL, timeout=5).json()
    except Exception as exc:  # noqa: BLE001 - a down API is a reportable state, not a crash
        print(f"  {'uvicorn (API)':<22} DOWN   unreachable  ({type(exc).__name__})")
        down_any = True
    else:
        loaded = ((body.get("code") or {}).get("loaded") or {}).get("fingerprint")
        if loaded is None:
            print(
                f"  {'uvicorn (API)':<22} {'STALE':<6} responding, but too old to report a "
                "version — it predates this check"
            )
            stale_any = True
        else:
            stale_any |= _report("uvicorn (API)", loaded, current.fingerprint)

    # --- Celery worker ---
    try:
        client = Redis.from_url(get_settings().redis_url)
        raw = client.get(WORKER_VERSION_KEY)
        client.close()
        payload = json.loads(raw) if raw else {}
        loaded = payload.get("fingerprint")
        started = (payload.get("started_at") or "")[:19]
    except Exception as exc:  # noqa: BLE001
        loaded, started = None, f"({type(exc).__name__})"
        down_any = True
    stale_any |= _report("celery worker", loaded, current.fingerprint, f"started={started}")

    print()
    if down_any and not stale_any:
        print("A process is DOWN — nothing stale, but the stack is not fully up.")
        return 1
    if stale_any:
        print("STALE PROCESS DETECTED — restart it before trusting anything it produced.")
        print("  worker:  python scripts/dev_worker.py")
        print("  API:     restart uvicorn (its --reload watcher cannot be trusted to recover)")
        return 1
    print("all reporting processes are running current code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
