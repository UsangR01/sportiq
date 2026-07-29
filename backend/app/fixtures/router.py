import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.schemas import (
    BestPick,
    ExtraMarketsResponse,
    FixtureDetail,
    FixtureSummary,
    LiveStateResponse,
    OddsLineResponse,
    PredictionResponse,
    TeamFeaturesResponse,
    TotalsProbability,
)
from app.models_ml.markets import CORNERS_LINES, GOALS_LINES, double_chance_probs, over_under_probs
from app.odds.models import Odds
from app.picks.service import best_available_odds, best_outcome
from app.predictions.models import Prediction
from app.sports.models import League, Sport

router = APIRouter(tags=["fixtures"])


def _build_extra_markets(prediction: Prediction) -> ExtraMarketsResponse:
    """Derives double chance and Over/Under goals/corners probabilities from an existing
    Prediction row — see app/models_ml/markets.py for why none of this needs a new model or
    a live recompute (double chance is arithmetic on home/draw/away; totals reuse the stored
    xg_home/xg_away and corners_xg_home/corners_xg_away as a Poisson rate)."""
    home_or_draw, away_or_draw = double_chance_probs(
        prediction.home_prob, prediction.draw_prob, prediction.away_prob
    )
    goals_total = (
        prediction.xg_home + prediction.xg_away
        if prediction.xg_home is not None and prediction.xg_away is not None
        else None
    )
    corners_total = (
        prediction.corners_xg_home + prediction.corners_xg_away
        if prediction.corners_xg_home is not None and prediction.corners_xg_away is not None
        else None
    )
    goals_probs = over_under_probs(goals_total, GOALS_LINES)
    corners_probs = over_under_probs(corners_total, CORNERS_LINES)
    return ExtraMarketsResponse(
        double_chance_home_or_draw_prob=home_or_draw,
        double_chance_away_or_draw_prob=away_or_draw,
        goals_totals=[
            TotalsProbability(line=line, under_prob=under, over_prob=over)
            for line, (under, over) in goals_probs.items()
        ],
        corners_totals=[
            TotalsProbability(line=line, under_prob=under, over_prob=over)
            for line, (under, over) in corners_probs.items()
        ],
    )


def _fixture_query():
    home_team = aliased(Team)
    away_team = aliased(Team)
    return (
        select(
            Fixture,
            Sport.slug,
            League.slug,
            League.name,
            League.country,
            home_team.name,
            away_team.name,
        )
        .join(Sport, Sport.id == Fixture.sport_id)
        .join(League, League.id == Fixture.league_id)
        .join(home_team, home_team.id == Fixture.home_team_id)
        .join(away_team, away_team.id == Fixture.away_team_id)
    )


def _to_summary(
    fixture: Fixture,
    sport_slug: str,
    league_slug: str,
    league_name: str,
    league_country: str | None,
    home_name: str,
    away_name: str,
    best_pick: BestPick | None = None,
    live_state: LiveStateResponse | None = None,
) -> FixtureSummary:
    return FixtureSummary(
        id=fixture.id,
        sport_slug=sport_slug,
        league_slug=league_slug,
        league_name=league_name,
        league_country=league_country,
        home_team=home_name,
        away_team=away_name,
        kickoff_utc=fixture.kickoff_utc,
        status=fixture.status.value,
        season=fixture.season,
        best_pick=best_pick,
        live_state=live_state,
    )


async def _bulk_live_states(db: AsyncSession, fixture_ids: list) -> dict:
    """Bulk-fetch FixtureLiveState for a whole page of /fixtures results — same one-query-not-
    N pattern as _bulk_best_picks, so the Home feed can show a score inline without a
    per-fixture round trip."""
    if not fixture_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id.in_(fixture_ids))
            )
        )
        .scalars()
        .all()
    )
    return {
        row.fixture_id: LiveStateResponse.model_validate(row, from_attributes=True) for row in rows
    }


async def _bulk_best_picks(db: AsyncSession, fixture_ids: list) -> dict:
    """Same selection/probability/odds math app/picks/service.py uses for /picks, computed in
    bulk for a whole page of /fixtures results — one query each for odds and predictions
    rather than a per-fixture round trip, mirroring app/picks/router.py's own bulk-fetch
    pattern. Unlike /picks, this never filters fixtures out by odds threshold — every
    fixture that has a real prediction gets a best_pick entry, odds or not (the mobile client
    decides how prominently to surface it, e.g. by a probability threshold)."""
    if not fixture_ids:
        return {}

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

    picks: dict = {}
    for fixture_id, prediction in latest_prediction_by_fixture.items():
        best_odds = best_available_odds(odds_by_fixture.get(fixture_id, []))
        outcome = best_outcome(
            prediction.home_prob,
            prediction.draw_prob,
            prediction.away_prob,
            best_odds["home"],
            best_odds["draw"],
            best_odds["away"],
        )
        # best_outcome requires odds to pick a candidate (it's built for /picks' EV math,
        # which needs both) — here we still want a badge when only a probability exists, so
        # fall back to whichever probability is highest when no odds have landed yet.
        if outcome is not None:
            picks[fixture_id] = BestPick(
                selection=outcome.selection, probability=outcome.probability, odds=outcome.odds
            )
        else:
            candidates = [
                ("home", prediction.home_prob, best_odds["home"]),
                ("draw", prediction.draw_prob, best_odds["draw"]),
                ("away", prediction.away_prob, best_odds["away"]),
            ]
            candidates = [c for c in candidates if c[1] is not None]
            if candidates:
                selection, probability, odds = max(candidates, key=lambda c: c[1])
                picks[fixture_id] = BestPick(
                    selection=selection, probability=probability, odds=odds
                )

    return picks


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
    fixture_ids = [row[0].id for row in rows]
    best_picks = await _bulk_best_picks(db, fixture_ids)
    live_states = await _bulk_live_states(db, fixture_ids)
    return [
        _to_summary(
            *row, best_pick=best_picks.get(row[0].id), live_state=live_states.get(row[0].id)
        )
        for row in rows
    ]


async def _load_fixture_or_404(fixture_id: uuid.UUID, db: AsyncSession):
    stmt = _fixture_query().where(Fixture.id == fixture_id)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fixture not found")
    return row


@router.get("/fixtures/{fixture_id}", response_model=FixtureDetail)
async def get_fixture(fixture_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    fixture, sport_slug, league_slug, league_name, league_country, home_name, away_name = (
        await _load_fixture_or_404(fixture_id, db)
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
        **_to_summary(
            fixture,
            sport_slug,
            league_slug,
            league_name,
            league_country,
            home_name,
            away_name,
            live_state=(
                LiveStateResponse.model_validate(live_state_row, from_attributes=True)
                if live_state_row
                else None
            ),
        ).model_dump(),
        odds=[OddsLineResponse.model_validate(o, from_attributes=True) for o in odds_rows],
        prediction=(
            PredictionResponse(
                model_version=latest_prediction.model_version,
                home_prob=latest_prediction.home_prob,
                draw_prob=latest_prediction.draw_prob,
                away_prob=latest_prediction.away_prob,
                confidence_tier=latest_prediction.confidence_tier.value,
                expected_value=latest_prediction.expected_value,
                extra_markets=_build_extra_markets(latest_prediction),
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
        extra_markets=_build_extra_markets(latest),
    )
