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
    """Loads the active model per sport, cached BY VERSION rather than for the process
    lifetime.

    The original cache held whatever resolution the process saw FIRST, forever, documented as
    "promotion takes effect on next worker restart". Measured cost on 2026-08-18: a freshly
    deployed worker resolved football's model during the minutes before seed_model_registry
    had run on that database, cached a registry row pointing at an artefact the new image no
    longer contained, and then failed EVERY football prediction with FileNotFoundError for
    the rest of its life — including long after the registry itself was fixed. Restarting the
    worker was the only cure, which is exactly the stale-state trap this project keeps
    hitting in new costumes.

    Now every call re-reads the active registry row (one indexed query — predictions each
    make live H2H HTTP calls, so this is noise) and reuses the loaded artefact only while the
    active version still matches. Promotion therefore takes effect on the NEXT PREDICTION,
    which is what TDD §3.1's "promotion is a DB update, not a redeploy" always claimed."""

    def __init__(self) -> None:
        self._cache: dict[uuid.UUID, BaseModel] = {}

    async def get_model(self, db: AsyncSession, sport_id: uuid.UUID) -> BaseModel:
        registry_row = (
            await db.execute(
                select(ModelRegistry).where(
                    ModelRegistry.sport_id == sport_id, ModelRegistry.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if registry_row is None:
            raise ValueError(f"No active model registered for sport_id={sport_id}")

        cached = self._cache.get(sport_id)
        if cached is not None and cached.version == registry_row.version:
            return cached

        sport = (await db.execute(select(Sport).where(Sport.id == sport_id))).scalar_one_or_none()
        if sport is None:
            raise ValueError(f"No sport registered with id={sport_id}")

        model_cls = _MODEL_CLASSES.get(sport.slug)
        if model_cls is None:
            raise ValueError(f"No model class registered for sport slug={sport.slug!r}")

        model = model_cls(artefact_path=registry_row.artefact_path, version=registry_row.version)
        self._cache[sport_id] = model
        return model
