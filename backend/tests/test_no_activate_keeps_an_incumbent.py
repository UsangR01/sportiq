"""--no-activate must not leave a sport with NO active model.

THE BUG THIS PINS, introduced and caught within the hour on 2026-08-13. _register_model demotes
every currently-active row and then inserts the new one. That is right when the new row is being
promoted, and wrong under --no-activate: the incumbent is demoted, nothing replaces it, and the
sport is left with zero active models.

Consequence is silent and total. app/models_ml/runner.py resolves the model to serve from the
registry, so a sport with no active row simply stops producing predictions -- no exception at
training time, no failure at ingest, just a feed that quietly goes empty. NBA lost its model
this way while three form-window measurement arms ran back to back; /stats/model dropped the
sport entirely, which is how it was noticed.

Parsed with ast rather than imported: the training scripts pull in xgboost, mlflow and optuna at
module scope, none of which the backend suite should need. Same technique as
test_train_serve_league_parity.py.
"""

import ast
from pathlib import Path

import pytest

TRAINING_DIR = Path(__file__).resolve().parents[2] / "ml" / "training"
SCRIPTS = ["train_nba.py", "train_tennis.py", "train_football.py"]


def _demotion_is_guarded(source: str) -> bool:
    """True when `row.is_active = False` only ever runs under a condition.

    Deliberately structural rather than a string match: the point is that the demotion loop sits
    inside an `if`, not that any particular flag name was used.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Attribute)]
        if not any(t.attr == "is_active" for t in targets):
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value is False):
            continue
        # Walk back up: this assignment must have an `if` somewhere among its ancestors.
        if not _has_enclosing_if(tree, node):
            return False
    return True


def _has_enclosing_if(tree: ast.AST, target: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for descendant in ast.walk(node):
                if descendant is target:
                    return True
    return False


@pytest.mark.parametrize("script", SCRIPTS)
def test_demotion_only_happens_when_something_replaces_the_incumbent(script):
    source = (TRAINING_DIR / script).read_text(encoding="utf-8")
    assert _demotion_is_guarded(source), (
        f"{script} demotes the active model unconditionally. Under --no-activate that leaves the "
        "sport with no active model at all, and it stops predicting silently."
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_training_script_can_measure_without_promoting(script):
    """A training run is often a MEASUREMENT. Without this flag an experiment arm becomes the
    served model just by finishing -- which happened to tennis, where the LOSING arm went live."""
    source = (TRAINING_DIR / script).read_text(encoding="utf-8")
    assert "--no-activate" in source
    assert "ACTIVATE_ON_REGISTER" in source
