"""Deploy-time settings that fail SILENTLY when wrong.

Nothing here can be caught by running the app: a container that is killed for using too much
memory logs no traceback, no error and no warning — it logs a clean "Application startup
complete" and then simply restarts. That is indistinguishable from a healthy service unless
somebody counts the restarts, which is how this went unnoticed through nine restarts in twenty
minutes on 2026-08-23.
"""

from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
RENDER_YAML = (Path(__file__).resolve().parents[2] / "infra" / "render.yaml").read_text(
    encoding="utf-8"
)


def test_the_worker_count_is_not_hardcoded():
    """THE BUG THIS FILE EXISTS FOR.

    Render logs "Setting WEB_CONCURRENCY=1 by default, based on available CPUs" on every boot,
    and a hardcoded `-w 2` silently beat it — the flag wins over the environment variable. Two
    workers is TWO FULL COPIES of the app (~128MB each) on a 512MB starter instance, plus the
    master and a connection pool apiece, which left no headroom and got the container OOM-killed
    on a loop.
    """
    command = next(line for line in DOCKERFILE.splitlines() if line.startswith("CMD"))

    assert "-w 2 " not in command, "worker count is hardcoded; the platform's setting is ignored"
    assert "WEB_CONCURRENCY" in command


def test_the_container_still_binds_the_port_render_routes_to():
    """A hardcoded 8000 means the container is healthy inside itself while Render sees nothing
    listening where it looked."""
    command = next(line for line in DOCKERFILE.splitlines() if line.startswith("CMD"))

    assert "${PORT:-8000}" in command


def test_gunicorn_is_exec_ed_so_it_receives_signals():
    """Shell form leaves /bin/sh as PID 1, which does not forward SIGTERM — so a deploy would
    cut in-flight requests instead of draining them."""
    command = next(line for line in DOCKERFILE.splitlines() if line.startswith("CMD"))

    assert command.startswith("CMD exec gunicorn"), command


def test_migrations_run_before_the_api_serves_traffic():
    """In preDeployCommand rather than a release hook, so they happen exactly once and never
    race the worker or beat coming up against an older schema."""
    assert "preDeployCommand: alembic upgrade head" in RENDER_YAML


def test_every_service_runs_from_the_same_image():
    """One image, three roles. Beat is the easy one to forget, and this project has already lost
    hours to workers running with no beat — so nothing executed on the schedule at all."""
    for name in ("sportpiq-api", "sportpiq-worker", "sportpiq-beat"):
        assert f"name: {name}" in RENDER_YAML, name
