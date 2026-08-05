import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import select

from app.adapters.base import OddsPayload
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.fixtures.service import find_fixture_by_abbreviations_and_time
from app.odds.models import Odds
from app.sports.models import League, Sport
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

ODDS_CACHE_TTL_SECONDS = 10 * 60
# Shortened from 7. Each extra day of lookahead is another potential request per league per
# run, and odds more than a few days out are both less accurate and less useful — the feed's
# value is concentrated in near-term fixtures.
ODDS_LOOKAHEAD_DAYS = 3


async def _resolve_fixture(
    db, sport_id, league_id, payload: OddsPayload
) -> tuple[Fixture | None, OddsPayload]:
    """Three fast paths in order, then a last-resort fuzzy fallback:
    1. This odds-provider event was already matched to a fixture on a previous run
       (odds_provider_external_id).
    2. payload.fixture_external_id matches a Fixture.external_id directly — real whenever the
       odds provider and the stats/fixtures provider for this sport are the SAME provider
       (e.g. API-Football odds for a football fixture API-Football itself ingested). Not
       applicable to TheRundown's payloads (a genuinely different ID space from BallDontLie/
       API-Football's fixture IDs), where this is just a guaranteed miss, not a false match.
    3. Team abbreviation + kickoff-time fuzzy match (the only option when the two providers
       use unrelated ID spaces, e.g. TheRundown odds vs BallDontLie/API-Football fixtures)."""
    fixture = (
        await db.execute(
            select(Fixture).where(
                Fixture.sport_id == sport_id,
                Fixture.odds_provider_external_id == payload.fixture_external_id,
            )
        )
    ).scalar_one_or_none()
    if fixture is not None:
        return fixture, payload

    fixture = (
        await db.execute(
            select(Fixture).where(
                Fixture.sport_id == sport_id, Fixture.external_id == payload.fixture_external_id
            )
        )
    ).scalar_one_or_none()
    if fixture is not None:
        if fixture.odds_provider_external_id is None:
            fixture.odds_provider_external_id = payload.fixture_external_id
        return fixture, payload

    if payload.kickoff_utc is None:
        return None, payload

    fixture = await find_fixture_by_abbreviations_and_time(
        db,
        sport_id,
        payload.home_team_short_name,
        payload.away_team_short_name,
        payload.kickoff_utc,
        league_id=league_id,
    )
    if fixture is None:
        # Try the SAME pairing with the sides swapped.
        #
        # Which competitor is "home" is not a fact about a tennis match — there is no home
        # court. Our side is an arbitrary but stable tiebreak (lower external player id, see
        # balldontlie_tennis.py:_home_away_players) and TheRundown picks its own. Measured on a
        # single real ATP day they disagreed for 8 of 22 matches, and because this matcher
        # requires home->home the odds for all 8 were silently dropped — no price, no error.
        fixture = await find_fixture_by_abbreviations_and_time(
            db,
            sport_id,
            payload.away_team_short_name,
            payload.home_team_short_name,
            payload.kickoff_utc,
            league_id=league_id,
        )

    if fixture is not None and fixture.odds_provider_external_id is None:
        fixture.odds_provider_external_id = payload.fixture_external_id
    return fixture, payload


async def _orient_payload(db, fixture: Fixture, payload: OddsPayload) -> OddsPayload:
    """Return `payload` with its sides swapped if the provider's home is our away.

    Decided by comparing NAMES every time, deliberately not by which lookup path matched.
    An earlier version flipped only inside the swapped-name fallback, which was wrong in a way
    that is easy to miss and expensive to get wrong: the first bookmaker's payload sets
    odds_provider_external_id, so every LATER payload for the same event resolves by that id
    instead — skipping the fallback, keeping the provider's orientation, and overwriting the
    corrected prices. The visible result was Alexander Zverev, a heavy favourite the market had
    at ~1.17, showing at 8.00.

    Attaching the favourite's price to the underdog is worse than having no odds at all, so
    this errs toward leaving the payload untouched whenever the names cannot be compared.
    """
    if not payload.home_team_short_name or not payload.away_team_short_name:
        return payload
    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    if home_team is None:
        return payload

    ours = (home_team.short_name or home_team.name or "").strip().casefold()
    theirs_home = payload.home_team_short_name.strip().casefold()
    theirs_away = payload.away_team_short_name.strip().casefold()
    if not ours or theirs_home == theirs_away:
        return payload
    # Only flip on a positive match for the AWAY side; an unrecognised name changes nothing.
    if ours == theirs_home or ours != theirs_away:
        return payload
    return replace(
        payload,
        home_odds=payload.away_odds,
        away_odds=payload.home_odds,
        over_odds=payload.under_odds,
        under_odds=payload.over_odds,
    )


async def _dates_with_fixtures(sport: Sport, league: League) -> list[date]:
    """The distinct kickoff dates this league actually has upcoming fixtures on.

    Odds endpoints are queried one request per DATE, so walking every day in the lookahead
    window spends a request on dates with no fixtures at all — which is most days, for most
    leagues. That waste is not theoretical: it exhausted TheRundown's 1,000-request MONTHLY
    quota in roughly 90 minutes (7 leagues x 8 dates x 288 runs/day ~= 16,000 requests/day),
    after which football had no odds at all for weeks. With no odds, expected-value ranking
    and the min_odds filter both silently degrade to probability-only behaviour, so the damage
    surfaced as an apparent modelling problem rather than an ingestion one.

    Querying only real fixture dates makes the cost proportional to the actual schedule
    instead of to the calendar."""
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Fixture.kickoff_utc).where(
                    Fixture.league_id == league.id,
                    Fixture.status.in_([FixtureStatus.SCHEDULED, FixtureStatus.LIVE]),
                    Fixture.kickoff_utc >= now - timedelta(days=1),
                    Fixture.kickoff_utc <= now + timedelta(days=ODDS_LOOKAHEAD_DAYS),
                )
            )
        ).scalars()
    return sorted({kickoff.date() for kickoff in rows})


async def _fetch_odds_payloads(sport: Sport, league: League) -> list[OddsPayload]:
    """Queries every odds adapter registered for this sport (see
    AdapterFactory.get_odds_adapters) and merges their results — football queries both
    TheRundown and API-Football since real coverage is complementary, split by league, not
    redundant (see CLAUDE.md). A league one adapter has no mapping for raises ValueError from
    that adapter alone (e.g. TheRundown for Brasileirão) — caught per-adapter so it can't
    block a DIFFERENT adapter's real data for the same league."""
    dates = await _dates_with_fixtures(sport, league)
    if not dates:
        return []

    payloads: list[OddsPayload] = []
    for adapter in AdapterFactory.get_odds_adapters(sport.slug):
        try:
            payloads.extend(
                await adapter.fetch_odds(
                    sport=sport.slug,
                    league=league.slug,
                    days_ahead=ODDS_LOOKAHEAD_DAYS,
                    dates=dates,
                )
            )
        except ValueError:
            logger.warning(
                "%s has no odds coverage for league=%s — skipping",
                type(adapter).__name__,
                league.slug,
            )
        except httpx.HTTPError as exc:
            # A transport/status failure from ONE provider must not abort odds ingestion for
            # every other league and sport in the same run. This was a real, two-day outage:
            # TheRundown's rate limit raised straight out of fetch_odds, killing the whole
            # 5-minutely task every cycle, and odds stopped being written entirely — which in
            # turn silently disabled expected-value ranking and the min_odds filter downstream.
            # The adapter now retries/paces internally (see therundown.py), but exhausting
            # those retries must still degrade to "this league has no fresh odds right now"
            # rather than taking everything else down with it. Mirrors the same per-league
            # isolation already added to ingest_fixtures.py/ingest_live_scores.py.
            logger.warning(
                "%s odds fetch failed for league=%s (%s) — skipping, other leagues unaffected",
                type(adapter).__name__,
                league.slug,
                exc,
            )
    return payloads


async def _ingest_odds_for_league(sport: Sport, league: League) -> None:
    redis = get_redis()

    async with async_session_factory() as db:
        payloads = await _fetch_odds_payloads(sport, league)

        for payload in payloads:
            # Events for a game we haven't ingested fixtures for yet (or can't match) are
            # skipped rather than guessed — matches ingest_fixtures.py's dedupe philosophy.
            fixture, payload = await _resolve_fixture(db, sport.id, league.id, payload)
            if fixture is None:
                continue
            payload = await _orient_payload(db, fixture, payload)

            db.add(
                Odds(
                    fixture_id=fixture.id,
                    bookmaker=payload.bookmaker,
                    market=payload.market,
                    home_odds=payload.home_odds,
                    draw_odds=payload.draw_odds,
                    away_odds=payload.away_odds,
                    line=payload.line,
                    over_odds=payload.over_odds,
                    under_odds=payload.under_odds,
                    updated_at=payload.updated_at,
                )
            )
            await redis.set(
                f"odds:{fixture.id}:{payload.bookmaker}:{payload.market}",
                payload.home_odds or "",
                ex=ODDS_CACHE_TTL_SECONDS,
            )
        await db.commit()


async def _ingest_odds() -> None:
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
            # Per-adapter ValueErrors (no odds coverage for this league) are already caught
            # inside _fetch_odds_payloads — nothing further to isolate at this level.
            await _ingest_odds_for_league(sport, league)


@celery_app.task(name="app.workers.ingest_odds.ingest_odds")
def ingest_odds() -> None:
    """Celery beat triggers this every 5 minutes for all active sports (TDD §2.3)."""
    run_task(_ingest_odds())
