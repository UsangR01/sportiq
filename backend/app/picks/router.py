import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.core.redis import get_redis
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.odds.models import Odds
from app.picks.schemas import PickResponse
from app.picks.service import (
    best_available_odds,
    best_outcome,
    compute_expected_value,
    meets_threshold,
)
from app.predictions.models import Prediction
from app.sports.models import Sport

router = APIRouter(tags=["picks"])

PICKS_CACHE_TTL_SECONDS = 180
PICKS_LOOKAHEAD_DAYS = 7


@router.get("/picks", response_model=list[PickResponse])
async def get_picks(
    min_odds: float = Query(..., ge=1.01, le=20.0),
    sport_slug: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    redis = get_redis()
    cache_key = f"picks:{sport_slug or 'all'}:{min_odds}:{limit}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return [PickResponse.model_validate(item) for item in json.loads(cached)]

    now = datetime.now(UTC)
    horizon = now + timedelta(days=PICKS_LOOKAHEAD_DAYS)

    home_team = aliased(Team)
    away_team = aliased(Team)

    stmt = (
        select(Fixture, Sport.slug, home_team.name, away_team.name)
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
        .where(
            Fixture.status == FixtureStatus.SCHEDULED,
            Fixture.kickoff_utc >= now,
            Fixture.kickoff_utc <= horizon,
        )
    )
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)

    fixture_rows = (await db.execute(stmt)).all()
    if not fixture_rows:
        await redis.set(cache_key, json.dumps([]), ex=PICKS_CACHE_TTL_SECONDS)
        return []

    fixture_ids = [row[0].id for row in fixture_rows]

    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id.in_(fixture_ids)))).scalars().all()
    )
    odds_by_fixture: dict = {}
    for o in odds_rows:
        odds_by_fixture.setdefault(o.fixture_id, []).append(
            {"home_odds": o.home_odds, "draw_odds": o.draw_odds, "away_odds": o.away_odds}
        )

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id.in_(fixture_ids))))
        .scalars()
        .all()
    )
    latest_prediction_by_fixture: dict = {}
    for p in prediction_rows:
        existing = latest_prediction_by_fixture.get(p.fixture_id)
        if existing is None or p.created_at > existing.created_at:
            latest_prediction_by_fixture[p.fixture_id] = p

    picks: list[PickResponse] = []
    for fixture, sport_slug_val, home_name, away_name in fixture_rows:
        best_odds = best_available_odds(odds_by_fixture.get(fixture.id, []))
        if not meets_threshold(best_odds, min_odds):
            continue

        prediction = latest_prediction_by_fixture.get(fixture.id)
        if prediction is None:
            continue

        outcome = best_outcome(
            prediction.home_prob,
            prediction.draw_prob,
            prediction.away_prob,
            best_odds["home"],
            best_odds["draw"],
            best_odds["away"],
        )
        if outcome is None:
            continue

        picks.append(
            PickResponse(
                fixture_id=fixture.id,
                sport_slug=sport_slug_val,
                home_team=home_name,
                away_team=away_name,
                kickoff_utc=fixture.kickoff_utc,
                selection=outcome.selection,
                odds=outcome.odds,
                model_probability=outcome.probability,
                expected_value=compute_expected_value(outcome.probability, outcome.odds),
                confidence_tier=prediction.confidence_tier.value,
            )
        )

    picks.sort(key=lambda p: p.model_probability, reverse=True)
    picks = picks[:limit]

    await redis.set(
        cache_key,
        json.dumps([p.model_dump(mode="json") for p in picks]),
        ex=PICKS_CACHE_TTL_SECONDS,
    )
    return picks
