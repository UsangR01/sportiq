"""Live score polling (TDD §2.3) — previously an unreachable stub because TheRundown's own
scores endpoint doesn't map onto any DataSourceAdapter ABC method (see the module's prior
history in git). Real now via a different route: fetch_fixtures (the stats/fixtures
adapter — API-Football for football, BallDontLie for NBA) already returns live goals/status
for any fixture in its queried date range, since that's the same endpoint ingest_fixtures.py
uses for the daily backfill. Re-querying a narrow window around "now" every 5 minutes catches
score/status changes for fixtures already in our DB, without needing a dedicated live-scores
adapter method at all.
"""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.adapters.base import FixturePayload
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.celery import celery_app, run_task
from app.workers.ingest_fixtures import _maybe_settle_outcome, _upsert_live_state

logger = logging.getLogger(__name__)

# +/-1 day around "now" — wide enough to catch a fixture that kicked off late yesterday (UTC)
# and is still in progress, or one about to start in the next few hours, without re-querying
# a fixture's provider-side data any more often than every 5 minutes (TDD §2.3's schedule).
LIVE_SCORES_WINDOW_DAYS = 1


async def _ingest_live_scores_for_league(sport: Sport, league: League) -> None:
    adapter = AdapterFactory.get_stats_adapter(sport.slug)

    async with async_session_factory() as db:
        payloads = await adapter.fetch_fixtures(
            sport=sport.slug,
            league=league.slug,
            days_ahead=LIVE_SCORES_WINDOW_DAYS,
            days_back=LIVE_SCORES_WINDOW_DAYS,
        )

        for payload in payloads:
            # Only update fixtures we already know about — a fixture this poll discovers for
            # the first time is ingest_fixtures.py's job (team creation, feature computation),
            # not this one's; it'll be picked up on the next daily run.
            fixture = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == sport.id, Fixture.external_id == payload.external_id
                    )
                )
            ).scalar_one_or_none()
            if fixture is None:
                continue

            new_status = FixtureStatus(payload.status)
            # Refresh the kickoff whenever a real one arrives — same gap as ingest_fixtures.py,
            # and this worker runs every 5 minutes so it corrects a stale time far sooner.
            if not payload.kickoff_is_estimated and payload.kickoff_utc is not None:
                fixture.kickoff_utc = payload.kickoff_utc
                fixture.kickoff_is_estimated = False
            elif (
                fixture.kickoff_is_estimated
                and payload.kickoff_utc is not None
                and payload.kickoff_utc != fixture.kickoff_utc
            ):
                # A REVISED estimate is still real information. Only accepting a confirmed time
                # froze fixtures on whatever day was first guessed: matches the provider had
                # since moved to today sat in the feed under yesterday, some of them already
                # underway, because the correction arrived as another estimate and was dropped.
                # ingest_fixtures.py already applies this rule; without it here the daily run
                # was the only thing that could fix a date, and it cannot fix one that moves
                # after it runs. Downgrading a CONFIRMED time to an estimate stays forbidden —
                # that is the first branch's job, and it is why this is an elif.
                fixture.kickoff_utc = payload.kickoff_utc

            if fixture.status != new_status:
                fixture.status = new_status
            elif new_status is FixtureStatus.SCHEDULED and _looks_underway(fixture, payload):
                # Derive "live" ourselves rather than trusting the provider's own label.
                # BallDontLie reports scheduled for ATP matches that are demonstrably underway
                # (checked against a public scoreboard showing them Interrupted/Suspended while
                # the feed still said scheduled), which left the Live tab permanently empty.
                # A fixture past its kickoff that has a real score on the board is playing,
                # whatever the feed claims.
                fixture.status = FixtureStatus.LIVE

            await _upsert_live_state(db, fixture.id, payload)
            home_team = (
                await db.execute(select(Team).where(Team.id == fixture.home_team_id))
            ).scalar_one_or_none()
            away_team = (
                await db.execute(select(Team).where(Team.id == fixture.away_team_id))
            ).scalar_one_or_none()
            if home_team is not None and away_team is not None:
                await _maybe_settle_outcome(
                    db, fixture.id, payload, home_team, away_team, sport.slug
                )

        await db.commit()


async def _ingest_live_scores() -> None:
    async with async_session_factory() as db:
        sports = (await db.execute(select(Sport).where(Sport.active.is_(True)))).scalars().all()
        leagues_by_sport = {
            sport.id: (
                await db.execute(
                    select(League).where(League.sport_id == sport.id, League.active.is_(True))
                )
            )
            .scalars()
            .all()
            for sport in sports
        }

    for sport in sports:
        for league in leagues_by_sport[sport.id]:
            # One league's stats adapter failing (e.g. a sport whose provider tier isn't
            # unlocked yet — tennis's BallDontLie endpoints 401 until the ALL-STAR plan is
            # confirmed, see CLAUDE.md) must never block every OTHER league's live-score
            # poll for the rest of this 5-minute cycle — same per-league isolation principle
            # ingest_odds.py already applies per-adapter. Without this, a single sport stuck
            # in this loop silently freezes every other sport's live scores/status forever,
            # since this task runs the full sport/league list every time it fires. ValueError
            # is also caught here (not just httpx.HTTPError): AdapterFactory.get_stats_adapter
            # raises it for any sport with no registered adapter at all — a real, if rarer,
            # misconfiguration case that shouldn't be able to take every other sport down
            # either.
            try:
                await _ingest_live_scores_for_league(sport, league)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Live-score polling failed for sport=%s league=%s (%s) — skipping, "
                    "other leagues unaffected",
                    sport.slug,
                    league.slug,
                    exc,
                )


@celery_app.task(name="app.workers.ingest_live_scores.ingest_live_scores")
def ingest_live_scores() -> None:
    """Celery beat triggers this every 5 minutes, alongside odds ingest (TDD §2.3)."""
    run_task(_ingest_live_scores())


def _looks_underway(fixture: Fixture, payload: FixturePayload) -> bool:
    """Is this fixture demonstrably being played, whatever the provider's status says?

    Two conditions, both required. The kickoff must have passed — never promote a fixture
    whose start time is still ahead of us, since a stale score from a previous meeting would
    otherwise mark a future match live. And there must be a real score on the board: a match
    with 0-0 and no clock hasn't provably started, so it stays scheduled rather than being
    guessed into LIVE.

    A fixture whose kickoff is only an ESTIMATE is deliberately excluded — a fabricated
    midnight is always "in the past", so treating it as evidence would promote most of a
    tournament the moment any score appeared.
    """
    if fixture.kickoff_is_estimated or fixture.kickoff_utc is None:
        return False
    kickoff = fixture.kickoff_utc
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=UTC)
    if kickoff > datetime.now(UTC):
        return False
    scored = (payload.home_score or 0) > 0 or (payload.away_score or 0) > 0
    return scored or payload.match_minute is not None
