import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, TeamFeatures
from app.models_ml.runner import ModelRunner
from app.predictions.models import Prediction, PredictionKind
from app.predictions.service import confidence_tier_for_probability, feature_completeness
from app.sports.models import Sport
from app.workers.celery import celery_app, run_task
from app.workers.notify_users import notify_new_pick

_model_runner = ModelRunner()


# The signals that make a football vector a statement about THIS fixture rather than the
# model's prior. All None on both sides means the live path had nothing — the season-opener
# state — and is the trigger for the game-log fallback below. Deliberately narrow: one side
# populated (an established club hosting a promoted one) is a REAL partial vector, exactly
# what training saw for promoted sides, and must not trigger a rebuild.
_FOOTBALL_CORE_SIGNALS = (
    "attack_str_home",
    "attack_str_away",
    "form_pts_home",
    "form_pts_away",
    "elo_diff",
)


def _football_vector_is_empty(features: dict) -> bool:
    return all(features.get(name) is None for name in _FOOTBALL_CORE_SIGNALS)


async def _assemble_features(
    db, sport_slug: str, fixture: Fixture, home_features, away_features
) -> dict:
    """Dispatches to the sport-specific feature-assembly function, mirroring how
    AdapterFactory/ModelRunner resolve sport-specific implementations by slug."""
    if sport_slug == "nba":
        from app.models_ml.nba_features import assemble_from_live_db

        return await assemble_from_live_db(db, fixture, home_features, away_features)
    if sport_slug == "football":
        from app.models_ml.football_features import assemble_from_live_db

        features = await assemble_from_live_db(db, fixture, home_features, away_features)
        if _football_vector_is_empty(features):
            # Season opening: /teams/statistics has nothing yet, so the live vector is empty
            # and the prediction would land on the model's flat prior -- measured on the EPL
            # 2026-27 opening round, all ten fixtures at H0.684 D0.232 A0.083, every pick
            # hidden by the completeness floor. The league's own multi-season game log ships
            # in the image and training rolls windows ACROSS season boundaries, so last
            # season's tail is what the model expects to see here, not an empty vector. See
            # assemble_upcoming_from_game_log for the leakage argument and the promoted-team
            # honesty rules.
            from app.workers.backfill_predictions import assemble_upcoming_from_game_log

            fallback = await assemble_upcoming_from_game_log(db, fixture)
            if fallback is not None and not _football_vector_is_empty(fallback):
                return fallback
        return features
    if sport_slug == "tennis":
        from app.models_ml.tennis_features import assemble_from_live_db

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
            feature_completeness=feature_completeness(features),
            # Set explicitly, never inferred. This is the live pre-kickoff path, so its
            # rows are the only ones that evidence forecasting skill — see PredictionKind.
            kind=PredictionKind.PRE_MATCH,
            xg_home=result.xg_home,
            xg_away=result.xg_away,
            corners_xg_home=result.corners_xg_home,
            corners_xg_away=result.corners_xg_away,
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
    run_task(_run_predictions(uuid.UUID(fixture_id)))
