import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
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
