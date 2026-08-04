"""models_registry.artefact_path must resolve on a machine that did not train the model.

Every model registered before this stored an absolute Windows path, which cannot load in a
Linux container — so the first deploy would have failed to load any model, and TDD §3.1's
design that promotion is a DB update rather than a redeploy would not have held.

No network, no DB.
"""

from pathlib import Path

from app.core.config import Settings
from app.models_ml.base import resolve_artefact_path


def test_bare_filename_resolves_against_the_configured_models_dir(tmp_path, monkeypatch):
    """The shape every newly-registered row uses."""
    artefact = tmp_path / "football_xgb_20260804155230.joblib"
    artefact.write_bytes(b"not a real artefact")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        assert Path(resolve_artefact_path(artefact.name)) == artefact
    finally:
        config.get_settings.cache_clear()


def test_a_windows_path_from_another_machine_falls_back_to_the_models_dir(tmp_path, monkeypatch):
    """THE deploy case. A container reading a row written on the trainer's Windows laptop must
    still find the artefact.

    The filename cannot be taken with Path(...).name here: on Linux, backslashes are ordinary
    characters, so Path(r"C:\\x\\y.joblib").name is the entire string. Getting this wrong looks
    like it works on Windows and silently fails everywhere else — which is exactly how the
    original absolute paths survived unnoticed.
    """
    artefact = tmp_path / "nba_xgb_20260728142552.joblib"
    artefact.write_bytes(b"not a real artefact")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        # A directory that exists on NO machine. Naming a real local path here would pass for
        # the wrong reason on the training machine: the file is genuinely there, so the
        # leave-it-alone branch answers first and the fallback is never exercised.
        stored = r"D:\some-other-trainer\ml\artifacts\nba_xgb_20260728142552.joblib"
        assert Path(resolve_artefact_path(stored)) == artefact
        posix_stored = "/home/trainer/ml/artifacts/nba_xgb_20260728142552.joblib"
        assert Path(resolve_artefact_path(posix_stored)) == artefact
    finally:
        config.get_settings.cache_clear()


def test_an_absolute_path_that_really_exists_is_left_alone(tmp_path, monkeypatch):
    """Rows predating the migration keep working on the machine that wrote them, so this is not
    a flag day: a row the migration missed still loads locally."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    artefact = elsewhere / "tennis_xgb_20260801195313.joblib"
    artefact.write_bytes(b"not a real artefact")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        assert Path(resolve_artefact_path(str(artefact))) == artefact
    finally:
        config.get_settings.cache_clear()


def test_models_dir_defaults_to_the_repo_artifacts_dir():
    """Local dev sets nothing; a deployment sets MODELS_DIR. The default has to point at the
    real ml/artifacts or every developer would need the env var just to run the app."""
    assert Settings(models_dir="").models_path.parts[-2:] == ("ml", "artifacts")
    assert Settings(models_dir="/srv/models").models_path == Path("/srv/models")
