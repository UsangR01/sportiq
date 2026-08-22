"""A market-blind artefact must never become the model that serves probabilities.

THIRD APPEARANCE OF THE SAME HAZARD, which is why this is pinned rather than trusted:
  1. train_*.py registered AND activated unconditionally, so a tennis experiment's LOSING arm
     went live merely by finishing.
  2. The --no-activate fix then demoted the incumbent without replacing it, leaving NBA with no
     active model and silently stopping the sport predicting.
  3. seed_model_registry.py is a SEPARATE DOOR to the same table. Its rule is "a newer artefact
     replaces the incumbent" -- and the blind artefact is the newest entry in the manifest, so a
     fresh deployment would have promoted it without anyone choosing to.

The blind model is deliberately WORSE than the one it explains (measured: accuracy 0.5021 vs
0.5082, RPS 0.2119 vs 0.2084) because it has never seen a bookmaker's price. Serving it would
hand users worse probabilities to fix a panel.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.predictions.explanation import BLIND_VERSION_SUFFIX

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TRAINERS = Path(__file__).resolve().parents[2] / "ml" / "training"


def test_the_suffix_the_whole_scheme_keys_on_is_what_the_trainer_writes() -> None:
    """`--market-blind` puts the variant in the artefact FILENAME, and model_version_for derives
    the registry version from that. If those two ever drift, a blind artefact stops being
    recognisable as one and every guard built on the suffix silently stops applying."""
    source = (TRAINERS / "train_football.py").read_text(encoding="utf-8")

    assert 'variant_suffix = "_blind" if MARKET_BLIND else ""' in source
    assert BLIND_VERSION_SUFFIX == "_blind"


def test_seed_model_registry_registers_a_blind_artefact_inactive() -> None:
    """Parsed rather than string-matched, so a reworded comment cannot pass for the guard."""
    tree = ast.parse((SCRIPTS / "seed_model_registry.py").read_text(encoding="utf-8"))

    # Find the branch guarded by the blind check and confirm it constructs its registry row with
    # is_active=False and returns before reaching the demote-and-activate path.
    blind_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(isinstance(n, ast.Name) and n.id == "blind" for n in ast.walk(node.test))
    ]
    assert blind_branches, "no branch keyed on a blind check in seed_model_registry.py"

    for branch in blind_branches:
        actives = [
            keyword
            for node in ast.walk(branch)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "is_active"
        ]
        assert actives, "the blind branch constructs no ModelRegistry row"
        for keyword in actives:
            assert isinstance(keyword.value, ast.Constant)
            assert keyword.value.value is False, "a blind artefact was registered active"
        # It must not fall through into the promotion path below it.
        assert any(isinstance(node, ast.Continue) for node in ast.walk(branch))


def test_the_trainer_forces_no_activate_for_a_blind_run() -> None:
    """Not merely defaulted -- FORCED. A blind run is always a measurement, never a promotion."""
    source = (TRAINERS / "train_football.py").read_text(encoding="utf-8")

    assert "ACTIVATE_ON_REGISTER = not (args.no_activate or args.market_blind)" in source
