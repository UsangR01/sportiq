import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models_ml.base import BaseModel
from app.models_ml.football import FootballModel
from app.models_ml.nba import NBAModel
from app.models_ml.tennis import TennisModel
from app.predictions.models import ModelRegistry
from app.sports.models import Sport

# Keyed by sport slug, not models_registry.model_type — the latter is a free-form version
# identifier (e.g. "nba_xgb_v1" per TDD §6.1's example insert), not a class name.
_MODEL_CLASSES: dict[str, type[BaseModel]] = {
    "football": FootballModel,
    "nba": NBAModel,
    "tennis": TennisModel,
}


class ModelRunner:
    """Loads the active model per sport and caches it in memory for the process lifetime
    (TDD §3.1). Model promotion (models_registry.is_active flip) takes effect on next worker
    restart — no code deploy required."""

    def __init__(self) -> None:
        self._cache: dict[uuid.UUID, BaseModel] = {}

    async def get_model(self, db: AsyncSession, sport_id: uuid.UUID) -> BaseModel:
        if sport_id in self._cache:
            return self._cache[sport_id]

        sport = (await db.execute(select(Sport).where(Sport.id == sport_id))).scalar_one_or_none()
        if sport is None:
            raise ValueError(f"No sport registered with id={sport_id}")

        model_cls = _MODEL_CLASSES.get(sport.slug)
        if model_cls is None:
            raise ValueError(f"No model class registered for sport slug={sport.slug!r}")

        registry_row = (
            await db.execute(
                select(ModelRegistry).where(
                    ModelRegistry.sport_id == sport_id, ModelRegistry.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if registry_row is None:
            raise ValueError(f"No active model registered for sport_id={sport_id}")

        model = model_cls(artefact_path=registry_row.artefact_path, version=registry_row.version)
        self._cache[sport_id] = model
        return model
