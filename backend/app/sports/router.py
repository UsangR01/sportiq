from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.sports.models import League, Sport
from app.sports.schemas import LeagueOption, SportResponse

router = APIRouter(tags=["sports"])

# Above this, a sport's leagues are NOT offered as filters. Basketball (NBA/WNBA) and tennis
# (ATP/WTA) are two competitions each that a user thinks of separately and would pick between;
# football's 18 would turn one dropdown row into a scrolling list of everything, and the feed
# already groups by league internally, which is the right affordance at that count.
#
# A threshold rather than a per-sport allowlist so a third basketball or tennis competition
# appears on its own, and so nothing has to be edited in two places when a league is added.
LEAGUE_PICKER_MAX = 4


@router.get("/sports", response_model=list[SportResponse])
async def list_sports(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Sport, func.count(League.id))
        .outerjoin(League, League.sport_id == Sport.id)
        .where(Sport.active.is_(True))
        .group_by(Sport.id)
    )
    rows = (await db.execute(stmt)).all()

    pickable = [sport.id for sport, count in rows if 0 < count <= LEAGUE_PICKER_MAX]
    leagues_by_sport: dict = {}
    if pickable:
        league_rows = (
            await db.execute(
                select(League)
                .where(League.sport_id.in_(pickable), League.active.is_(True))
                .order_by(League.slug)
            )
        ).scalars()
        for league in league_rows:
            leagues_by_sport.setdefault(league.sport_id, []).append(
                LeagueOption(slug=league.slug, name=league.name)
            )

    return [
        SportResponse(
            id=sport.id,
            slug=sport.slug,
            name=sport.name,
            model_type=sport.model_type,
            league_count=league_count,
            leagues=leagues_by_sport.get(sport.id, []),
        )
        for sport, league_count in rows
    ]
