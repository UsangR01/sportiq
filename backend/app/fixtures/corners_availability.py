"""Which leagues can settle a corners pick AT FULL TIME.

THE PROBLEM THIS SOLVES. A corners pick can only show a tick or a cross once we hold the corner
counts. API-Football is the only source that publishes them promptly; TheStatsAPI covers far
more leagues but lags about twelve hours -- measured 2026-08-14: HTTP 404 at 3.4h past kickoff,
still nothing at 7.9h, present at 12.9h. So in a league API-Football does not cover, a corners
pick is STRUCTURALLY ungradable for half a day, no matter how often the fill retries. The card
sits grey through exactly the window in which users open the app to see whether they won.

Retrying faster does not fix that, and telling the user to wait is not a product. The answer is
the one other prediction apps use: do not offer a market you cannot settle.

MEASURED PER LEAGUE, NOT ASSUMED -- API-Football asked directly for six recent settled fixtures
in each league, 2026-08-16, because our own stored counts can no longer say which source filled
them:

    veikkausliiga        0/6    0%     no prompt corners at all
    czech_first          2/6   33%
    ekstraklasa          3/6   50%
    liga_i               4/6   67%
    brasileirao          5/6   83%     kept
    every other league   6/6  100%

Re-derive with scripts/measure_prompt_corner_coverage.py rather than editing this by hand.

=== RE-ENABLING, WHICH IS THE EXPECTED OUTCOME ===

This is a limitation of the current data supply, not a judgement about the corners model, which
measures a real if modest signal (r=+0.288 against actual totals, against goals' +0.049). The
day a provider with prompt corners across every league is in place:

    1. run scripts/measure_prompt_corner_coverage.py against the new supply
    2. if every league clears MIN_PROMPT_CORNER_COVERAGE, set the set below to frozenset()

That single edit restores corners everywhere. Nothing else in the pick pipeline needs touching,
and no migration or retrain is involved.
"""

# A league must settle at least this share of its corners promptly to be offered the market.
# 0.80 rather than 1.00 because a provider missing the occasional fixture is normal and the
# 15-minute second-source fill covers those within the day; what this excludes is leagues where
# a MAJORITY of cards would sit grey overnight.
MIN_PROMPT_CORNER_COVERAGE = 0.80

# Set to frozenset() to offer corners in every league again — see the header.
# RE-MEASURED 2026-08-19 (scripts/measure_prompt_corner_coverage.py):
#   czech_first and ekstraklasa now measure 6/6 prompt — API-Football's coverage improved, so
#   both are re-admitted; the measurement, not the original snapshot, owns this set.
#   uel joins instead at 1/6: Europa League QUALIFIERS involve clubs whose statistics
#   API-Football does not carry promptly. Worth re-measuring once the league phase proper
#   starts (late September) — the main-draw clubs are exactly the ones with full coverage.
#   ucl 3/3 and uecl 6/6 both passed and are offered from day one.
LEAGUES_WITHOUT_PROMPT_CORNERS = frozenset(
    {
        "veikkausliiga",
        "liga_i",
        "uel",
    }
)

# A SECOND, SEPARATE reason to withhold corners: measured no skill. Scoped to the Conference
# League alone by explicit product decision (2026-08-19), after its qualifier corners picks
# went 5/13 on over-9.5 claims of ~65%. Two measurements behind it:
#   - live: every UECL fixture was predicted a near-constant 10.7-11.5 total corners (cup
#     clubs have no fixture history with us, so the rolling corners features are None and the
#     regressors fall back toward the pooled mean) in a competition averaging ~9.4; settled
#     UECL mean 9.29, over-9.5 rate 0.43;
#   - held-out, features populated: uecl corners r=+0.049 CI [-0.119, +0.214] on the served
#     model's test parquet — no signal even in training conditions.
# Lift condition is SKILL, not history accumulating: re-measure on a future served model's
# parquet and remove only if the interval clears zero with a meaningful r. The wider per-league
# corners measurement (2026-08-19: pooled r=+0.093, zero of 22 leagues passing the goals bar)
# is recorded in commit c7e7c2d if this ever needs to widen.
LEAGUES_WITHOUT_DEMONSTRATED_CORNERS_SIGNAL = frozenset({"uecl"})


def offers_corners(league_slug: str | None) -> bool:
    """False for a league measured as unable to settle corners at full time (supply), or
    measured to have no corners skill (currently the Conference League alone) — two sets, two
    lift conditions, see each above.

    An unknown league returns True: a newly-added competition has no measurement yet, and
    silently withholding a market from it would be a worse default than showing it and finding
    out. The measurement script is what moves a league into the supply set."""
    if not league_slug:
        return True
    if league_slug in LEAGUES_WITHOUT_DEMONSTRATED_CORNERS_SIGNAL:
        return False
    return league_slug not in LEAGUES_WITHOUT_PROMPT_CORNERS


# A THIRD reason, and the first that is about a SIDE OF A LINE rather than a league: the model
# is calibrated on average and overconfident exactly where picks are drawn from.
#
# Measured 2026-08-24 on the served model's own test parquet (5,970 fixtures with real corner
# counts), P(over 9.5) predicted vs observed:
#
#     all fixtures        claimed 0.532  actual 0.512  gap +0.02
#     predicted >= 0.60   claimed 0.634  actual 0.566  gap +0.07   n=1050
#     predicted >= 0.65   claimed 0.689  actual 0.591  gap +0.10   n= 225
#     predicted >= 0.70   claimed 0.736  actual 0.600  gap +0.14   n=  65
#
# THE GAP GROWS WITH CONFIDENCE. A pick must clear ~0.5955 to pass MIN_EDGE_OVER_BASE_RATE, so
# every over-9.5 pick that reaches a card is drawn from precisely the band where the model is
# worst. The headline "2-point gap" over all fixtures is real and irrelevant: it averages over
# thousands of fixtures that could never be picked.
#
# Confirmed live rather than inferred: 69 settled over-9.5 card picks hit 52% against a claimed
# 67%, a 15-point overstatement whose claim sits outside the 95% interval's upper bound of 64%.
# 52% is also the pooled base rate, so those picks were adding nothing.
#
# UNDER 10.5 IS DELIBERATELY NOT BARRED and is the reason this is a side-of-a-line rule rather
# than "corners are broken": 18 settled picks, 72% against a claimed 70%. Its picks sit in a
# better-behaved part of the same distribution.
#
# THIS IS A HOLDING MEASURE. The real fix is calibrating the derived over/under probability --
# the artefact calibrates the corner RATE and never the probability derived from it -- after
# which MIN_EDGE_OVER_BASE_RATE would drop these on its own and this set can be emptied. Lift
# it when a served model shows the >=0.65 band inside ~3 points.
OVERSTATED_CORNER_SELECTIONS: frozenset[tuple[str, float]] = frozenset({("over", 9.5)})


def corner_selection_is_publishable(selection: str, line: float | None) -> bool:
    """False for a corners side measured to overstate itself where picks are actually drawn.

    Barred from WINNING the headline only. The selection still appears in all_market_picks and
    in the fixture detail, and an explicit request for it is still honoured — the same
    treatment NO_DEMONSTRATED_SIGNAL_MARKETS gives goals.
    """
    if line is None:
        return True
    return (selection, float(line)) not in OVERSTATED_CORNER_SELECTIONS
