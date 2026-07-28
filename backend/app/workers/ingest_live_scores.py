import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus
from app.history.models import MatchResult
from app.workers.celery import celery_app

LIVE_STATE_CACHE_TTL_SECONDS = 6 * 60


def _result_for_scores(home_score: int, away_score: int) -> MatchResult:
    if home_score > away_score:
        return MatchResult.HOME_WIN
    if away_score > home_score:
        return MatchResult.AWAY_WIN
    return MatchResult.DRAW


async def _ingest_live_scores() -> None:
    now = datetime.now(UTC)

    async with async_session_factory() as db:
        candidates = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.status.in_([FixtureStatus.SCHEDULED, FixtureStatus.LIVE]),
                        Fixture.kickoff_utc <= now,
                    )
                )
            )
            .scalars()
            .all()
        )

        for _fixture in candidates:
            # TheRundown's scores endpoint (GET /v2/sports/{sport_id}/events/{date}
            # ?include=scores, TDD §2.3) doesn't map onto any of the 4 DataSourceAdapter
            # methods (TDD §2.2's KEY note only defines fetch_odds/fetch_fixtures/
            # fetch_team_stats/fetch_injuries) — the adapter interface has no live-score
            # method yet. Not implemented.
            raise NotImplementedError(
                "Live score fetching has no corresponding DataSourceAdapter method yet"
            )

            # Once a real fetch exists, the upsert/transition/cache logic is exactly this:
            # live_state = await db.get(FixtureLiveState, fixture.id)
            # if live_state is None:
            #     live_state = FixtureLiveState(
            #         fixture_id=fixture.id, home_score=0, away_score=0, status="live"
            #     )
            #     db.add(live_state)
            # live_state.home_score = ...
            # live_state.away_score = ...
            # live_state.match_minute = ...
            # live_state.period = ...
            # live_state.status = ...
            # live_state.last_updated_utc = now
            # fixture.status = (
            #     FixtureStatus.LIVE
            #     if live_state.status != "completed"
            #     else FixtureStatus.COMPLETED
            # )
            # await redis.set(
            #     f"live:{fixture.id}", json.dumps({...}), ex=LIVE_STATE_CACHE_TTL_SECONDS
            # )
            # if fixture.status == FixtureStatus.COMPLETED:
            #     db.add(Outcome(
            #         fixture_id=fixture.id, home_score=live_state.home_score,
            #         away_score=live_state.away_score,
            #         result=_result_for_scores(live_state.home_score, live_state.away_score),
            #         settled_at=now,
            #     ))
            #     # "model performance metrics are updated" (TDD §2.3) — deferred until
            #     # /stats/model aggregation is implemented (app/history/router.py).
        await db.commit()


@celery_app.task(name="app.workers.ingest_live_scores.ingest_live_scores")
def ingest_live_scores() -> None:
    """Celery beat triggers this every 5 minutes, alongside odds ingest (TDD §2.3)."""
    asyncio.run(_ingest_live_scores())
