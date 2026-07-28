import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.rotowire import RotoWireAdapter
from app.core.config import get_settings
from app.core.database import async_session_factory
from app.fixtures.models import (
    Fixture,
    FixtureStatus,
    PlayerInjuryStatus,
    Team,
    TeamFeatures,
    TeamKeyPlayer,
)
from app.models_ml.nba_key_players import get_key_player_availability
from app.sports.models import Sport
from app.workers.celery import celery_app

logger = logging.getLogger(__name__)

RE_INFERENCE_WINDOW_HOURS = 3


async def _resolve_team(db, sport_id, external_id: str) -> Team | None:
    """update.team_external_id is the injury provider's own team ID — a different space from
    our internal UUID (the same cross-provider mismatch as fixtures/odds). Must be resolved
    via Team.external_id before use, never assigned directly into a UUID FK column (that bug
    was previously latent here — never exercised, since both injury adapters were still
    NotImplementedError stubs until now)."""
    return (
        await db.execute(
            select(Team).where(Team.sport_id == sport_id, Team.external_id == external_id)
        )
    ).scalar_one_or_none()


async def _has_active_nba_fixtures_today(db, sport_id) -> bool:
    now = datetime.now(UTC)
    end_of_day = now.replace(hour=23, minute=59, second=59)
    result = await db.execute(
        select(Fixture.id).where(
            Fixture.sport_id == sport_id,
            Fixture.status != FixtureStatus.COMPLETED,
            Fixture.kickoff_utc.between(now, end_of_day),
        )
    )
    return result.first() is not None


async def _maybe_trigger_reinference(db, sport: Sport, team_id, player_name: str) -> None:
    """TDD §3.3: fires when a player in that team's season team_key_players (not a salary-
    rank proxy) is reported OUT within 3 hours of tip-off — replaces the previous log-only
    placeholder (there was no trained model to call inference on when this was first
    written) and the previous coarse "any fixture today" gate with the actual per-fixture
    3-hour window the TDD specifies."""
    from app.workers.run_predictions import run_predictions

    is_key_player = (
        await db.execute(
            select(TeamKeyPlayer.id).where(
                TeamKeyPlayer.team_id == team_id,
                func.lower(TeamKeyPlayer.player_name) == player_name.lower(),
            )
        )
    ).first() is not None
    if not is_key_player:
        return

    now = datetime.now(UTC)
    window_end = now + timedelta(hours=RE_INFERENCE_WINDOW_HOURS)
    fixtures = (
        (
            await db.execute(
                select(Fixture).where(
                    Fixture.sport_id == sport.id,
                    Fixture.status == FixtureStatus.SCHEDULED,
                    Fixture.kickoff_utc.between(now, window_end),
                    or_(Fixture.home_team_id == team_id, Fixture.away_team_id == team_id),
                )
            )
        )
        .scalars()
        .all()
    )

    for fixture in fixtures:
        # Recompute the fresh key-player-availability snapshot before re-running inference —
        # TDD §3.3 says Stage 2 runs "again on any RotoWire re-inference trigger", not just
        # at the original ingest time.
        available, per_combined = await get_key_player_availability(
            db, team_id, int(fixture.season)
        )
        team_features = (
            await db.execute(
                select(TeamFeatures).where(
                    TeamFeatures.fixture_id == fixture.id, TeamFeatures.team_id == team_id
                )
            )
        ).scalar_one_or_none()
        if team_features is not None:
            team_features.key_players_available = available
            team_features.key_players_per_combined = per_combined
            await db.commit()

        logger.info(
            "Re-inference trigger: key player %s OUT within %sh of fixture %s",
            player_name,
            RE_INFERENCE_WINDOW_HOURS,
            fixture.id,
        )
        run_predictions.delay(str(fixture.id))


async def _ingest_injuries_rotowire(db, sport: Sport) -> None:
    """RotoWire path (TDD §2.3): only runs on NBA game days; a key player (team_key_players)
    reported OUT within 3 hours of tip-off triggers re-inference for that specific fixture."""
    if not await _has_active_nba_fixtures_today(db, sport.id):
        return

    adapter = RotoWireAdapter()
    updates = await adapter.fetch_injuries(sport=sport.slug)

    now = datetime.now(UTC)
    for update in updates:
        team = await _resolve_team(db, sport.id, update.team_external_id)
        if team is None:
            continue

        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id=update.player_external_id,
                team_id=team.id,
                player_name=update.player_name,
                status=update.status,
                return_date=update.return_date,
                salary_rank=update.salary_rank,
                source=update.source,
                updated_at=now,
            )
        )
        await db.commit()

        if update.status == "OUT":
            await _maybe_trigger_reinference(db, sport, team.id, update.player_name)


async def _ingest_injuries_balldontlie(db, sport: Sport) -> None:
    """BallDontLie fallback path (TDD §2.3): used automatically when ROTOWIRE_API_KEY is
    absent. Less real-time (no GTD → OUT alerts); re-inference trigger stays disabled."""
    adapter = BallDontLieAdapter()
    updates = await adapter.fetch_injuries(sport=sport.slug)

    now = datetime.now(UTC)
    for update in updates:
        team = await _resolve_team(db, sport.id, update.team_external_id)
        if team is None:
            continue

        db.add(
            PlayerInjuryStatus(
                sport_id=sport.id,
                player_id=update.player_external_id,
                team_id=team.id,
                player_name=update.player_name,
                status=update.status,
                return_date=update.return_date,
                salary_rank=update.salary_rank,
                source=update.source,
                updated_at=now,
            )
        )
    await db.commit()


async def _ingest_injuries() -> None:
    settings = get_settings()

    async with async_session_factory() as db:
        nba = (await db.execute(select(Sport).where(Sport.slug == "nba"))).scalar_one_or_none()
        if nba is None:
            return

        if not settings.rotowire_api_key:
            logger.warning("ROTOWIRE_API_KEY not set — using BallDontLie fallback")
            await _ingest_injuries_balldontlie(db, nba)
        else:
            await _ingest_injuries_rotowire(db, nba)


@celery_app.task(name="app.workers.ingest_injuries.ingest_injuries")
def ingest_injuries() -> None:
    """Celery beat triggers this every 30 minutes; the RotoWire path additionally gates on
    NBA game days (TDD §2.3). NBA only at MVP."""
    asyncio.run(_ingest_injuries())
