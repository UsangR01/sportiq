import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, TeamFeatures
from app.models_ml.runner import ModelRunner
from app.predictions.models import Prediction
from app.predictions.service import confidence_tier_for_probability
from app.sports.models import Sport
from app.workers.celery import celery_app
from app.workers.notify_users import notify_new_pick

_model_runner = ModelRunner()


async def _assemble_features(
    db, sport_slug: str, fixture: Fixture, home_features, away_features
) -> dict:
    """Dispatches to the sport-specific feature-assembly function, mirroring how
    AdapterFactory/ModelRunner resolve sport-specific implementations by slug. Only NBA has
    one so far (app/models_ml/nba_features.py) — football has no equivalent yet since
    FootballModel itself is still unimplemented."""
    if sport_slug == "nba":
        from app.models_ml.nba_features import assemble_from_live_db

        return await assemble_from_live_db(db, fixture, home_features, away_features)
    raise NotImplementedError(f"No feature-assembly function for sport={sport_slug!r} yet")


async def _run_predictions(fixture_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        fixture = (
            await db.execute(select(Fixture).where(Fixture.id == fixture_id))
        ).scalar_one_or_none()
        if fixture is None:
            raise ValueError(f"No fixture with id={fixture_id}")

        sport = (await db.execute(select(Sport).where(Sport.id == fixture.sport_id))).scalar_one()
        model = await _model_runner.get_model(db, fixture.sport_id)

        home_features = (
            await db.execute(
                select(TeamFeatures).where(
                    TeamFeatures.fixture_id == fixture_id,
                    TeamFeatures.team_id == fixture.home_team_id,
                )
            )
        ).scalar_one_or_none()
        away_features = (
            await db.execute(
                select(TeamFeatures).where(
                    TeamFeatures.fixture_id == fixture_id,
                    TeamFeatures.team_id == fixture.away_team_id,
                )
            )
        ).scalar_one_or_none()

        features = await _assemble_features(db, sport.slug, fixture, home_features, away_features)
        result = model.predict(features)

        probability = max(result.home_prob, result.away_prob, result.draw_prob or 0.0)
        prediction = Prediction(
            fixture_id=fixture_id,
            model_version=model.version,
            home_prob=result.home_prob,
            draw_prob=result.draw_prob,
            away_prob=result.away_prob,
            confidence_tier=confidence_tier_for_probability(probability),
            created_at=datetime.now(UTC),
        )
        db.add(prediction)
        await db.commit()
        await db.refresh(prediction)

    notify_new_pick.delay(str(fixture_id), str(prediction.id))


@celery_app.task(name="app.workers.run_predictions.run_predictions")
def run_predictions(fixture_id: str) -> None:
    """Triggered by ingest events (new fixture, late high-priority injury news) rather than a
    fixed schedule — see ingest_injuries.py's re-inference trigger note (TDD §3.3)."""
    asyncio.run(_run_predictions(uuid.UUID(fixture_id)))
