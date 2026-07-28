import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.schemas import (
    FixtureDetail,
    FixtureSummary,
    LiveStateResponse,
    OddsLineResponse,
    PredictionResponse,
    TeamFeaturesResponse,
)
from app.odds.models import Odds
from app.predictions.models import Prediction
from app.sports.models import League, Sport

router = APIRouter(tags=["fixtures"])


def _fixture_query():
    home_team = aliased(Team)
    away_team = aliased(Team)
    return (
        select(Fixture, Sport.slug, League.slug, home_team.name, away_team.name)
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(League, League.id == Fixture.league_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
    )


def _to_summary(
    fixture: Fixture, sport_slug: str, league_slug: str, home_name: str, away_name: str
) -> FixtureSummary:
    return FixtureSummary(
        id=fixture.id,
        sport_slug=sport_slug,
        league_slug=league_slug,
        home_team=home_name,
        away_team=away_name,
        kickoff_utc=fixture.kickoff_utc,
        status=fixture.status.value,
        season=fixture.season,
    )


@router.get("/fixtures", response_model=list[FixtureSummary])
async def list_fixtures(
    sport_slug: str | None = None,
    league_slug: str | None = None,
    status_filter: FixtureStatus | None = Query(None, alias="status"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = _fixture_query()
    if sport_slug:
        stmt = stmt.where(Sport.slug == sport_slug)
    if league_slug:
        stmt = stmt.where(League.slug == league_slug)
    if status_filter:
        stmt = stmt.where(Fixture.status == status_filter)
    if date_from:
        stmt = stmt.where(Fixture.kickoff_utc >= date_from)
    if date_to:
        stmt = stmt.where(Fixture.kickoff_utc <= date_to)
    stmt = stmt.order_by(Fixture.kickoff_utc).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    return [_to_summary(*row) for row in rows]


async def _load_fixture_or_404(fixture_id: uuid.UUID, db: AsyncSession):
    stmt = _fixture_query().where(Fixture.id == fixture_id)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture not found")
    return row


@router.get("/fixtures/{fixture_id}", response_model=FixtureDetail)
async def get_fixture(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    fixture, sport_slug, league_slug, home_name, away_name = await _load_fixture_or_404(
        fixture_id, db
    )

    live_state_row = (
        await db.execute(select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id))
    ).scalar_one_or_none()

    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id))).scalars().all()
    )

    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id == fixture_id)))
        .scalars()
        .all()
    )
    latest_prediction = max(prediction_rows, key=lambda p: p.created_at, default=None)

    home_features = (
        await db.execute(
            select(TeamFeatures).where(
                TeamFeatures.fixture_id == fixture_id, TeamFeatures.team_id == fixture.home_team_id
            )
        )
    ).scalar_one_or_none()
    away_features = (
        await db.execute(
            select(TeamFeatures).where(
                TeamFeatures.fixture_id == fixture_id, TeamFeatures.team_id == fixture.away_team_id
            )
        )
    ).scalar_one_or_none()

    return FixtureDetail(
        **_to_summary(fixture, sport_slug, league_slug, home_name, away_name).model_dump(),
        live_state=(
            LiveStateResponse.model_validate(live_state_row, from_attributes=True)
            if live_state_row
            else None
        ),
        odds=[OddsLineResponse.model_validate(o, from_attributes=True) for o in odds_rows],
        prediction=(
            PredictionResponse(
                model_version=latest_prediction.model_version,
                home_prob=latest_prediction.home_prob,
                draw_prob=latest_prediction.draw_prob,
                away_prob=latest_prediction.away_prob,
                confidence_tier=latest_prediction.confidence_tier.value,
                expected_value=latest_prediction.expected_value,
            )
            if latest_prediction
            else None
        ),
        home_team_form=(
            TeamFeaturesResponse.model_validate(home_features, from_attributes=True)
            if home_features
            else None
        ),
        away_team_form=(
            TeamFeaturesResponse.model_validate(away_features, from_attributes=True)
            if away_features
            else None
        ),
    )


@router.get("/fixtures/{fixture_id}/live", response_model=LiveStateResponse)
async def get_fixture_live(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    live_state = (
        await db.execute(select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id))
    ).scalar_one_or_none()
    if live_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture is not live")
    return LiveStateResponse.model_validate(live_state, from_attributes=True)


@router.get("/fixtures/{fixture_id}/odds", response_model=list[OddsLineResponse])
async def get_fixture_odds(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    odds_rows = (
        (await db.execute(select(Odds).where(Odds.fixture_id == fixture_id))).scalars().all()
    )
    return [OddsLineResponse.model_validate(o, from_attributes=True) for o in odds_rows]


@router.get("/fixtures/{fixture_id}/prediction", response_model=PredictionResponse)
async def get_fixture_prediction(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    prediction_rows = (
        (await db.execute(select(Prediction).where(Prediction.fixture_id == fixture_id)))
        .scalars()
        .all()
    )
    latest = max(prediction_rows, key=lambda p: p.created_at, default=None)
    if latest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No prediction available")
    return PredictionResponse(
        model_version=latest.model_version,
        home_prob=latest.home_prob,
        draw_prob=latest.draw_prob,
        away_prob=latest.away_prob,
        confidence_tier=latest.confidence_tier.value,
        expected_value=latest.expected_value,
    )
