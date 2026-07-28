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
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
