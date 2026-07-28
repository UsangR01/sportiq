import asyncio
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture
from app.odds.models import Odds
from app.picks.service import best_available_odds, best_outcome
from app.predictions.models import ConfidenceTier, Prediction
from app.users.models import User, UserPreference
from app.workers.celery import celery_app


async def _recipients_for_pick(db, fixture: Fixture, min_odds_met: float) -> list[User]:
    """All users whose saved min_odds preference is met for this pick's sport (TDD §5.4)."""
    rows = (
        (
            await db.execute(
                select(User)
                .join(UserPreference, UserPreference.user_id == User.id)
                .where(
                    UserPreference.default_sport_id == fixture.sport_id,
                    UserPreference.default_min_odds.is_not(None),
                    UserPreference.default_min_odds <= min_odds_met,
                    User.expo_push_token.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows


async def _send_push(expo_push_token: str, title: str, body: str, data: dict) -> None:
    """POST to https://exp.host/--/api/v2/push/send via exponent-server-sdk (TDD §5.4). Not
    yet implemented — needs a configured Expo access token and real network access."""
    raise NotImplementedError("Expo push send not yet implemented")


async def _notify_new_pick(fixture_id: uuid.UUID, prediction_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        fixture = (
            await db.execute(select(Fixture).where(Fixture.id == fixture_id))
        ).scalar_one_or_none()
        prediction = (
            await db.execute(select(Prediction).where(Prediction.id == prediction_id))
        ).scalar_one_or_none()
        if fixture is None or prediction is None:
            return

        if prediction.confidence_tier != ConfidenceTier.HIGH:
            return

        odds_rows = [
            {"home_odds": o.home_odds, "draw_odds": o.draw_odds, "away_odds": o.away_odds}
            for o in (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id)))
            .scalars()
            .all()
        ]
        best_odds = best_available_odds(odds_rows)
        outcome = best_outcome(
            prediction.home_prob,
            prediction.draw_prob,
            prediction.away_prob,
            best_odds["home"],
            best_odds["draw"],
            best_odds["away"],
        )
        if outcome is None:
            return

        recipients = await _recipients_for_pick(db, fixture, min_odds_met=outcome.odds)

        for user in recipients:
            await _send_push(
                expo_push_token=user.expo_push_token,
                title="New high-confidence pick",
                body=(
                    f"{outcome.selection} @ {outcome.odds} — "
                    f"{outcome.probability:.0%} model probability"
                ),
                data={"fixture_id": str(fixture_id)},
            )


@celery_app.task(name="app.workers.notify_users.notify_new_pick")
def notify_new_pick(fixture_id: str, prediction_id: str) -> None:
    """Queued by run_predictions when a new HIGH-confidence prediction is generated (TDD
    §5.4). Deep link on tap: sportiq://fixture/{fixture_id}, handled by Expo Router."""
    asyncio.run(_notify_new_pick(uuid.UUID(fixture_id), uuid.UUID(prediction_id)))


@celery_app.task(name="app.workers.notify_users.notify_kickoff_reminder")
def notify_kickoff_reminder(fixture_id: str) -> None:
    """T-60 min kickoff reminder for fixtures in a user's watchlist, scheduled via Celery's
    eta parameter (TDD §5.4). Not implemented — no watchlist table exists in the TDD §2.1
    schema (PICK-07 "save to watchlist" is a Could-Have, Phase 2 requirement)."""
    raise NotImplementedError("No watchlist table exists yet — see PICK-07 (Phase 2)")
