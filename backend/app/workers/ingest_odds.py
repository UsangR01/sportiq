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
from app.fixtures.service import find_fixture_by_abbreviations_and_time, name_tokens
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
    db, sport_id, league_id, payload: OddsPayload, sport_slug: str | None = None
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

    # Tennis competitors are PEOPLE, and the two providers spell their names differently
    # (order reversed, extra given names, capitalisation). Clubs do not have that problem, so
    # the tolerance is opt-in per sport rather than loosened for everyone.
    name_variants = sport_slug == "tennis"

    fixture = await find_fixture_by_abbreviations_and_time(
        db,
        sport_id,
        payload.home_team_short_name,
        payload.away_team_short_name,
        payload.kickoff_utc,
        league_id=league_id,
        allow_name_variants=name_variants,
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
            allow_name_variants=name_variants,
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

    ours_raw = (home_team.short_name or home_team.name or "").strip()
    ours = ours_raw.casefold()
    theirs_home = payload.home_team_short_name.strip().casefold()
    theirs_away = payload.away_team_short_name.strip().casefold()
    if not ours or theirs_home == theirs_away:
        return payload

    # Compared as token SETS as well as literally, because the matcher now resolves fixtures
    # whose names differ by ordering or extra given names ("Wu Yibing" for "Yibing Wu"). Those
    # would fail a literal comparison here and silently keep the provider's orientation, which
    # is the exact bug that once showed a 1.17 favourite at 8.00 — matching a fixture and then
    # mis-orienting it is worse than never matching it.
    ours_tokens = name_tokens(ours_raw)
    home_tokens = name_tokens(payload.home_team_short_name)
    away_tokens = name_tokens(payload.away_team_short_name)

    def same_person(a: frozenset[str], b: frozenset[str]) -> bool:
        return bool(a) and bool(b) and len(a & b) >= 2 and (a <= b or b <= a)

    is_their_home = ours == theirs_home or same_person(ours_tokens, home_tokens)
    is_their_away = ours == theirs_away or same_person(ours_tokens, away_tokens)

    # Only flip on a positive match for the AWAY side; an unrecognised name changes nothing.
    if is_their_home or not is_their_away:
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


async def _fetch_odds_payloads(
    sport: Sport, league: League, adapters: list | None = None, dates: list | None = None
) -> list[OddsPayload]:
    """Queries every odds adapter registered for this sport (see
    AdapterFactory.get_odds_adapters) and merges their results — football queries both
    TheRundown and API-Football since real coverage is complementary, split by league, not
    redundant (see CLAUDE.md). A league one adapter has no mapping for raises ValueError from
    that adapter alone (e.g. TheRundown for Brasileirão) — caught per-adapter so it can't
    block a DIFFERENT adapter's real data for the same league.

    `adapters` restricts the run to a specific subset, which exists so a quota-free provider
    can be refreshed more often than a quota-metered one — see _ingest_tennis_odds. Default
    (None) queries every adapter registered for the sport.

    `dates` likewise narrows the request to specific match days. The closing-odds capture uses
    it to ask for ONLY the day a fixture kicks off on, rather than the whole lookahead window,
    which is what keeps that job affordable against TheRundown's monthly cap."""
    dates = dates if dates is not None else await _dates_with_fixtures(sport, league)
    if not dates:
        return []

    payloads: list[OddsPayload] = []
    for adapter in (
        adapters if adapters is not None else AdapterFactory.get_odds_adapters(sport.slug)
    ):
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


async def _ingest_odds_for_league(
    sport: Sport, league: League, adapters: list | None = None, dates: list | None = None
) -> None:
    redis = get_redis()

    async with async_session_factory() as db:
        payloads = await _fetch_odds_payloads(sport, league, adapters, dates)

        for payload in payloads:
            # Events for a game we haven't ingested fixtures for yet (or can't match) are
            # skipped rather than guessed — matches ingest_fixtures.py's dedupe philosophy.
            fixture, payload = await _resolve_fixture(db, sport.id, league.id, payload, sport.slug)
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


async def _ingest_tennis_odds(adapters: list | None = None) -> None:
    """Refresh tennis odds ahead of the shared 6-hourly run, which is too slow for this sport.

    Books price a tennis match close to its start and new fixtures appear daily, so a six-hour
    gap routinely left genuinely priced matches showing no odds on the card. Two jobs feed this
    function, deliberately at different cadences, because the two providers have very different
    costs and very different coverage:

      BallDontLie (hourly)      free — it is also the tennis fixtures provider, so its GOAT
                                allowance is 600 requests/MINUTE, and one refresh costs a
                                couple of calls. But it prices FEW matches: measured on a real
                                day, 4 distinct matches against 60 of our fixtures in window.

      TheRundown (2-hourly)     quota-metered at 5,000/MONTH, and far broader: 31 real ATP
                                events that same day, ALL 31 with an unmasked moneyline —
                                unusually good for this subscription.

    THE CADENCE IS A QUOTA DECISION, NOT A DEFAULT. Tennis costs ~2 calls per run (one per
    date with fixtures), so 2-hourly is ~720 calls/month. Hourly would be ~1,440, which does
    not fit: football is expected to reach ~3,600/month once the European seasons open, and
    5,040 would breach the cap. Two hours cuts the staleness window from six to two while
    leaving real headroom. Revisit only with the football figure re-measured, not by feel.
    """
    if adapters is None:
        from app.adapters.balldontlie_tennis import BallDontLieTennisAdapter

        adapters = [BallDontLieTennisAdapter()]
    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "tennis", Sport.active.is_(True)))
        ).scalar_one_or_none()
        if sport is None:
            return
        leagues = (
            (
                await db.execute(
                    select(League).where(League.sport_id == sport.id, League.active.is_(True))
                )
            )
            .scalars()
            .all()
        )

    for league in leagues:
        try:
            await _ingest_odds_for_league(sport, league, adapters)
        except Exception:
            # WTA still 401s on every GOAT endpoint (ATP-only subscription), and one tour
            # failing must never stop the other — the same per-league isolation ingest_fixtures
            # already applies for exactly this reason.
            logger.exception("Tennis odds refresh failed for league=%s", league.slug)


# How close to kickoff the closing snapshot is taken. Short enough that the price is genuinely
# the market's last word, long enough that a 15-minute scheduler always catches the window.
CLOSING_WINDOW_START_MINUTES = 10
CLOSING_WINDOW_END_MINUTES = 45


async def _capture_closing_odds() -> None:
    """Take one last price for fixtures about to kick off, so CLV can be measured.

    Closing Line Value is the only test that distinguishes a model with an edge from one that
    merely wins short-priced favourites, and it needs the market's FINAL pre-kickoff price. The
    6-hourly job cannot supply that: whether its last run lands 10 minutes or 5 hours before
    kickoff is chance. Measured over settled fixtures, only 72 of 2,369 had any pre-kickoff
    price at all.

    COST IS PROPORTIONAL TO MATCH DAYS, NOT TO THE CLOCK. This runs every 15 minutes but makes
    ZERO API calls unless a fixture is actually kicking off in the next 10-45 minutes, and then
    asks only for that fixture's own date rather than the whole lookahead window. A naive
    every-15-minutes sweep would be ~672 requests/day against TheRundown's 5,000 per MONTH --
    the same arithmetic that caused the original weeks-long outage.

    Snapshots are appended, never updated (Odds has no unique constraint, by design), so this
    adds a row rather than overwriting the price history CLV is computed from.
    """
    now = datetime.now(UTC)
    window_start = now + timedelta(minutes=CLOSING_WINDOW_START_MINUTES)
    window_end = now + timedelta(minutes=CLOSING_WINDOW_END_MINUTES)

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Fixture.league_id, Fixture.kickoff_utc).where(
                    Fixture.status == FixtureStatus.SCHEDULED,
                    Fixture.kickoff_utc >= window_start,
                    Fixture.kickoff_utc <= window_end,
                )
            )
        ).all()
        if not rows:
            return  # nothing imminent: no request, no quota spent

        dates_by_league: dict = {}
        for league_id, kickoff in rows:
            dates_by_league.setdefault(league_id, set()).add(kickoff.date())

        leagues = (
            (await db.execute(select(League).where(League.id.in_(dates_by_league.keys()))))
            .scalars()
            .all()
        )
        sports = {
            s.id: s
            for s in (
                await db.execute(select(Sport).where(Sport.id.in_({lg.sport_id for lg in leagues})))
            )
            .scalars()
            .all()
        }

    for league in leagues:
        sport = sports.get(league.sport_id)
        if sport is None:
            continue
        try:
            await _ingest_odds_for_league(sport, league, dates=sorted(dates_by_league[league.id]))
        except Exception:
            # One league's provider failing must not cost every other league its closing
            # price - the window will have passed by the next run.
            logger.exception("Closing-odds capture failed for league=%s", league.slug)


@celery_app.task(name="app.workers.ingest_odds.capture_closing_odds")
def capture_closing_odds() -> None:
    """Every 15 minutes; free unless a fixture is imminent - see _capture_closing_odds."""
    run_task(_capture_closing_odds())


@celery_app.task(name="app.workers.ingest_odds.ingest_tennis_odds")
def ingest_tennis_odds() -> None:
    """Hourly, quota-free tennis odds refresh from BallDontLie — see _ingest_tennis_odds."""
    run_task(_ingest_tennis_odds())


@celery_app.task(name="app.workers.ingest_odds.ingest_tennis_rundown_odds")
def ingest_tennis_rundown_odds() -> None:
    """2-hourly tennis odds from TheRundown, which carries far more matches than BallDontLie.

    Separate from the task above because the two providers cost completely different things:
    one is effectively free and the other is metered at 5,000 requests/month. Running them on
    one schedule would force the cheap, broad-coverage refresh down to the expensive one's
    cadence, which is exactly the gap this closes — see _ingest_tennis_odds for the arithmetic
    behind two hours rather than one.
    """
    from app.adapters.therundown import TheRundownAdapter

    run_task(_ingest_tennis_odds([TheRundownAdapter()]))


@celery_app.task(name="app.workers.ingest_odds.ingest_odds")
def ingest_odds() -> None:
    """Celery beat triggers this every 6 hours for all active sports (TDD §2.3)."""
    run_task(_ingest_odds())
