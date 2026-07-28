from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.history.schemas import HistoryEntry, ModelStats

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
async def get_model_stats(sport_slug: str | None = None):
    """Model performance summary per sport (TDD §4.1). Needs a models_registry entry with
    real evaluation metrics — not implemented until a model has actually been trained."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Model stats not implemented yet — no trained model registered.",
    )
