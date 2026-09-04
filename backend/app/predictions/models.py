import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConfidenceTier(str, enum.Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class PredictionKind(str, enum.Enum):
    """Whether a prediction was a genuine forecast or produced after the result was known.

    Both write paths land in the same table, and until this existed nothing distinguished
    them — so any performance measurement built on `predictions` would have mixed hindsight
    with forecasting and flattered the model. That is the whole reason GET /history could not
    be trusted to say anything about real skill.

    Timestamps cannot substitute for this, which is why it is a stored column rather than a
    derived one. `created_at < kickoff_utc` looks like a safe proxy but breaks the moment a
    prediction is REGENERATED: 91 football predictions were regenerated on 2026-08-10 after a
    model change, resetting created_at to well after those fixtures had kicked off. Rows
    written before this column existed therefore include a genuinely unrecoverable group —
    see UNKNOWN.
    """

    # Made before kickoff, with no knowledge of the result. The only kind that evidences
    # forecasting skill, and the only kind /history should report by default.
    PRE_MATCH = "pre_match"
    # Produced after the fact by app/workers/backfill_predictions.py, which is deliberately
    # allowed to read real lineups and completed-match history. Legitimate for showing "what
    # the model would have said" in the feed; never evidence of skill.
    RETRODICTION = "retrodiction"
    # Pre-dates this column and cannot be classified honestly. Deliberately its own value
    # rather than being guessed into one of the above: a wrong label here silently inflates
    # every downstream number, which is worse than admitting the row is unusable.
    UNKNOWN = "unknown"


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id"), nullable=False
    )
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    home_prob: Mapped[float] = mapped_column(Float, nullable=False)
    draw_prob: Mapped[float] = mapped_column(
        Float, nullable=True
    )  # null for two-outcome sports (e.g. NBA)
    away_prob: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_tier: Mapped[ConfidenceTier] = mapped_column(
        Enum(ConfidenceTier, name="confidence_tier"), nullable=False
    )
    expected_value: Mapped[float] = mapped_column(Float, nullable=True)
    # Layer 1's own expected-goals output (football only) — previously computed then discarded;
    # persisted so Over/Under-goals probabilities can be derived at read time (see
    # app/models_ml/markets.py) without re-running inference. corners_xg_* are the new corners-
    # Poisson-regressor outputs (same idea, a different target). All four null for NBA and for
    # any football prediction made before this feature existed.
    xg_home: Mapped[float | None] = mapped_column(Float, nullable=True)
    xg_away: Mapped[float | None] = mapped_column(Float, nullable=True)
    corners_xg_home: Mapped[float | None] = mapped_column(Float, nullable=True)
    corners_xg_away: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fraction of this model's feature vector that had a real value (0.0-1.0), recorded at
    # inference time. NULL for predictions made before this existed.
    #
    # A prediction built from mostly-missing features is not wrong, but it IS far less
    # informative than one built from a full vector - and today the UI renders both with
    # identical authority. Measured example: 26% of retrodicted ATP fixtures collapsed to the
    # exact same 0.562 probability, because the players' prior-match history was largely absent
    # and the model fell back on almost no signal. Surfacing this lets the client distinguish
    # "confidently 60%" from "60% because we know nothing", instead of implying the two are
    # equally trustworthy.
    feature_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Set at write time by whichever path produced the row — never inferred. See
    # PredictionKind for why a timestamp comparison is not a valid substitute.
    kind: Mapped[PredictionKind] = mapped_column(
        Enum(PredictionKind, name="prediction_kind"),
        nullable=False,
        server_default=PredictionKind.UNKNOWN.name,
    )
    # Exact TreeSHAP contributions, computed at PREDICTION TIME by the market-blind variant
    # (app/models_ml/attribution.py). Null for every row written before this existed, and for
    # any sport with no blind artefact staged.
    #
    # STORED RATHER THAN DERIVED, AND IT CANNOT BE BACKFILLED. Contributions are a function of
    # the feature vector as it stood at the moment of inference; reconstructing that vector
    # afterwards would explain the fixture with today's form, today's Elo and today's odds
    # coverage. An old prediction therefore carries no explanation and says so, rather than
    # being handed a plausible one invented after the result was known.
    #
    # Raw per-feature values per estimator, NOT the grouped display rows: grouping is a
    # presentation choice that will change, and re-grouping stored numbers costs nothing while
    # re-running inference to change a label would cost real API calls.
    driver_contributions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelRegistry(Base):
    """Decouples model deployment from code deployment — promotion is a row update, not a
    redeploy (TDD §2.1, §3.1)."""

    __tablename__ = "models_registry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sports.id"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    artefact_path: Mapped[str] = mapped_column(String(255), nullable=False)
    rps_score: Mapped[float] = mapped_column(Float, nullable=True)
    accuracy: Mapped[float] = mapped_column(Float, nullable=True)
    # Flat-stake ROI from the training script's backtest simulation (ml/training/train_nba.py)
    # — nullable since it's a small-sample, directional metric that not every model version
    # will have a real value for (e.g. no bookmaker odds existed for the test-set games).
    roi_simulation: Mapped[float | None] = mapped_column(Float, nullable=True)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PickSnapshot(Base):
    """The pick as it was actually SHOWN, captured once per fixture before kickoff.

    Without this, product performance cannot be measured at all. best_pick is computed per
    request from a prediction plus whatever odds existed at that moment, filtered by
    MIN_EDGE_OVER_BASE_RATE, MAX_EDGE_OVER_MARKET, MIN_FEATURE_COMPLETENESS and the caller's own
    thresholds — and none of it was stored. Recomputing it after the result would measure a
    different product than users saw: odds move, and the guards themselves changed twice in the
    month before this table existed.

    So hit rate, flat-stake ROI and CLV over shown picks are only computable for fixtures
    captured from here onward. See docs/history-metrics-spec.md §2b.

    ONE ROW PER FIXTURE, enforced by a unique constraint. The task is idempotent and a re-run
    must not double-count a pick in the ROI denominator.
    """

    __tablename__ = "pick_snapshots"
    __table_args__ = (
        UniqueConstraint("fixture_id", name="uq_pick_snapshots_fixture"),
        Index("ix_pick_snapshots_captured", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False
    )
    # The prediction this pick was derived from — so a later model change cannot be mistaken
    # for the model that actually made the call.
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("predictions.id", ondelete="CASCADE"), nullable=False
    )
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)

    market: Mapped[str] = mapped_column(String(32), nullable=False)
    selection: Mapped[str] = mapped_column(String(32), nullable=False)
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    # The price at the moment of display. NULL where the market had no real price yet, which is
    # common and must stay distinguishable from "priced at zero" — a fixture with no odds can
    # still be graded for hit rate, just not for ROI or CLV.
    odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    feature_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # How long before kickoff the pick was taken. Recorded because CLV is only meaningful with
    # a real gap between this price and the closing one — see snapshot_shown_picks.
    hours_before_kickoff: Mapped[float] = mapped_column(Float, nullable=False)


class FrozenPick(Base):
    """THE CARD AS IT STOOD AT KICKOFF. Read thereafter, never recomputed.

    Reported 2026-09-04: "Why are cards number changing after games... Games should not change in
    any form after or hours before start. How will a user feel to see a prediction and stake
    based on what he sees... after the game, the card that initial pushed him to make a stake
    disappears."

    MEASURED, on cards pulled from production on 2026-08-30 at the app's own defaults -- so
    provably on screen -- re-checked five days later:

        396 displayed      320 (81%) unchanged
                            28 ( 7%) showed a DIFFERENT BET
                            48 (12%) GONE from the feed

    best_pick was computed on every request and never stored, so there was no record of what a
    user had been shown and therefore nothing to protect. Every input kept moving after the
    whistle: predictions regenerate on a 20-hour cycle for anything not COMPLETED or POSTPONED
    (which INCLUDES a live match -- 578 of 740 settled fixtures rendered a prediction created
    after their own kickoff), and the guards are evaluated at read time, so barring corners on
    2026-08-30 rewrote 17 already-published cards on its own.

    ONE ROW PER FIXTURE, written once when kickoff passes.

    A NULL market IS A RECORD, not a missing one: it means no pick was shown at kickoff, and it
    is what stops a fixture GAINING a pick later. Adding a pick nobody saw is the same defect as
    deleting one they did -- both were caught in the same week when relaxing a guard for settled
    fixtures surfaced a 1X pick that had been correctly hidden all along.

    NOT pick_snapshots, though the shape is close. That table captures 4-8h before kickoff
    specifically so CLV has a real gap against the closing price; overloading it with a
    kickoff-time capture would silently destroy that measurement.
    """

    __tablename__ = "frozen_picks"
    __table_args__ = (
        UniqueConstraint("fixture_id", name="uq_frozen_picks_fixture"),
        Index("ix_frozen_picks_frozen_at", "frozen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False
    )

    # NULL market == "no pick was on this card". See the class docstring.
    market: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selection: Mapped[str | None] = mapped_column(String(32), nullable=True)
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    # NULL means the market genuinely had no price, which must stay distinguishable from zero.
    odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    feature_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Which model made the call, and when it made it -- so a later promotion can never be
    # mistaken for the model the user actually acted on.
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prediction_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # "kickoff" for the normal path; "backfill" for rows written when this table was introduced,
    # which record TODAY's card rather than the one originally shown. Kept so a future reader
    # can tell a genuine capture from a reconstruction rather than trusting them equally.
    frozen_reason: Mapped[str] = mapped_column(String(16), nullable=False, default="kickoff")
