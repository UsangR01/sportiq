"""What the push gate is allowed to interrupt someone for (notify_users._notify_new_pick).

It used to fire only for ConfidenceTier.HIGH. That tier measured WORST on settled pre-match
predictions — HIGH claimed 74.1% and delivered 60.9% (n=69) while MEDIUM claimed 57.8% and
delivered 68.5% (n=89) — so the most intrusive channel was pointing at the weaker set.

The replacement is deliberately NOT a new threshold invented on that evidence; n=69 is far too
thin to justify one. It is the rule the product already applies to decide a pick is worth
showing at all, so a notification can no longer disagree with the app.
"""

from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "workers" / "notify_users.py").read_text(
    encoding="utf-8"
)


def test_the_confidence_tier_no_longer_gates_notifications():
    """The tier is hidden from users as misleading; continuing to ACT on it for push would be
    the same claim by another route."""
    assert "if prediction.confidence_tier != ConfidenceTier.HIGH" not in SOURCE


def test_the_gate_reuses_the_feeds_own_selection():
    """Not a new rule. _bulk_best_picks applies the base-rate edge, market-disagreement and
    completeness guards the feed already uses, so a push can only ever be sent for a pick the
    app itself would surface."""
    assert "_bulk_best_picks" in SOURCE


def test_a_fixture_with_no_surfaceable_pick_sends_nothing():
    """The volume control. best_outcome returns something for almost any priced fixture, so
    dropping the tier gate without replacing it would have notified on nearly every match."""
    assert "if pick is None or pick.odds is None:" in SOURCE
    gate = SOURCE.index("if pick is None or pick.odds is None:")
    assert "return" in SOURCE[gate : gate + 300]


def test_the_notification_no_longer_claims_high_confidence():
    """The old title asserted exactly what the product has stopped asserting elsewhere."""
    assert 'title="New high-confidence pick"' not in SOURCE
    assert 'title="New pick"' in SOURCE


def test_a_totals_pick_says_what_the_line_is_about():
    """ "UNDER @ 1.30" is meaningless without knowing under WHAT. The old body only ever
    described h2h picks, so it never had to say — and that was the same bug as the gate: the
    push described a different market from the card."""
    from app.workers.notify_users import format_selection

    class _Pick:
        def __init__(self, market, selection, line):
            self.market, self.selection, self.line = market, selection, line

    assert format_selection(_Pick("goals_total", "under", 3.5)) == "UNDER 3.5 goals"
    assert format_selection(_Pick("corners_total", "over", 9.5)) == "OVER 9.5 corners"
    assert format_selection(_Pick("h2h", "home", None)) == "HOME"
