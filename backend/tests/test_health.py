"""GET /health — liveness, plus which code the process actually loaded.

The version block exists because long-lived processes here silently serve whatever they
imported at launch (see app/core/code_version.py). Asserted loosely on shape rather than
values: the fingerprint changes every time a source file is saved, so pinning it would make
this test fail on every edit.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_loaded_code_version():
    """scripts/check_stale.py relies on this shape to tell a stale process from a live one —
    a process too old to emit it is itself proof of staleness."""
    body = client.get("/health").json()
    code = body["code"]

    assert code["loaded"]["fingerprint"], "must report what this process loaded"
    assert code["current"]["fingerprint"], "must report what is on disk now"
    assert isinstance(code["stale"], bool)


def test_health_is_not_stale_in_process():
    """Within one process the loaded and current fingerprints agree unless a file was saved
    mid-run — so a freshly imported app must never report itself stale."""
    code = client.get("/health").json()["code"]
    assert code["stale"] == (code["loaded"]["fingerprint"] != code["current"]["fingerprint"])
