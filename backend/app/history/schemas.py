import uuid
from datetime import date, datetime

from pydantic import BaseModel


class HistoryEntry(BaseModel):
    """One settled fixture the model made a real call on, with how that call turned out.

    TWO VERDICTS, because they answer different questions and only one of them is the product.

    `was_correct` scores the 1X2 market — the model's argmax of home/draw/away. The original
    reasoning for scoring only this still holds and is worth keeping: best_pick depends on which
    odds happened to be ingested, so its accuracy is partly a function of odds coverage, while
    1X2 exists for every prediction and stays comparable across sports and across time.

    `pick_was_correct` scores the pick that was actually ON THE CARD, across all four markets.
    What the original reasoning missed is that a track record of something users never see
    cannot build trust in what they do see, and the two genuinely disagree: Hearts v Dundee Utd
    (2026-08-09) showed UNDER 10.5 CORNERS and won, while its 1X2 call lost. Measured over the
    settled pre-match population, football's card picks are 43 double chance + 19 corners and
    ZERO h2h — so `was_correct` describes a market football cards never show.

    Both are returned rather than one replacing the other. Silently changing what `was_correct`
    means would hand every existing consumer a different number with no signal that it moved."""

    fixture_id: uuid.UUID
    sport_slug: str
    league_slug: str
    home_team: str
    away_team: str
    model_version: str
    # The model's own confidence in the outcome it picked (not necessarily the home side).
    predicted_probability: float
    # UNVALIDATED — do not render this to users.
    #
    # Measured on settled pre-match predictions (2026-08-10): HIGH claimed 74.1% and delivered
    # 60.9% (n=69), while MEDIUM claimed 57.8% and delivered 68.5% (n=89). The label pointed at
    # the weaker set, so it was removed from the app and from the push gate. It is still
    # computed and returned as MEASUREMENT DATA, so the thresholds can be recalibrated against
    # outcomes once the sample supports it — not as advice.
    confidence_tier: str
    predicted_outcome: str  # "home" | "draw" | "away"
    result: str  # the real settled MatchResult
    was_correct: bool  # the 1X2 call — see the class docstring
    settled_at: datetime

    # --- the pick that was actually on the card ---------------------------------------------
    # All null when the feed's own guards left the fixture with no pick at all (the fixture is
    # still listed, because "we showed nothing here" is itself part of the record).
    pick_market: str | None = None  # "h2h" | "double_chance" | "goals_total" | "corners_total"
    pick_selection: str | None = None
    pick_line: float | None = None
    pick_probability: float | None = None
    pick_odds: float | None = None
    # None means UNVERIFIABLE, never "wrong" — a corners pick on a fixture whose real corner
    # counts were never captured, or a voided match. See app/predictions/grading.py.
    pick_was_correct: bool | None = None


class HistorySummary(BaseModel):
    """Aggregate model performance over settled fixtures — the real "how good is this thing"
    number, computed from actual results rather than from training-time metrics.

    Distinct from GET /stats/model, which reports what a model scored on its own held-out TEST
    SET at training time. This is the live equivalent: how the currently-serving predictions
    have actually fared on real fixtures since. The two can legitimately diverge, and that
    divergence is exactly what makes this worth surfacing."""

    sport_slug: str
    settled_fixtures: int
    correct: int
    accuracy: float
    # Fixtures excluded from the numbers above because there was nothing to score: a tennis
    # retirement or walkover, where bookmakers generally void bets. Counted and reported rather
    # than silently dropped, so the denominator is never quietly wrong.
    voided: int

    # --- honesty fields (docs/history-metrics-spec.md §6) -----------------------------------
    # An accuracy without these reads as a track record when it is often noise. Measured
    # 2026-08-10: at n=139 football, only a 10.5pp effect is detectable at 80% power, while the
    # edge we believe we have is 4.1pp. Reporting 56% with no sample context invites exactly
    # the conclusion the number cannot support.
    kind: str
    # 95% Wilson interval on `accuracy`. Wilson rather than normal-approximation because it
    # behaves at small n and near 0/1, which is precisely where this endpoint currently lives.
    accuracy_ci_low: float
    accuracy_ci_high: float
    # Smallest true effect this sample could detect at 80% power. When it exceeds the effect
    # being claimed, the number cannot settle the question either way.
    detectable_effect: float
    # False when settled_fixtures < MIN_REPORTABLE_N. Consumers should render "not enough data"
    # rather than a percentage. Safe to DISPLAY.
    sufficient_sample: bool
    # Safe to ACT on: the sample can detect the pre-registered edge threshold (spec §9.2).
    # A metric is routinely reportable AND inconclusive at the same time — that is the normal
    # state today, and the UI must be able to say so rather than implying a verdict.
    conclusive: bool
    # Predictions excluded because their provenance could not be established — see
    # PredictionKind.UNKNOWN. Reported rather than hidden so the denominator stays auditable.
    excluded_unknown_provenance: int

    # --- the same population, scored on the pick that was actually SHOWN ---------------------
    # `accuracy` above is the 1X2 call. This is the product's own promise, and the two are not
    # interchangeable: football card picks are 43 double chance + 19 corners and ZERO h2h, so
    # the 1X2 number describes a market football cards never show.
    #
    # READ THESE TOGETHER WITH card_pick_baseline. Double chance and corners carry much higher
    # base rates than 1X2, so a higher card accuracy is mostly the market being easier, not the
    # model being better. Measured 2026-08-13: football 0.3651 on 1X2 against 0.7258 on card
    # picks, where the no-skill baseline is 0.6391 — an +8.67pp gap, not a +36pp one.
    card_pick_graded: int
    card_pick_correct: int
    card_pick_accuracy: float
    card_pick_ci_low: float
    card_pick_ci_high: float
    # Mean of each shown pick's own measured base rate (MARKET_BASE_RATES). None where the sport
    # has none — tennis abstains deliberately, since its "home" is a player-id tiebreak rather
    # than a venue, so borrowing football's rate would be meaningless.
    card_pick_baseline: float | None
    # Settled fixtures whose card pick could not be graded (corners with no captured count), and
    # those the feed's guards left with no pick at all. Counted, never folded into the losses.
    card_pick_ungradable: int
    card_pick_absent: int


class HistoryQuery(BaseModel):
    sport_slug: str | None = None
    league_slug: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    result: str | None = None


class ModelStats(BaseModel):
    sport_slug: str
    model_version: str
    accuracy: float | None = None
    rps_score: float | None = None
    # Small-sample, directional flat-stake backtest metric (see ml/training/train_nba.py) —
    # null for any model version trained before this column existed, or where no bookmaker
    # odds existed for the test-set games.
    roi_simulation: float | None = None
    trained_at: datetime
