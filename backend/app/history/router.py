from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.history.schemas import HistoryEntry, ModelStats
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

router = APIRouter(tags=["history"])

_optional_bearer = HTTPBearer(auto_error=False)


@router.get("/history", response_model=list[HistoryEntry])
async def get_history(
    sport_slug: str | None = None,
    league_slug: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
):
    """Without auth: global prediction history + accuracy stats. With auth: also the caller's
    personal saved picks history (TDD §4.1 HIST-01). Needs settled outcomes to exist first —
    not implemented until run_predictions/ingest have produced real data (plan §4)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="History aggregation not implemented yet — no settled outcomes exist.",
    )


@router.get("/stats/model", response_model=list[ModelStats])
async def get_model_stats(sport_slug: str | None = None, db: AsyncSession = Depends(get_db)):
    """Model performance summary per sport (TDD §4.1) — the currently *active* model per
    sport, since that's what's actually serving predictions right now. Model promotion
    (models_registry.is_active flip) is a DB update per TDD §3.1, so this always reflects
    whichever version is live without a code change."""
    stmt = (
        select(ModelRegistry, Sport.slug)
        .join(Sport, Sport.id == ModelRegistry.sport_id)
        .where(ModelRegistry.is_active.is_(True))
    )
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)

    rows = (await db.execute(stmt)).all()
    return [
        ModelStats(
            sport_slug=slug,
            model_version=model.version,
            accuracy=model.accuracy,
            rps_score=model.rps_score,
            roi_simulation=model.roi_simulation,
            trained_at=model.trained_at,
        )
        for model, slug in rows
    ]
