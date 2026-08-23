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

=== SCOPE: EVERYTHING, INCLUDING SETTLED FIXTURES, AS OF 2026-08-23 ===

It began as upcoming-only, on the reasoning below. That held for four days and then produced a
report -- "we took out the MLS games, but they are showing again today (6/11)" -- which was the
exemption working exactly as designed and being experienced as a failure. Asked a second time
to hide them including history, MLS moved to SUPPRESSED_LEAGUES_INCLUDING_HISTORY.

THE ORIGINAL REASONING IS KEPT BECAUSE IT IS STILL TRUE, not because it won: hiding settled
cards deletes the losses that prompted the suppression, which makes the VISIBLE record better
than reality -- the retroactive-filtering bias CLAUDE.md records from the Hearts v Dundee Utd
incident, where a guard tightened after the fact erased a published, winning pick.

What makes it acceptable here is the one thing that has not changed: /history and
/history/summary read `predictions` directly and are untouched by either set, so every MLS
result keeps counting toward the accuracy this project reports. The cards are hidden; the
scoreboard is not edited. If that ever stops being true, this decision needs revisiting.

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
SUPPRESSED_LEAGUES: frozenset[str] = frozenset()

# The SAME suppression, but hiding SETTLED fixtures too, so the league vanishes from the feed
# entirely including its history.
#
# EMPTY BY DEFAULT, AND PREFER SUPPRESSED_LEAGUES ABOVE. Hiding a league's settled cards
# deletes its losses from view, which makes the visible track record better than reality --
# the same retroactive-filtering bias that once erased a published, winning Hearts v Dundee Utd
# pick when a completeness floor was raised after the fact. /history and /history/summary are
# NOT affected by either set, so the numbers this project reports stay honest either way; what
# this changes is only whether a user can still see the cards.
#
# Legitimate uses: a league ingested by mistake, a competition with corrupt fixture data, or a
# league being retired outright. "Its recent results look bad" is NOT one -- use
# SUPPRESSED_LEAGUES for that.
#
# MLS MOVED HERE 2026-08-23, on a direct and repeated instruction ("Hide them including
# history") after settled MLS cards kept appearing in the feed -- which was the upcoming-only
# suppression working exactly as designed, and reported as it not working.
#
# The bias warning above still applies and is not waived: hiding settled cards removes losing
# results from view. What makes it acceptable rather than a quiet improvement of the record is
# that /history and /history/summary read `predictions` directly and are untouched by either
# set, so every MLS loss keeps counting toward the accuracy this project publishes. The cards
# are hidden; the scoreboard is not edited.
SUPPRESSED_LEAGUES_INCLUDING_HISTORY: frozenset[str] = frozenset(
    {
        "mls",
    }
)

# One market withheld for one league: {league_slug: {market, ...}}. Markets are the raw values
# _all_market_candidates emits -- "h2h", "double_chance", "goals_total", "corners_total".
#
# THIS IS THE OPERATOR OVERRIDE, AND IT IS DELIBERATELY SEPARATE FROM THE MEASURED GATES.
# goals_availability.py and corners_availability.py encode what MEASUREMENT concluded about a
# market's skill or settlement supply, each with the numbers that justified it and a stated
# condition for lifting. This dict encodes what a PERSON decided, for any market including the
# two those files do not cover (h2h and double_chance have no measured gate at all).
#
# Keeping them apart matters: a decision recorded here cannot be mistaken later for a
# measurement, and re-running a measurement cannot silently overwrite a human's call.
SUPPRESSED_MARKETS_BY_LEAGUE: dict[str, frozenset[str]] = {}


def offers_picks(league_slug: str | None) -> bool:
    """False only for a league explicitly suppressed above (either set).

    An unknown or missing league returns True: these are explicit, temporary denylists of
    leagues someone decided to withhold, never a default. That is the opposite polarity to
    offers_goals (which is earned by measurement) and matches offers_corners' benefit of the
    doubt -- a league nobody has ruled on keeps working."""
    if not league_slug:
        return True
    return (
        league_slug not in SUPPRESSED_LEAGUES
        and league_slug not in SUPPRESSED_LEAGUES_INCLUDING_HISTORY
    )


def suppressed_markets_for(league_slug: str | None) -> frozenset[str]:
    """Markets withheld from the headline pick for this league by operator decision.

    Empty for a league nobody has ruled on. A suppressed market still appears in
    all_market_picks and in the fixture detail's Other Markets, and an explicit
    ?market=<name> request is still honoured -- exactly like NO_DEMONSTRATED_SIGNAL_MARKETS.
    Withholding a market from the DEFAULT ranking is not the same as deleting it."""
    if not league_slug:
        return frozenset()
    return SUPPRESSED_MARKETS_BY_LEAGUE.get(league_slug, frozenset())
