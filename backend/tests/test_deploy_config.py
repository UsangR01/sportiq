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


# === The worker, added 2026-08-24 after it OOM'd itself ===========================================


def test_the_celery_worker_runs_one_child_not_two():
    """THE SAME MISTAKE THE API HAD WITH -w 2, in the other service.

    A prefork child that runs a prediction loads a model (~278MB measured), and one league's
    fixture ingest peaks around 359MB. Two children on a 512MB starter cannot both do that, and
    an OOM kill is silent: the container restarts, the task vanishes, the queue drains, nothing
    changed. That is precisely how the daily ingest failed — draining its queue and altering not
    one fixture, with no error anywhere.
    """
    assert "--concurrency=2" not in RENDER_YAML, "two prefork children do not fit in 512MB"
    assert "--concurrency=1" in RENDER_YAML


def test_the_worker_recycles_its_child_periodically():
    """Without this a loaded model pins memory for the worker's whole lifetime."""
    assert "--max-tasks-per-child" in RENDER_YAML


def test_worker_and_beat_wait_for_the_schema_before_starting():
    """preDeployCommand runs on the WEB service alone and a blueprint starts services in
    parallel, so the worker can come up against an unmigrated database. Its ORM then selects a
    column that does not exist, every query raises, and ingest's per-league try/except swallows
    it — a run that completes, changes nothing and logs no error."""
    # Counted as INVOCATIONS, not mentions: the surrounding comment names the script too.
    invocations = [
        line
        for line in RENDER_YAML.splitlines()
        if line.strip().startswith("dockerCommand:") and "wait_for_schema.py" in line
    ]

    assert len(invocations) == 2, f"both worker and beat must wait, found {invocations}"
    # And exactly one service may MIGRATE: three racing alembic processes is worse than
    # starting late, since alembic takes no lock by default.
    assert RENDER_YAML.count("alembic upgrade head") == 1


def test_the_schema_gate_fails_rather_than_proceeding():
    """A loud restart loop is far easier to diagnose than a worker running against a schema it
    does not match and silently doing nothing."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "wait_for_schema.py").read_text(
        encoding="utf-8"
    )

    assert "raise SystemExit(1)" in source


def test_a_chained_docker_command_is_wrapped_in_a_shell():
    """THE BUG THIS ASSERTION EXISTS FOR, and it took the worker down.

    Render runs dockerCommand EXEC-STYLE, so `a && b` does not chain: everything after && is
    passed to `a` as stray argv, `a` exits 0, and the container ends because that was the whole
    command. The worker restart-looped every few minutes logging only "schema is at ...;
    starting" — celery never reached its own banner, and nothing said why.

    Any command using && must therefore go through /bin/sh -c, and the long-running half must
    be exec'd so it becomes PID 1 and receives SIGTERM.
    """
    for line in RENDER_YAML.splitlines():
        stripped = line.strip()
        if not stripped.startswith("dockerCommand:") or "&&" not in stripped:
            continue
        assert "/bin/sh -c" in stripped, f"chained command not shell-wrapped: {stripped}"
        assert "&& exec " in stripped, f"the long-running half must be exec'd: {stripped}"


def test_no_docker_command_uses_yaml_folding_with_a_chain():
    """A folded scalar (>-) joins lines with spaces and still produces one exec-style string,
    so it looks like a shell command and is not one. That is exactly how this shipped broken."""
    assert "dockerCommand: >-" not in RENDER_YAML
