"""Cross-provider PERSON-name matching, from four real fixtures that had prices and showed none.

TheRundown and BallDontLie both carry the same ATP match and spell the player differently. The
lookup was exact and case-sensitive, so each of these silently cost a fixture its odds — no
error, just an empty card:

    Soonwoo Kwon      vs  SoonWoo Kwon              capitalisation
    Yibing Wu         vs  Wu Yibing                 family name first
    Yunchaokete Bu    vs  Bu Yunchaokete            family name first
    Coleman Wong      vs  Chak Lam Coleman Wong     extra given names

Clubs do not have these problems, which is why the tolerance is opt-in per sport rather than a
general loosening of a matcher that football and NBA rely on.
"""

import pytest

from app.fixtures.service import name_tokens


@pytest.mark.parametrize(
    "ours, theirs",
    [
        ("Soonwoo Kwon", "SoonWoo Kwon"),
        ("Yibing Wu", "Wu Yibing"),
        ("Yunchaokete Bu", "Bu Yunchaokete"),
        ("Coleman Wong", "Chak Lam Coleman Wong"),
    ],
)
def test_the_four_real_spellings_are_recognised_as_one_person(ours, theirs):
    a, b = name_tokens(ours), name_tokens(theirs)
    assert len(a & b) >= 2 and (a <= b or b <= a)


@pytest.mark.parametrize(
    "ours, theirs",
    [
        ("Alexander Zverev", "Mischa Zverev"),  # brothers: one shared token is not a person
        ("Wong", "Chak Lam Coleman Wong"),  # bare surname would match every Wong
        ("Manchester United", "Manchester City"),
        ("Carlos Alcaraz", "Novak Djokovic"),
    ],
)
def test_different_people_are_refused(ours, theirs):
    """The two-shared-token bar is what keeps this from being reckless. Attaching one player's
    price to another is far worse than showing no price — this codebase already shipped that
    bug once, displaying a 1.17 favourite at 8.00."""
    a, b = name_tokens(ours), name_tokens(theirs)
    assert not (len(a & b) >= 2 and (a <= b or b <= a))


def test_the_tolerance_is_off_by_default():
    """Football and NBA match fine today; a looser rule could only make them worse. Pinned
    because the safe default is the whole reason this is a parameter and not a behaviour."""
    import inspect

    from app.fixtures.service import find_fixture_by_abbreviations_and_time

    sig = inspect.signature(find_fixture_by_abbreviations_and_time)
    assert sig.parameters["allow_name_variants"].default is False


def test_only_tennis_opts_in():
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "app" / "workers" / "ingest_odds.py").read_text(
        encoding="utf-8"
    )
    assert 'sport_slug == "tennis"' in body
