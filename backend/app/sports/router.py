from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.sports.models import League, Sport
from app.sports.schemas import SportResponse

router = APIRouter(tags=["sports"])


@router.get("/sports", response_model=list[SportResponse])
async def list_sports(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Sport, func.count(League.id))
        .outerjoin(League, League.sport_id == Sport.id)
        .where(Sport.active.is_(True))
        .group_by(Sport.id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        SportResponse(
            id=sport.id,
            slug=sport.slug,
            name=sport.name,
            model_type=sport.model_type,
            league_count=league_count,
        )
        for sport, league_count in rows
    ]
