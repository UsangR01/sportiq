from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.sports.models import League, Sport
from app.sports.schemas import LeagueOption, SportResponse

router = APIRouter(tags=["sports"])

# Above this, a sport's leagues are NOT offered as filters. Tennis (ATP/WTA) and basketball are
# competitions a user thinks of separately and would pick between; football's 34 would turn one
# row into a scrolling list of everything, and the feed already groups by league internally,
# which is the right affordance at that count.
#
# A threshold rather than a per-sport allowlist so a new competition appears on its own, and so
# nothing has to be edited in two places when a league is added.
#
# RAISED 4 -> 12 ON 2026-10-05, AND THE OLD VALUE IS WHY. Basketball went from 2 leagues to 9
# when the European competitions landed, crossed the threshold, and its picker VANISHED -- the
# NBA and WNBA chips a user had been using disappeared along with the new leagues they were
# looking for. Reported as "I don't see other Basketball leagues", and the picker showing
# nothing at all is a worse answer than showing nine.
#
# 12 rather than 9 deliberately: a threshold set exactly at today's count re-breaks silently the
# next time a league is added, which is precisely the failure being fixed. Football stays out at
# 34 by design, not by accident.
LEAGUE_PICKER_MAX = 12


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
