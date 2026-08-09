"""The completeness floor on _pick_best (see MIN_FEATURE_COMPLETENESS).

A prediction assembled from a mostly-missing feature vector is not a statement about the
fixture -- it is the model's fallback prior wearing a team name. The real case: Tottenham vs
Newcastle on 2026-08-29 had 3 of 31 features populated (EPL's season had not opened, so
neither side had played) and served 1X at 99.7%, i.e. Newcastle to neither win nor draw at
0.3%. The same vector through the previous 9-league artefact gave 93.6%, so pooling worsened
an existing defect rather than introducing one.

The floor rather than a probability cap, because a cap would hit the wrong band: measured over
159 real predictions, the 0.50+ completeness band is just as extreme (97% of its picks >= 0.90)
and is 81% correct on n=64 settled fixtures. Confidence built on real data is the product
working, and must survive.
"""

import pytest

from app.fixtures.router import (
    MIN_FEATURE_COMPLETENESS,
    _MarketCandidate,
    _pick_best,
)


def _candidate(probability, completeness, *, odds=None, selection="home", market="h2h"):
    return _MarketCandidate(
        selection=selection,
        probability=probability,
        odds=odds,
        market=market,
        line=None,
        feature_completeness=completeness,
    )


def test_an_empty_vector_cannot_produce_a_pick_however_confident_it_looks():
    """THE guard. 99.7% off 3-of-31 features is the exact number a real user would have seen."""
    assert _pick_best([_candidate(0.997, 0.097)]) is None


def test_confidence_built_on_real_data_survives():
    """The band this must NOT touch. 0.50+ completeness is equally extreme and measured 81%
    correct -- suppressing it would remove the picks that actually earn their confidence."""
    pick = _pick_best([_candidate(0.95, 0.62)])
    assert pick is not None and pick.probability == 0.95


def test_the_floor_outranks_probability_rather_than_merely_de_ranking_it():
    """Low-completeness picks were not just present, they RANKED FIRST: with no odds, EV
    ranking degrades to probability ranking, so the emptiest vectors sat at the top of the
    feed. Ordering alone is not enough -- the empty one must be gone, not second."""
    pick = _pick_best([_candidate(0.99, 0.10), _candidate(0.71, 0.55, selection="away")])
    assert pick is not None
    assert pick.selection == "away" and pick.probability == 0.71


def test_an_unmeasured_prediction_is_kept():
    """feature_completeness shipped nullable with no backfill, because older predictions
    genuinely have no measurement. Treating unmeasured as failing would silently erase every
    prediction made before that migration."""
    pick = _pick_best([_candidate(0.72, None)])
    assert pick is not None and pick.probability == 0.72


@pytest.mark.parametrize(
    "completeness, kept",
    [(MIN_FEATURE_COMPLETENESS - 0.01, False), (MIN_FEATURE_COMPLETENESS, True)],
)
def test_the_boundary_is_inclusive(completeness, kept):
    assert (_pick_best([_candidate(0.72, completeness)]) is not None) is kept


def test_the_floor_is_below_mobiles_limited_data_threshold():
    """Different instruments, deliberately different numbers. Mobile's 0.35 dims a badge and
    says "limited data" -- cheap if over-applied. This removes a pick outright, so it clears a
    stricter bar. Equalising them would suppress the 0.25-0.35 band, which measured one mildly
    extreme pick in seventeen and does not deserve removal."""
    assert MIN_FEATURE_COMPLETENESS < 0.35
