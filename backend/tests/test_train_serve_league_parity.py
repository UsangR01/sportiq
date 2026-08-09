"""The set of leagues the model is TRAINED on must match the set the app SERVES.

These are two separate wirings -- ml/training/train_football.py's LEAGUES and
app/adapters/api_football.py's LEAGUE_IDS -- and they drifted apart silently. Nine Tier-1
leagues were collected, pooled and shipped inside a trained artefact while LEAGUE_IDS still
listed nine, so the model had learned from leagues the app ingested nothing for: no fixture,
no odds, no prediction, nothing a user could ever see. Nothing failed; the work simply did not
reach anybody.

The reverse direction matters too but is NOT an error: a league can legitimately be served
while untrained, because one model serves the whole sport (the Scottish Premiership, MLS and
the CSL were all served by an EPL/Brasileirao-trained model long before their own history was
collected). So this asserts one direction only -- everything trained must be served -- and
reports the other as information.

Parsed with ast rather than imported: train_football.py pulls in xgboost, mlflow and optuna at
module scope, none of which the backend test suite should need to have installed.
"""

import ast
from pathlib import Path

import pytest

from app.adapters.api_football import LEAGUE_IDS

TRAIN_SCRIPT = Path(__file__).resolve().parents[2] / "ml" / "training" / "train_football.py"


def _trained_leagues() -> list[str]:
    """Read the LEAGUES list literal out of train_football.py without importing it."""
    tree = ast.parse(TRAIN_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "LEAGUES":
                return [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant)
                ]
    raise AssertionError(f"no LEAGUES assignment found in {TRAIN_SCRIPT}")


@pytest.mark.skipif(not TRAIN_SCRIPT.exists(), reason="ml/ not present in this checkout")
def test_every_trained_league_is_actually_served():
    """THE guard. A league in the training pool but not in LEAGUE_IDS is invisible work."""
    missing = sorted(set(_trained_leagues()) - set(LEAGUE_IDS))
    assert not missing, (
        f"trained on but never ingested: {missing}. Add them to LEAGUE_IDS (and "
        f"CALENDAR_YEAR_SEASON_LEAGUES if they run Jan-Dec), or drop them from "
        f"train_football.py's LEAGUES."
    )


@pytest.mark.skipif(not TRAIN_SCRIPT.exists(), reason="ml/ not present in this checkout")
def test_the_training_pool_is_not_accidentally_empty_or_tiny():
    """Guards the parser itself. If the ast walk silently returned [] -- a renamed variable, a
    LEAGUES built by comprehension instead of a literal -- the test above would pass while
    checking nothing at all."""
    assert len(_trained_leagues()) >= 9
