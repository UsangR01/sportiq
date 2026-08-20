"""Leagues temporarily withheld from the FEED as a whole, rather than a market at a time.

THIS IS A DIFFERENT INSTRUMENT FROM THE TWO MARKET GATES, and the difference is the point:

    goals_availability.py    one market, admitted per league on measured skill
    corners_availability.py  one market, withheld per league on settlement supply
    THIS                     EVERY market, withheld per league, deliberately temporary

corners/goals answer "which market should win this league's headline". This answers "should
this league be offering picks at all right now", which no per-market gate can express: a
league whose picks are simply not working does not want its second-best market promoted, it
wants to stop making recommendations until something changes.

=== MLS, suppressed 2026-08-19 on a direct product decision ===

Reported from the live card record: roughly 4 of 11 settled MLS picks landing. That is a small
sample and the decision was the user's rather than a measured threshold being crossed -- stated
plainly so nobody later reads this as a bar that was met. What makes it defensible is that the
cost of being wrong is asymmetric: continuing to recommend a league that looks broken damages
trust far more than withholding a league that turns out to be fine, and withholding is
reversible in one edit.

=== SCOPE: UPCOMING ONLY. SETTLED FIXTURES STAY VISIBLE, AND THAT IS DELIBERATE ===

Only fixtures that have not been decided are hidden. A settled MLS card keeps its result and
its tick or cross.

Hiding those too would be the more literal reading of "drop it from my cards", and it is
refused because it would silently IMPROVE the visible track record by deleting the losses that
prompted this -- exactly the retroactive-filtering bias CLAUDE.md records from the Hearts v
Dundee Utd incident, where a guard tightened after the fact erased a published, winning pick
and made the record look better than it was. Withholding a recommendation is a product choice;
editing the scoreboard is not. /history and /history/summary are untouched for the same reason,
so MLS keeps counting toward every accuracy this project reports.

=== LIFTING IT ===

Delete the slug. The honest re-admission condition is a real measurement rather than a feeling
that enough time has passed: at least MIN_REPORTABLE_N settled MLS card picks (the same floor
/history/summary uses before it will quote an accuracy at all), with a 95% Wilson interval whose
lower bound clears the mean base rate of the markets those picks came from. At n=11 no interval
can distinguish a broken league from an unlucky one, which is the real reason this is a product
call and not a measurement.
"""

# Every market is withheld for these leagues, on upcoming fixtures only. Empty this set to
# restore normal behaviour everywhere.
SUPPRESSED_LEAGUES = frozenset(
    {
        "mls",
    }
)


def offers_picks(league_slug: str | None) -> bool:
    """False only for a league explicitly suppressed above.

    An unknown or missing league returns True: this list is an explicit, temporary denylist of
    leagues someone decided to withhold, never a default. That is the opposite polarity to
    offers_goals (which is earned by measurement) and matches offers_corners' benefit of the
    doubt -- a league nobody has ruled on keeps working."""
    if not league_slug:
        return True
    return league_slug not in SUPPRESSED_LEAGUES
