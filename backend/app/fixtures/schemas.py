import uuid
from datetime import datetime

from pydantic import BaseModel


class TeamFeaturesResponse(BaseModel):
    elo_rating: float | None = None
    attack_str: float | None = None
    defence_str: float | None = None
    form_pts_5: float | None = None
    xg_for_5: float | None = None
    xg_against_5: float | None = None
    days_since_last_match: int | None = None
    home_win_rate: float | None = None
    away_win_rate: float | None = None
    # Recent results as W/D/L, MOST RECENT FIRST -- "WWLDW" reads left to right as newest to
    # oldest. Variable length: a side five matches into a season has five characters, one that
    # has played twice has two, and a sport or league we hold no settled results for has None
    # rather than an empty string, so the client can tell "no history" from "no results yet".
    recent_form: str | None = None


class DriverRow(BaseModel):
    """One factor row in the expanded panel (design spec §3.2, §5.4).

    `weight` is a RELATIVE share of the movement, never a probability, and the two must not be
    confused in the UI. For football these contributions come from a market-blind model that
    never saw a price, so they decompose a DIFFERENT model from the one that produced the
    probability on the card and cannot sum to it -- which is why the panel's eyebrow says what
    the DATA says rather than "why the model called it".
    """

    label: str
    #: Positive means this factor supports the pick being shown, whatever the market or side.
    contribution: float
    #: |contribution| as a share of all rows' |contribution|, in 0..1.
    weight: float


class BestPick(BaseModel):
    """The model's single favoured outcome for this fixture, with the best available odds for
    it — drawn from ACROSS every market this product supports (h2h, double chance, Over/Under
    goals, Over/Under corners), not just home/draw/away, per the user's explicit ask that the
    Home/Picks feed surface "the best odds with the highest probability of winning" regardless
    of which market that happens to live in. See app/fixtures/router.py:_pick_best. odds is
    null when a prediction exists but no odds have been ingested yet for ANY market (real for
    Brasileirão fixtures before TheRundown/API-Football odds land, or for a market a league's
    odds provider simply doesn't cover — see CLAUDE.md's per-league odds-coverage notes)."""

    # "home"|"draw"|"away" (h2h); "1X"|"X2" (double_chance); "over"|"under" (totals)
    selection: str
    probability: float
    odds: float | None = None
    market: str = "h2h"  # "h2h" | "double_chance" | "goals_total" | "corners_total"
    line: float | None = None  # goals_total/corners_total only
    # Fraction of the model's feature vector that had a real value when this prediction was
    # made (0.0-1.0); null for predictions made before this was recorded. Lets the client
    # distinguish a well-informed probability from one the model effectively fell back to the
    # base rate for -- see Prediction.feature_completeness.
    feature_completeness: float | None = None
    # WHEN this pick's underlying prediction was generated, and what it superseded.
    #
    # best_pick is recomputed on EVERY request against whatever prediction and odds exist at
    # that moment, and nothing about it is stored -- so a card genuinely can read differently
    # from one visit to the next. Reported twice by a user in one day (a WNBA pick moving
    # 59% -> 66% overnight; a La Liga card showing over-1.5 one day and a double chance the
    # next) and experienced as the app changing its mind behind their back.
    #
    # The churn itself is mostly legitimate -- odds landing is new information, and the market
    # feeds the model -- so the defect is not that the number moves, it is that the card
    # presents a moving estimate as though it were timeless. These two fields let the client
    # say "as of 09:40, up from 59%" instead, which is honest and still fresh.
    #
    # previous_probability is the last MATERIALLY different value (see
    # MIN_REPORTABLE_PROBABILITY_MOVE) from an earlier pre-kickoff prediction for the same
    # fixture; null when the pick has not meaningfully moved, which is the common case.
    as_of: datetime | None = None
    previous_probability: float | None = None
    # The three biggest real-world factors behind this pick, or null.
    #
    # Null is an ORDINARY outcome, not an error, and the client must render nothing rather than
    # a placeholder: predictions written before attribution existed carry none (contributions
    # cannot be reconstructed after the fact), a draw pick has no expressible direction, and the
    # divergence guard suppresses the panel outright when the market-blind model favours a
    # different outcome from the one on the card.
    # How this pick is doing RIGHT NOW, for a fixture in play: "on_track" | "at_risk" | "lost",
    # or null when it cannot be judged (design spec §4.1).
    #
    # NULL IS MEANINGFUL AND MUST RENDER AS NOTHING, not as a neutral badge. Corners have no
    # in-play state at all (counts are written once, at settlement) and basketball reports no
    # clock, so for those the honest answer is silence — an absent tag reads as "not
    # applicable", a grey one reads as "we checked and it is fine".
    live_status: str | None = None
    drivers: list[DriverRow] | None = None
    # True when `drivers` came from a market-blind variant rather than the serving model, so the
    # UI can label them as what the DATA says instead of implying they add up to `probability`.
    drivers_are_market_blind: bool = False


class LiveStateResponse(BaseModel):
    home_score: int
    away_score: int
    match_minute: int | None = None
    period: str | None = None
    status: str
    # Football only — real corner-kick counts, fetched once at settlement time (see
    # app/workers/ingest_fixtures.py:_maybe_settle_outcome). Null for NBA and for any fixture
    # settled before this existed — used to give the Over/Under corners market a real
    # win/loss verdict instead of staying permanently unverifiable.
    home_corners: int | None = None
    away_corners: int | None = None
    # Null for a normally-played-out result; "retired"/"walkover" for one that ended without
    # being played out (tennis in practice). The mobile feed shows a neutral "RET" badge and
    # withholds the win/loss verdict for these rather than counting them against the model,
    # since bookmakers generally void bets on a retirement.
    result_type: str | None = None
    last_updated_utc: datetime


class FixtureSummary(BaseModel):
    id: uuid.UUID
    sport_slug: str
    league_slug: str
    league_name: str
    league_country: str | None = None
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str
    season: str
    # Tennis only — a tour (ATP/WTA) is one league, so the feed groups by TOURNAMENT instead,
    # giving users something they can actually find in a betting app. Null for football/NBA,
    # where league_name/league_country already serve that role. `tournament_location` is a
    # CITY, not a country (the provider exposes no country field) — the client maps it.
    tournament_name: str | None = None
    tournament_surface: str | None = None
    tournament_location: str | None = None
    # True when kickoff_utc was inferred, not reported by the provider - the client shows
    # "Time TBC" rather than asserting a precise time we don't have (tennis in practice).
    kickoff_is_estimated: bool = False
    best_pick: BestPick | None = None
    # Every real candidate across all four markets (h2h, double chance, goals/corners O/U) —
    # NOT just best_pick's single winner — so a past/completed fixture can show a full
    # win/loss breakdown across every market for evaluating model performance, per explicit
    # user request ("I need all markets predicted in the past to still be shown... Over and
    # Under, double chances, corners. Everything should be shown"). Always the FULL,
    # market-param-independent list (unlike best_pick, which respects GET /fixtures's own
    # market/line restriction) — see app/fixtures/router.py:_bulk_best_picks.
    all_market_picks: list[BestPick] = []
    # Present for both in-progress and completed fixtures (see
    # app/workers/ingest_fixtures.py:_upsert_live_state) — null for a fixture that hasn't
    # started. Surfaced in the list view too, not just fixture detail, so the Home feed can
    # show a score inline without a per-fixture call.
    live_state: LiveStateResponse | None = None


class OddsLineResponse(BaseModel):
    bookmaker: str
    market: str
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    updated_at: datetime


class TotalsProbability(BaseModel):
    """One Over/Under line's calibrated probability pair — see app/models_ml/markets.py.
    under_prob/over_prob are both null when the underlying expected total (xg_home+xg_away
    for goals, corners_xg_home+corners_xg_away for corners) isn't available yet (e.g. an
    artefact trained before the corners regressors existed)."""

    line: float
    under_prob: float | None = None
    over_prob: float | None = None


class ExtraMarketsResponse(BaseModel):
    """Football-only prediction markets beyond the core home/draw/away 1X2 — double chance and
    Over/Under goals/corners (see app/models_ml/markets.py). None/empty fields mean the
    underlying inputs aren't available (e.g. NBA has no draw_prob, so double chance is null;
    an older prediction has no corners_xg_*, so corners_totals is empty), never a fabricated
    50/50 split."""

    double_chance_home_or_draw_prob: float | None = None
    double_chance_away_or_draw_prob: float | None = None
    goals_totals: list[TotalsProbability] = []
    corners_totals: list[TotalsProbability] = []


class PredictionResponse(BaseModel):
    model_version: str
    home_prob: float
    draw_prob: float | None = None
    away_prob: float
    # UNVALIDATED — do not render this to users. Measured 2026-08-10: HIGH claimed 74.1% and
    # delivered 60.9% (n=69) while MEDIUM claimed 57.8% and delivered 68.5% (n=89), so the
    # label pointed at the weaker set and was removed from the app and the push gate. Still
    # returned as MEASUREMENT DATA so the thresholds can be recalibrated later — not as advice.
    confidence_tier: str
    expected_value: float | None = None
    extra_markets: ExtraMarketsResponse | None = None


class ComparisonStat(BaseModel):
    """One labelled comparison row: the same measure for each side.

    Named for the SHAPE rather than for head-to-head, because two panels now use it — the H2H
    averages over past meetings, and the stats of a single completed match. The JSON is
    identical either way; only the meaning of the number changes with the panel it sits in.

    A LIST rather than named fields, because the three sports genuinely do not share a stat
    vocabulary and their providers do not expose the same depth:

        football   goals, corners, shots, shots on goal, possession   (API-Football)
        tennis     aces, double faults, 1st serve %, break points,
                   total points won                                   (BallDontLie, GOAT tier)
        NBA/WNBA   points scored, points allowed                      (scores ONLY -- see below)

    Naming them all in one model would mean a football row carrying eleven permanently-null
    tennis fields, and every new sport editing this schema plus the mobile component. The
    client renders whatever rows it is given.
    """

    label: str
    home: float | None = None
    away: float | None = None
    # Appended verbatim when rendering: "%" for percentages, "" for counts. The unit belongs
    # with the value that has it, rather than being re-derived from the label on the client.
    suffix: str = ""


class HeadToHeadResponse(BaseModel):
    """Real head-to-head history between this fixture's two teams — replaces the raw
    bookmaker-odds table on the fixture detail screen per direct user request ("Users don't
    find the Odds section useful... replaced with H2H statistics"), showing averages over the
    last real meetings rather than a list of individual scores ("important stats that will give
    users confidence on the prediction").

    home_wins/draws/away_wins and every stat are relative to THIS fixture's home/away
    assignment, not each past meeting's own — a team's H2H record should not flip depending on
    which side it happened to be on in a past meeting.

    Available for football, tennis and basketball. Null — never a fabricated empty record —
    when the two have genuinely never met, or a team could not be resolved.

    DEPTH VARIES BY PROVIDER, and NBA/WNBA is the thin one for a reason worth stating:
    BallDontLie's /stats returns 401 on this plan, so no box score exists at any price we hold
    and the only real per-meeting numbers are the final scores. Two rows is what the data
    supports; inventing more would mean fabricating them."""

    meetings_count: int
    home_wins: int
    draws: int
    away_wins: int
    stats: list[ComparisonStat] = []


class FixtureDetail(FixtureSummary):
    odds: list[OddsLineResponse] = []
    prediction: PredictionResponse | None = None
    home_team_form: TeamFeaturesResponse | None = None
    away_team_form: TeamFeaturesResponse | None = None
    head_to_head: HeadToHeadResponse | None = None
    # What actually happened in THIS match, for a fixture that has been played — so the
    # prediction above can be read against the result rather than taken on trust. The card
    # already shows a tick or a cross; this is the evidence behind it (a corners pick that
    # settled at 9 is a very different read from one that settled at 14).
    #
    # EMPTY, never fabricated, until there is something real to show: the fixture must be
    # COMPLETED, and each row is dropped when neither side has a value. Basketball is empty by
    # nature -- BallDontLie's /stats is 401 on this plan, so the final score is the only real
    # per-match number and it is already displayed above.
    match_stats: list[ComparisonStat] = []
