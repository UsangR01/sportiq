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
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.adapters.base import FixturePayload
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.history.models import Outcome
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


# How long after kickoff a still-SCHEDULED fixture is treated as never having been played.
#
# Two thresholds because a placeholder kickoff carries no information about the real start.
# Tennis fixtures whose provider gives no scheduled_time are stored at midnight and flagged
# kickoff_is_estimated, so "the kickoff has passed" is true from the first second of the day
# for a match that may not begin until late evening. Sweeping those on the same clock as a
# real kickoff would mark most of a tournament abandoned every morning.
# 12 hours, not 24: live scores are polled every 5 minutes and a played match records a
# FixtureLiveState within minutes, so half a day of silence is already far beyond normal. It
# also means an evening fixture that never happened is corrected by the next morning rather
# than still showing an active pick a full day later, which is the symptom that was reported.
# The risk it accepts — mislabelling a match we simply failed to poll for 12 straight hours —
# is both unlikely and self-correcting: ingest_fixtures backfills 7 days of history, so the
# fixture is re-seen and its real status restored.
ABANDONED_AFTER_HOURS = 12

# ESTIMATED KICKOFFS ARE NO LONGER RETIRED BY THE CLOCK AT ALL, and this constant survives
# only to size the staleness warning below.
#
# The derivation was sound and the input was not. A placeholder was assumed to mean "some time
# on day D", so 24h for the day plus 6h for a late finish bounded when a real match could still
# be underway. But for tennis the placeholder is the TOURNAMENT'S START DATE -- BallDontLie's
# match object carries no date of its own, only scheduled_time (null for the overwhelming
# majority) and the tournament's start/end. So every timeless match in a ten-day draw is stored
# on day one, and a third-round match is "30 hours late" before it was ever going to be played.
#
# MEASURED 2026-08-14: nine Cincinnati fixtures were showing POSTPONED -- Djokovic, Zverev,
# Shapovalov among them. All nine were checked against the provider and all nine still existed
# as `scheduled`. Every one of those postponements was ours, not the provider's.
#
# This is the FOURTH time a clock-based rule has been wrong about a vanished fixture, and the
# threshold has been nudged twice already. The instrument is wrong, not its calibration:
# _reconcile_vanished_fixtures decides on POSITIVE EVIDENCE -- the provider's own current list
# no longer containing the fixture -- and it correctly hid 33 genuinely-withdrawn Cincinnati
# fixtures on the same day the clock invented these nine.
#
# A real kickoff time still gets the clock treatment (ABANDONED_AFTER_HOURS), because there the
# input means what it says.
ABANDONED_DAY_HOURS = 24
ABANDONED_LATE_FINISH_GRACE_HOURS = 6
ABANDONED_AFTER_HOURS_ESTIMATED = ABANDONED_DAY_HOURS + ABANDONED_LATE_FINISH_GRACE_HOURS


# How far back to keep retrying a missing corner count, and how many per run. Corner capture is
# otherwise ONE-SHOT at settlement: _maybe_settle_outcome fetches once behind its idempotency
# guard and degrades to (None, None) on any HTTP error, so a single rate limit or timeout loses
# that fixture's corners permanently. Measured 2026-08-14: 5 of 6 recently-completed football
# fixtures had no counts, and re-requesting found real data for 3 of them -- the other 2 have
# none upstream, which is a real gap rather than a missed fetch.
#
# Without a count, corners_total cannot be graded, so the card shows a neutral GREY badge on a
# finished match instead of the green tick or red cross it earned. That was the reported symptom.
CORNER_BACKFILL_LOOKBACK_DAYS = 3
CORNER_BACKFILL_MAX_PER_RUN = 25


async def _backfill_missing_corner_counts() -> None:
    """Retry the corner fetch for recently-completed football fixtures that have none.

    Bounded per run so a backlog cannot spend the day's API allowance in one sweep, and scoped
    to a few days back because a fixture whose counts were genuinely never published upstream
    should stop being asked about. A fixture the provider has no statistics for simply stays
    ungraded -- null is the honest answer, and it is what the mobile card already renders as a
    neutral badge rather than a fabricated verdict.
    """
    from app.adapters.api_football import fetch_corner_stats

    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Fixture, FixtureLiveState, Sport.slug)
                .join(Sport, Sport.id == Fixture.sport_id)
                .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(
                    Sport.slug == "football",
                    Fixture.status == FixtureStatus.COMPLETED,
                    Fixture.kickoff_utc > now - timedelta(days=CORNER_BACKFILL_LOOKBACK_DAYS),
                    FixtureLiveState.home_corners.is_(None),
                    Fixture.external_id.is_not(None),
                )
                .limit(CORNER_BACKFILL_MAX_PER_RUN)
            )
        ).all()
        if not rows:
            return

        filled = 0
        for fixture, live_state, _slug in rows:
            try:
                by_team = await fetch_corner_stats(fixture.external_id)
            except httpx.HTTPError:
                # Same reasoning as at settlement: an enrichment must not break the sweep.
                continue
            home = await _team_external_id(db, fixture.home_team_id)
            away = await _team_external_id(db, fixture.away_team_id)
            home_corners = by_team.get(str(home)) if home else None
            away_corners = by_team.get(str(away)) if away else None
            if home_corners is None or away_corners is None:
                continue
            live_state.home_corners = home_corners
            live_state.away_corners = away_corners
            filled += 1
        if filled:
            await db.commit()
            logger.info("Backfilled corner counts for %d completed fixture(s)", filled)


async def _team_external_id(db, team_id) -> str | None:
    return (
        await db.execute(select(Team.external_id).where(Team.id == team_id))
    ).scalar_one_or_none()


async def _roll_forward_stale_placeholders() -> None:
    """Move a PLACEHOLDER kickoff that has fallen into the past up to today.

    THE REPORTED SYMPTOM: nine Cincinnati fixtures -- Djokovic and Zverev among them -- sat
    under "Yesterday" showing a blue, actionable pick badge. They are real upcoming matches; the
    date was ours.

    A tennis fixture with no scheduled_time inherits the TOURNAMENT'S start date, because
    BallDontLie's match object carries no date of its own. So every timeless match in a ten-day
    draw is stamped day one, and each day that passes strands more of them further in the past
    while they are still perfectly playable.

    "Not before today" is strictly more accurate than a date already known to be wrong: the
    fixture is still SCHEDULED, has never been observed underway and has never settled, so the
    earliest it can now be played is today. kickoff_is_estimated stays TRUE, so the card still
    says Time TBC and nothing claims a start time it does not have. The placeholder simply
    follows the day forward until a real time arrives -- from the provider, or from the odds
    feed via _adopt_real_kickoff.

    Only ever moves a placeholder FORWARD, and only one whose day has already ended. A real
    kickoff is never touched: that provider owns the schedule, and a genuinely missed match
    should be retired by the clock sweep, not quietly rescheduled.
    """
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session_factory() as db:
        stale = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.status == FixtureStatus.SCHEDULED,
                        Fixture.kickoff_is_estimated.is_(True),
                        Fixture.kickoff_utc < today,
                        ~select(FixtureLiveState.fixture_id)
                        .where(FixtureLiveState.fixture_id == Fixture.id)
                        .exists(),
                        ~select(Outcome.id).where(Outcome.fixture_id == Fixture.id).exists(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not stale:
            return
        for fixture in stale:
            fixture.kickoff_utc = today
        await db.commit()
        logger.info(
            "Rolled %d placeholder kickoff(s) forward to today; they remain Time-TBC",
            len(stale),
        )


# The second corner source runs on its OWN, SLOWER schedule rather than inside the 5-minute
# sweep, and the reason is quota rather than correctness.
#
# Measured: a match 3.4 hours past kickoff was already `finished` and its statistics endpoint
# returned 404; every sampled match five or more days old carried real corners. TheStatsAPI
# publishes statistics late, and it bills against a monthly cap. Asking every five minutes
# would spend a few hundred calls per fixture learning "not yet".
#
# So: fire as soon as the data plausibly exists, then keep asking a handful of times a day
# until it does or the fixture ages out. Seven days because the measured upper bound was around
# five, with margin.
THESTATSAPI_LOOKBACK_DAYS = 7
THESTATSAPI_MAX_PER_RUN = 30
# Nothing is asked about before this: the measured 404 at 3.4h means an earlier attempt is a
# call spent to be told nothing.
THESTATSAPI_MIN_AGE_HOURS = 12


async def _backfill_corners_from_thestatsapi() -> None:
    """Second pass at the corner counts API-Football never supplied.

    Only ever fills a GAP -- a fixture that already has counts is never re-fetched and never
    overwritten, so API-Football stays the primary and this cannot silently disagree with it.
    """
    from app.adapters.thestatsapi import (
        COMPETITION_IDS,
        TheStatsAPINotConfigured,
        fetch_corners,
    )

    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Fixture, FixtureLiveState, League.slug)
                .join(Sport, Sport.id == Fixture.sport_id)
                .join(League, League.id == Fixture.league_id)
                .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(
                    Sport.slug == "football",
                    Fixture.status == FixtureStatus.COMPLETED,
                    FixtureLiveState.home_corners.is_(None),
                    FixtureLiveState.home_score.is_not(None),
                    League.slug.in_(list(COMPETITION_IDS)),
                    Fixture.kickoff_utc > now - timedelta(days=THESTATSAPI_LOOKBACK_DAYS),
                    Fixture.kickoff_utc < now - timedelta(hours=THESTATSAPI_MIN_AGE_HOURS),
                )
                .limit(THESTATSAPI_MAX_PER_RUN)
            )
        ).all()
        if not rows:
            return

        filled = 0
        for fixture, live_state, league_slug in rows:
            try:
                corners = await fetch_corners(
                    league_slug,
                    fixture.season or "",
                    fixture.kickoff_utc.date(),
                    live_state.home_score,
                    live_state.away_score,
                )
            except TheStatsAPINotConfigured as exc:
                # A deployment gap, not a provider problem, and it applies to every row -- so
                # say it once and stop rather than repeating it thirty times.
                logger.warning("%s", exc)
                return
            except httpx.HTTPError:
                continue
            if corners is None:
                continue
            live_state.home_corners, live_state.away_corners = corners
            filled += 1

        if filled:
            await db.commit()
            logger.info(
                "Filled corner counts for %d fixture(s) from TheStatsAPI that API-Football "
                "never supplied",
                filled,
            )


async def _mark_abandoned_fixtures() -> None:
    """Retire fixtures that were scheduled, never played, and have quietly vanished.

    NEITHER PROVIDER EMITS A CANCELLED STATUS FOR THESE — the row simply disappears, which is
    why nothing caught them before. Verified against both live APIs: an ATP match cancelled on
    2026-08-05 returns HTTP 404 from BallDontLie's /matches/{id}, and a Liga I fixture that
    never happened on 2026-08-10 is absent from API-Football's list for that date, which still
    returns the two matches that did. Ingest only ever updates fixtures it can still see, so a
    vanished one keeps its SCHEDULED status forever.

    The user-visible symptom is the reason this matters: days later the card still showed an
    active pick with a probability and a price, on a match that was never played. FixtureStatus
    already documents POSTPONED as the shared bucket for every non-live/non-scheduled provider
    status (postponed/cancelled/abandoned/suspended), and it already suppresses best_pick and
    renders a neutral badge, so these belong in it — no new status, no migration, and the
    display behaviour the report asked for comes for free.

    Deliberately conservative, because wrongly retiring a real upcoming fixture removes a pick
    a user could have acted on:
      - only SCHEDULED fixtures, so nothing that reached LIVE or COMPLETED is touched;
      - only with no FixtureLiveState row, i.e. never observed underway;
      - only with no Outcome row, i.e. never settled;
      - only well past kickoff, on the two thresholds above.

    Reversible by design. If the fixture reappears in the provider's feed — a postponement that
    gets rescheduled — ingest_fixtures upserts it by external_id and sets its status from the
    payload again, which moves it straight back out of POSTPONED.
    """
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        stale = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.status == FixtureStatus.SCHEDULED,
                        ~select(FixtureLiveState.fixture_id)
                        .where(FixtureLiveState.fixture_id == Fixture.id)
                        .exists(),
                        ~select(Outcome.id).where(Outcome.fixture_id == Fixture.id).exists(),
                        # A CLOCK CAN ONLY JUDGE A FIXTURE WHOSE CLOCK WE ACTUALLY KNOW.
                        # Estimated kickoffs are excluded outright -- see the block comment on
                        # ABANDONED_AFTER_HOURS_ESTIMATED for the nine real matches this
                        # invented postponements for.
                        Fixture.kickoff_is_estimated.is_(False),
                        Fixture.kickoff_utc < now - timedelta(hours=ABANDONED_AFTER_HOURS),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not stale:
            return
        for fixture in stale:
            fixture.status = FixtureStatus.POSTPONED
        await db.commit()
        logger.info(
            "Marked %d fixture(s) POSTPONED: scheduled, never played, past kickoff", len(stale)
        )

    await _warn_if_stale_fixtures_remain()


async def _warn_if_stale_fixtures_remain() -> None:
    """Report fixtures the sweep should have caught and did not.

    This exists because the sweep has now been wrong three times, each found by a user noticing
    a cancelled match still carrying a live-looking pick. Every one of those was invisible to
    us: nothing errors, nothing logs, the fixture simply sits there looking normal. A threshold
    that is too generous is indistinguishable from a working sweep unless something counts what
    is left over.

    Deliberately generous — a full day beyond the estimated threshold — so this flags a broken
    rule rather than re-flagging the grace period it is built on.
    """
    cutoff_hours = ABANDONED_AFTER_HOURS_ESTIMATED + 24
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        leftover = (
            await db.execute(
                select(func.count())
                .select_from(Fixture)
                .where(
                    Fixture.status == FixtureStatus.SCHEDULED,
                    Fixture.kickoff_utc < now - timedelta(hours=cutoff_hours),
                    ~select(FixtureLiveState.fixture_id)
                    .where(FixtureLiveState.fixture_id == Fixture.id)
                    .exists(),
                    ~select(Outcome.id).where(Outcome.fixture_id == Fixture.id).exists(),
                )
            )
        ).scalar_one()
    if leftover:
        logger.warning(
            "%d fixture(s) are still SCHEDULED more than %dh after kickoff with no live state "
            "and no outcome — the abandoned-fixture sweep is not catching them. These show a "
            "live pick on a match that was never played.",
            leftover,
            cutoff_hours,
        )


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

    # After the polling pass, so a fixture that just reported a score this cycle is already
    # LIVE/COMPLETED and can never be swept. Costs one query and no API calls.
    await _mark_abandoned_fixtures()
    await _roll_forward_stale_placeholders()
    await _backfill_missing_corner_counts()


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


@celery_app.task(name="app.workers.ingest_live_scores.backfill_corners_from_thestatsapi")
def backfill_corners_from_thestatsapi() -> None:
    """Own schedule, not the 5-minute sweep -- see THESTATSAPI_LOOKBACK_DAYS for why the
    cadence is a quota decision rather than a correctness one."""
    run_task(_backfill_corners_from_thestatsapi())
