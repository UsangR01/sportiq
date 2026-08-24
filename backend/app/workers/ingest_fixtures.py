import logging
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import FixturePayload, TeamStats
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.service import get_or_create_team
from app.history.models import MatchResult, Outcome
from app.models_ml.elo import INITIAL_ELO, apply_match_result
from app.models_ml.key_player_availability import get_key_player_availability
from app.predictions.models import Prediction
from app.sports.models import League, Sport
from app.users.models import WatchlistItem
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

FEATURE_LOOKAHEAD_DAYS = 7
FEATURE_WINDOW_MATCHES = 10
# How far back to backfill completed fixtures for browsing/score display — symmetric with the
# forward lookahead above. Nothing ingested fixtures before this (only ever forward-looking),
# so a fresh backfill of the current window is needed once, not just going forward.
FIXTURE_HISTORY_DAYS = 7

# How old a prediction for an UPCOMING fixture may be before it is regenerated.
#
# This loop recomputes TeamFeatures on every run, so a team's form, Elo, streak and rest days
# all move daily -- but a prediction used to be regenerated only when it did not exist or when
# the model version changed. A fixture predicted the moment it was first ingested therefore
# carried that number all the way to kickoff, however much better its inputs became.
#
# Measured 2026-08-18: Philadelphia Union v Inter Miami served away at 0.04, while the SAME
# model version fed that fixture's current vector returns away 0.30. Every MLS card read 1X
# above 90%, and had read the same three days earlier.
#
# 20 hours rather than 24 so a daily ingest at a slightly drifting hour still catches it, and
# rather than "always" so a re-run within the same day queues nothing. The cost is one live H2H
# call per upcoming fixture per day -- roughly 150 for football against API-Football's 75,000
# daily allowance. The original "only if no prediction exists" guard was written when that
# allowance was 7,500 and this arithmetic looked different.
PREDICTION_MAX_AGE_HOURS = 20

# Sports where a tied score cannot mean a draw. Tennis matches always have a winner, including
# retirements, where equal COMPLETED sets (6-1, 6-7, 0-2 ret.) still leave a real result. NBA
# games go to overtime rather than end level. For these, a tie means the winner is not
# derivable from the score, which is a different statement from "the match was drawn" — see
# _maybe_settle_outcome.
SPORTS_WITHOUT_DRAWS = {"tennis", "nba"}


async def _upsert_live_state(db, fixture_id, payload: FixturePayload) -> None:
    """Real score storage for both in-progress and completed fixtures — FixtureLiveState
    already had the right shape (home_score/away_score/match_minute/period/status/
    last_updated_utc) for this, just nothing ever wrote to it (TDD's live-scores ingestion was
    a documented gap — see app/workers/ingest_live_scores.py). A completed fixture's row
    simply stops being updated once the game ends, which is exactly the desired "final score"
    behavior — no separate settlement step needed just to show a number inline."""
    if payload.home_score is None and payload.away_score is None:
        return

    live_state = (
        await db.execute(select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id))
    ).scalar_one_or_none()
    if live_state is None:
        live_state = FixtureLiveState(fixture_id=fixture_id, home_score=0, away_score=0, status="")
        db.add(live_state)

    live_state.home_score = payload.home_score or 0
    live_state.away_score = payload.away_score or 0
    live_state.match_minute = payload.match_minute
    live_state.status = payload.status
    live_state.result_type = payload.result_type
    live_state.last_updated_utc = datetime.now(UTC)


async def _maybe_fetch_corner_stats(
    sport_slug: str, payload: FixturePayload, home_team: Team, away_team: Team
) -> tuple[int | None, int | None]:
    """Football only — one real, one-off API call for this fixture's final corner-kick
    counts, so the Over/Under corners market can show a real win/loss verdict (previously
    permanently unverifiable — see CLAUDE.md). Only ever called once per fixture, from
    _maybe_settle_outcome's own idempotency guard. Failures (a very old fixture whose stats
    are no longer served, a transient API error) degrade to (None, None) — a real, honest
    gap, not a fabricated 0 — rather than blocking Outcome/Elo settlement, which must still
    happen regardless."""
    if sport_slug != "football" or not home_team.external_id or not away_team.external_id:
        return None, None
    from app.adapters.api_football import fetch_corner_stats

    try:
        corners_by_team = await fetch_corner_stats(payload.external_id)
    except httpx.HTTPError:
        return None, None
    return corners_by_team.get(home_team.external_id), corners_by_team.get(away_team.external_id)


async def _maybe_settle_outcome(
    db, fixture_id, payload: FixturePayload, home_team: Team, away_team: Team, sport_slug: str
) -> None:
    """Writes a real settled Outcome row once a fixture completes — the outcomes table TDD's
    own schema already defines but nothing has ever written to (GET /history's real blocker
    per CLAUDE.md: "no settled outcomes exist"). Idempotent: both this worker's daily backfill
    and ingest_live_scores.py's 5-minute poll can observe the same fixture completing, so this
    only ever inserts once. Aggregating these into /history itself (real model-performance
    rollups) is a separate, larger task — not attempted here, just unblocking the raw data.

    Also updates both teams' real, persistent Elo rating (app/models_ml/elo.py), and (football
    only) fetches real final corner-kick counts onto FixtureLiveState — both exactly once per
    real completed match, since this idempotency check is the natural single hook point for
    settlement-time side effects that would otherwise double-apply across the same
    daily-backfill + live-poll overlap this function already guards against for the Outcome
    row."""
    if payload.status != "completed" or payload.home_score is None or payload.away_score is None:
        return
    existing = (
        await db.execute(select(Outcome).where(Outcome.fixture_id == fixture_id))
    ).scalar_one_or_none()
    if existing is not None:
        return

    if payload.home_score > payload.away_score:
        result = MatchResult.HOME_WIN
    elif payload.away_score > payload.home_score:
        result = MatchResult.AWAY_WIN
    elif sport_slug in SPORTS_WITHOUT_DRAWS:
        # A tied score in a sport that cannot draw means the winner is not derivable from the
        # score, NOT that the match was drawn. In tennis it happens on a retirement: 6-1, 6-7,
        # 0-2 ret. is 1-1 in completed sets and still has a real winner (see
        # balldontlie_tennis._is_completed_set). Writing DRAW recorded a result that cannot
        # exist -- 12 such rows accumulated before this guard.
        #
        # Nothing is written, deliberately. The absent Outcome is the honest state and leaves
        # the match settleable later if a real winner signal appears; a wrong one would have to
        # be found and corrected first. It also means this is retried on each ingest, which is
        # cheap (no API call) and self-healing.
        #
        # The provider offers no usable winner here. BallDontLie exposes no retirement marker,
        # and its habit of listing the winner as player1 -- 100% on settled 2022/2025 data via
        # the list endpoint -- does NOT hold where it would be needed: 68% on the current
        # season and 48% via /matches/{id}. Measured, not assumed.
        logger.info(
            "Not settling an outcome for %s fixture %s: score %s-%s is tied in a sport with no "
            "draw, so the winner is not derivable",
            sport_slug,
            fixture_id,
            payload.home_score,
            payload.away_score,
        )
        return
    else:
        result = MatchResult.DRAW
    db.add(
        Outcome(
            fixture_id=fixture_id,
            home_score=payload.home_score,
            away_score=payload.away_score,
            result=result,
            settled_at=datetime.now(UTC),
        )
    )

    elo_home = home_team.elo_rating if home_team.elo_rating is not None else INITIAL_ELO
    elo_away = away_team.elo_rating if away_team.elo_rating is not None else INITIAL_ELO
    home_team.elo_rating, away_team.elo_rating = apply_match_result(
        elo_home, elo_away, payload.home_score, payload.away_score
    )

    home_corners, away_corners = await _maybe_fetch_corner_stats(
        sport_slug, payload, home_team, away_team
    )
    if home_corners is not None or away_corners is not None:
        live_state = (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id)
            )
        ).scalar_one_or_none()
        if live_state is not None:
            live_state.home_corners = home_corners
            live_state.away_corners = away_corners


def _utc_date(moment: datetime) -> date:
    """The UTC calendar date of a kickoff, tolerating a naive value.

    Rows written before the column was consistently tz-aware come back naive; treating those as
    UTC matches how every writer here stores them.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()


async def _reconcile_vanished_fixtures(
    db: AsyncSession, sport: Sport, league: League, payloads: list[FixturePayload]
) -> int:
    """Retire fixtures the provider has STOPPED returning, using its own current list as proof.

    THIS IS THE SAME FAILURE ingest_live_scores._mark_abandoned_fixtures exists for, caught by
    the opposite kind of evidence, because that sweep structurally cannot see this case. It
    infers absence from ELAPSED TIME — a fixture is retired once its kickoff is 12 hours past
    (30 for a Time-TBC placeholder). A fixture that vanishes while its kickoff is still in the
    FUTURE is therefore invisible to it, and stays a live-looking pick until the clock catches
    up. Measured 2026-08-12: 33 of the 53 upcoming ATP fixtures returned HTTP 404 from
    BallDontLie, every one of them carrying a prediction, all dated the following day. They
    were a provisional Cincinnati Round-of-128 draw the provider withdrew and replaced — the
    ids form one contiguous block, and several pairings were simply wrong (we held Baez v
    Goffin; the real match is Baez v Dimitrov). The time-based sweep would not have touched
    them until ~30 hours after a kickoff that had not happened yet, by which point they had
    already been the majority of a day's feed.

    Absence from the payload is a POSITIVE signal, not an inference, so it fires immediately.
    Three guards keep it from becoming the more dangerous opposite error — retiring real
    upcoming fixtures, which deletes picks a user could have acted on:

      1. An EMPTY payload retires nothing. A rate-limited or failed fetch returns an empty
         list that reads exactly like "this league has no fixtures", and CLAUDE.md already
         records that false negative poisoning a diagnosis. There is no partial-payload case
         to worry about underneath this: neither adapter swallows a per-tournament or per-date
         error, so a failure propagates out of fetch_fixtures and this is never reached.
      2. Only kickoff DATES the provider demonstrably reported on. A date it returned nothing
         for is not evidence of absence, and is left to the time-based sweep as before.
      3. The same conservative conditions as that sweep — SCHEDULED only, never observed
         underway (no FixtureLiveState), never settled (no Outcome).

    Reversible, like the time-based sweep: the update branch above sets status straight from
    the payload, so a fixture that reappears moves back out of POSTPONED on the next ingest.
    """
    seen = {payload.external_id for payload in payloads}
    if not seen:
        return 0
    covered_dates = {_utc_date(payload.kickoff_utc) for payload in payloads}

    candidates = (
        (
            await db.execute(
                select(Fixture).where(
                    Fixture.sport_id == sport.id,
                    Fixture.league_id == league.id,
                    Fixture.status == FixtureStatus.SCHEDULED,
                    Fixture.external_id.notin_(seen),
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
    gone = [f for f in candidates if _utc_date(f.kickoff_utc) in covered_dates]
    if not gone:
        return 0

    for fixture in gone:
        fixture.status = FixtureStatus.POSTPONED
        # Distinct from a provider-stated postponement, which stays visible: this one was
        # never a real scheduled match, so the feed hides it. See Fixture.withdrawn.
        fixture.withdrawn = True
    await db.commit()
    # WARNING, not INFO, so it reaches Sentry. Every previous instance of this class of bug was
    # found by a user rather than by us, because nothing errors and nothing logs — the fixture
    # simply sits there looking normal.
    logger.warning(
        "Retired %d fixture(s) for league=%s: still SCHEDULED but no longer in the provider's "
        "own list for a date it did report on",
        len(gone),
        league.slug,
    )
    return len(gone)


async def _active_model_version(db: AsyncSession, sport_id) -> str | None:
    """The version currently serving this sport, or None if nothing is promoted yet.

    Read from models_registry rather than from a loaded model, because promotion here is a DB
    update rather than a deploy (TDD §3.1) — so the registry is the authority on what a fresh
    prediction would be stamped with, and comparing against it is what makes a retrain
    propagate to already-predicted fixtures.
    """
    from app.predictions.models import ModelRegistry

    return (
        (
            await db.execute(
                select(ModelRegistry.version).where(
                    ModelRegistry.sport_id == sport_id, ModelRegistry.is_active.is_(True)
                )
            )
        )
        .scalars()
        .first()
    )


async def _ingest_fixtures_for_league(sport: Sport, league: League) -> None:
    # NOTE: TDD §6.2 references a sports.data_source_slug column that isn't in the §2.1 schema
    # listing. Using sport.slug directly as the AdapterFactory key until that's reconciled.
    adapter = AdapterFactory.get_stats_adapter(sport.slug)

    async with async_session_factory() as db:
        fixture_payloads = await adapter.fetch_fixtures(
            sport=sport.slug,
            league=league.slug,
            days_ahead=FEATURE_LOOKAHEAD_DAYS,
            days_back=FIXTURE_HISTORY_DAYS,
        )

        for payload in fixture_payloads:
            # Dedupe on the provider's own fixture ID, not the internal UUID PK — matching on
            # Fixture.id here would never hit (see CLAUDE.md for why this was previously wrong).
            existing = (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == sport.id, Fixture.external_id == payload.external_id
                    )
                )
            ).scalar_one_or_none()

            home_team = await get_or_create_team(
                db,
                sport_id=sport.id,
                league_id=league.id,
                external_id=payload.home_team_external_id,
                name=payload.home_team_name or payload.home_team_external_id,
                short_name=payload.home_team_short_name,
            )
            away_team = await get_or_create_team(
                db,
                sport_id=sport.id,
                league_id=league.id,
                external_id=payload.away_team_external_id,
                name=payload.away_team_name or payload.away_team_external_id,
                short_name=payload.away_team_short_name,
            )

            if existing is None:
                fixture = Fixture(
                    sport_id=sport.id,
                    league_id=league.id,
                    external_id=payload.external_id,
                    home_team_id=home_team.id,
                    away_team_id=away_team.id,
                    kickoff_utc=payload.kickoff_utc,
                    status=FixtureStatus(payload.status),
                    season=payload.season,
                    tournament_name=payload.tournament_name,
                    tournament_surface=payload.tournament_surface,
                    tournament_location=payload.tournament_location,
                    tournament_end_utc=payload.tournament_end_utc,
                    kickoff_is_estimated=payload.kickoff_is_estimated,
                )
                db.add(fixture)
                await db.flush()  # populate fixture.id for the live-state upsert below
            else:
                # TDD §2.3's "cancelled/postponed fixtures are updated" is now real for
                # football (see app/adapters/api_football.py:_map_status) via FixtureStatus.
                # POSTPONED — a single shared bucket for every non-live/non-scheduled provider
                # status, not one enum value per real-world reason. BallDontLie has no
                # equivalent signal at all (its status field is a free-form string with no
                # postponed marker — see balldontlie.py:_map_status), so NBA fixtures can still
                # only ever transition scheduled -> live -> completed.
                new_status = FixtureStatus(payload.status)
                if existing.status != new_status:
                    existing.status = new_status
                # THE PLAYERS CAN CHANGE UNDER A FIXED MATCH ID, and they were written on
                # INSERT only -- so whichever pairing we saw first stuck forever.
                #
                # Reported as "the players don't match the games for today". Measured against
                # the live provider: we were showing Majchrzak v Jarry where the provider had
                # Majchrzak v BONZI, and Comesana v Bellucci where it had Comesana v YIBING WU
                # -- the same match id, the same player on one side, a different opponent on
                # the other. A qualifying draw is published provisionally and then filled in as
                # earlier rounds settle, and BallDontLie updates the existing record rather
                # than issuing a new one, so _reconcile_vanished_fixtures could never see it
                # either: the id is still in the payload, so the fixture is not "vanished".
                #
                # Assigned unconditionally rather than guarded on a change: the ids come from
                # get_or_create_team above, so an unchanged pairing is a no-op write, and any
                # condition here would be another place for this to silently stop happening.
                existing.home_team_id = home_team.id
                existing.away_team_id = away_team.id
                # The provider is listing it again, so whatever caused it to be treated as
                # withdrawn no longer holds. Clearing here is what makes _reconcile_vanished_
                # fixtures reversible — a withdrawn draw CAN be republished, and a fixture that
                # could never lose this flag would stay hidden from the feed forever.
                existing.withdrawn = False
                # Backfills tournament metadata onto fixtures ingested before these columns
                # existed, without clobbering real values if a later payload omits them.
                if payload.tournament_name is not None:
                    existing.tournament_name = payload.tournament_name
                if payload.tournament_surface is not None:
                    existing.tournament_surface = payload.tournament_surface
                if payload.tournament_location is not None:
                    existing.tournament_location = payload.tournament_location
                if payload.tournament_end_utc is not None:
                    existing.tournament_end_utc = payload.tournament_end_utc

                # Refresh the kickoff time, which used to be written on INSERT only.
                #
                # Tennis exposed why that mattered: BallDontLie leaves scheduled_time null
                # until close to the match, so a fixture ingested days ahead fell back to its
                # tournament's start date (midnight) and kept that forever — even after the
                # provider published a real time. Measured live, 26 of 37 ATP fixtures in a
                # +/-2 day window were stuck on an estimated kickoff and 31 sat at exactly
                # 00:00, which put matches on the wrong DAY in the feed entirely.
                #
                # Only ever move to a REAL time: an estimate must not overwrite a known
                # kickoff, or a re-ingest that happens to lose the field would drag a correct
                # time back to midnight. Once a real time is known the fixture stops being
                # estimated; while it is still estimated the flag follows the payload, so a
                # fixture cannot silently drop its "Time TBC" label while keeping a made-up
                # time — which is what would have happened here, and is worse than admitting
                # the time is unknown.
                if not payload.kickoff_is_estimated:
                    existing.kickoff_utc = payload.kickoff_utc
                    existing.kickoff_is_estimated = False
                elif existing.kickoff_is_estimated:
                    existing.kickoff_utc = payload.kickoff_utc
                    existing.kickoff_is_estimated = True
                fixture = existing

            await _upsert_live_state(db, fixture.id, payload)
            await _maybe_settle_outcome(db, fixture.id, payload, home_team, away_team, sport.slug)
        await db.commit()

        # Runs BEFORE the feature/prediction loop below so a fixture that no longer exists is
        # already POSTPONED by the time that loop selects its work — no features computed and
        # no prediction queued for a match that is not going to be played.
        await _reconcile_vanished_fixtures(db, sport, league, fixture_payloads)

        # Team feature vectors, computed at ingest time for not-yet-played fixtures (TDD
        # §2.3) — last 10 matches, xG, H2H via the stats adapter. Completed fixtures (now
        # real thanks to the backfill above) don't need pre-game features or a prediction —
        # skipping them avoids wasted fetch_team_stats calls on games that already happened.
        # POSTPONED fixtures are excluded for the same reason: there's no game to build a
        # pre-game feature vector or prediction for until/unless it's rescheduled.
        upcoming = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.league_id == league.id,
                        Fixture.status.notin_([FixtureStatus.COMPLETED, FixtureStatus.POSTPONED]),
                    )
                )
            )
            .scalars()
            .all()
        )
        # A team appears in many fixtures within one run (every fixture it plays in this
        # league), and fetch_team_stats is expensive/rate-limited — cache per external_id so
        # each team is only fetched once per run instead of once per (fixture, team) pair.
        team_stats_cache: dict[str, TeamStats] = {}
        for fixture in upcoming:
            for team_id in (fixture.home_team_id, fixture.away_team_id):
                # fetch_team_stats needs the provider's own team ID, not our internal UUID —
                # passing team_id directly here would silently query the real API with a
                # UUID it doesn't recognise (caught while writing this, not from experience).
                team = (
                    await db.execute(select(Team).where(Team.id == team_id))
                ).scalar_one_or_none()
                if team is None or team.external_id is None:
                    continue
                if team.external_id not in team_stats_cache:
                    team_stats_cache[team.external_id] = await adapter.fetch_team_stats(
                        team.external_id, n_matches=FEATURE_WINDOW_MATCHES, league=league.slug
                    )
                stats = team_stats_cache[team.external_id]

                # Stage 2 of TDD §3.3's key-player availability feature — sport-agnostic (see
                # app/models_ml/key_player_availability.py). Reads exclusively from
                # player_injury_status, never a box score; returns (None, None) on its own for
                # any team/season Stage 1 never ran for, so no per-sport gate is needed here.
                key_players_available, key_players_per_combined = await get_key_player_availability(
                    db, team_id, int(fixture.season)
                )

                # Re-running this worker (daily, per TDD §2.3) for the same not-yet-played
                # fixture previously inserted a brand-new TeamFeatures row every time — no
                # dedup existed at all, so a fixture ingested repeatedly over several days
                # before kickoff would accumulate multiple rows and _run_predictions.py's
                # .scalar_one_or_none() lookup would eventually raise MultipleResultsFound.
                # Delete-then-insert per (team_id, fixture_id), same idiom already used for
                # team_key_players.
                await db.execute(
                    delete(TeamFeatures).where(
                        TeamFeatures.team_id == team_id, TeamFeatures.fixture_id == fixture.id
                    )
                )
                # team.elo_rating is the real, persistent, incrementally-updated value (see
                # app/models_ml/elo.py) — stats.elo_rating from the stats adapter is always
                # None (no provider carries Elo), so this is the only real source. A team with
                # no completed, Elo-tracked match yet has never had this column touched
                # (stays None) — genuinely missing, not defaulted to INITIAL_ELO here, so the
                # model sees an honest gap rather than a fabricated neutral rating.
                db.add(
                    TeamFeatures(
                        team_id=team_id,
                        fixture_id=fixture.id,
                        elo_rating=team.elo_rating,
                        attack_str=stats.attack_str,
                        defence_str=stats.defence_str,
                        form_pts_5=stats.form_pts_5,
                        xg_for_5=stats.xg_for_5,
                        xg_against_5=stats.xg_against_5,
                        days_since_last_match=stats.days_since_last_match,
                        home_win_rate=stats.home_win_rate,
                        away_win_rate=stats.away_win_rate,
                        season_point_diff=stats.season_point_diff,
                        key_players_available=key_players_available,
                        key_players_per_combined=key_players_per_combined,
                        win_streak=stats.win_streak,
                        losing_streak=stats.losing_streak,
                        rank_points=stats.rank_points,
                        rank_position=stats.rank_position,
                    )
                )
        await db.commit()

        # A freshly-ingested fixture never got a prediction of its own before this — the only
        # existing trigger was ingest_injuries.py's re-inference path, which fires just for a
        # real key-player status change within 3 hours of kickoff, not for an ordinary new
        # fixture. Confirmed live via the user's own report ("no prediction made at all" for
        # most MLS/Scottish Premiership fixtures): only the single fixture manually
        # spot-checked per league during that feature's verification had a real Prediction
        # row — every other upcoming fixture had none, which is exactly why the Picks feed
        # showed almost nothing for those leagues (not a probability/odds threshold issue at
        # all). Only queues for a fixture with NO prediction yet — never re-queues one that
        # already has a real prediction, so a daily re-run of this worker doesn't waste real
        # H2H/moneyline API calls recomputing predictions whose features haven't materially
        # changed since kickoff is still days out.
        # ...AND when the prediction it already has came from a SUPERSEDED model.
        #
        # "No prediction yet" alone meant a retrain never reached a single user. Measured
        # 2026-08-13: all 135 upcoming football fixtures were serving predictions from one of
        # FOUR superseded versions, the newest of them three retrains old — including the
        # rolling-window change adopted the day before. Nothing failed and nothing logged; the
        # models were trained, registered and activated, and the feed simply went on showing
        # the old numbers. That is the same shape as the stale-worker and served-but-untrained
        # traps already recorded here: work completed, never delivered.
        #
        # Cost stays bounded because this fires on a VERSION CHANGE, not on a schedule — a
        # routine daily re-run still queues nothing, which is what the original guard was
        # protecting. Only a promotion causes the sweep, and a promotion is exactly when every
        # served number is out of date.
        from app.workers.run_predictions import run_predictions

        active_version = await _active_model_version(db, sport.id)
        cutoff = datetime.now(UTC) - timedelta(hours=PREDICTION_MAX_AGE_HOURS)
        for fixture in upcoming:
            existing = (
                await db.execute(
                    select(Prediction.model_version, Prediction.created_at)
                    .where(Prediction.fixture_id == fixture.id)
                    .order_by(Prediction.created_at.desc())
                )
            ).first()
            if existing is None:
                run_predictions.delay(str(fixture.id))
                continue
            existing_version, created_at = existing
            superseded = active_version is not None and existing_version != active_version
            # THE FEATURES MOVE EVERY DAY AND THE PREDICTION DID NOT. This loop rewrites
            # TeamFeatures on every run, but until 2026-08-18 a prediction was only ever
            # regenerated when it did not exist or when the MODEL VERSION changed -- so a
            # fixture predicted the moment it was first ingested kept that number until
            # kickoff, however much better its features became.
            #
            # Reported as MLS cards all showing 1X above 90%. Philadelphia Union v Inter
            # Miami served away at 0.04; fed the SAME fixture's current vector, the same
            # model version returns away 0.30. Nothing was wrong with the model or the
            # features — only with when the prediction had been taken. It had looked the
            # same three days earlier, which is the tell.
            outdated = created_at is not None and created_at < cutoff
            if superseded or outdated:
                run_predictions.delay(str(fixture.id))

        await _queue_kickoff_reminders(db, upcoming)

    # Retrodicted predictions for newly-backfilled completed fixtures (TDD has no equivalent
    # step — added so the Home feed can show "what the model would have called" alongside a
    # real final score). Football and tennis for now — see app/workers/backfill_predictions.py
    # / backfill_tennis_predictions.py's module docstrings for the leakage-safety design and
    # why this is deliberately not the same code path as live inference.
    if sport.slug == "football":
        from app.workers.backfill_predictions import _retrodict_league

        await _retrodict_league(sport, league)
    if sport.slug == "tennis":
        from app.workers.backfill_tennis_predictions import _retrodict_tennis_league

        await _retrodict_tennis_league(sport, league)
    if sport.slug == "nba":
        # Basketball had no retrodiction path at all until 2026-08-15, so a fixture first seen
        # already-finished could never get a prediction and its card stayed blank forever.
        # Measured: 23 completed NBA/WNBA fixtures in that state in production.
        from app.workers.backfill_basketball_predictions import _retrodict_basketball_league

        await _retrodict_basketball_league(sport, league)


async def _ingest_fixtures() -> None:
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
            # confirmed, see CLAUDE.md) must never block every OTHER league's daily ingest —
            # same per-league isolation principle ingest_odds.py already applies per-adapter,
            # applied here since this loop has the identical "one failure kills the rest of
            # the run" shape ingest_live_scores.py's own loop had before this fix. ValueError
            # is also caught (not just httpx.HTTPError): AdapterFactory.get_stats_adapter
            # raises it for any sport with no registered adapter at all.
            try:
                await _ingest_fixtures_for_league(sport, league)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Fixture ingest failed for sport=%s league=%s (%s) — skipping, other "
                    "leagues unaffected",
                    sport.slug,
                    league.slug,
                    exc,
                )


@celery_app.task(name="app.workers.ingest_fixtures.ingest_fixtures")
def ingest_fixtures() -> None:
    """Celery beat triggers this daily at 02:00 UTC (TDD §2.3).

    FANS OUT ONE TASK PER LEAGUE rather than walking all of them in this process, and the
    reason is memory rather than tidiness.

    Measured 2026-08-24: ingesting a SINGLE league peaks around 359MB — fixtures, features and,
    for tennis, the cached history retrodiction reads. The worker sits at ~278MB once a model
    is loaded, on a 512MB instance. Doing 24 leagues back to back in one process therefore ran
    it out of memory partway through, and an OOM kill is silent: the container restarts, the
    task is simply gone, the queue drains, and nothing has changed.

    That was not hypothetical. A manually enqueued run drained the queue and altered nothing --
    no fixture updated, no prediction regenerated, no error anywhere -- while football's 22
    leagues were processed ahead of tennis and never reached it. The nightly run had almost
    certainly been dying the same way.

    Per league, each task starts fresh and its memory is released when it ends. It also means
    one league's failure can no longer take the rest of the run with it, which the inner
    try/except was already trying to achieve and could not once the process itself died.
    """
    run_task(_fan_out_league_ingests())


async def _fan_out_league_ingests() -> None:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Sport.slug, League.slug)
                .join(League, League.sport_id == Sport.id)
                .where(Sport.active.is_(True), League.active.is_(True))
            )
        ).all()
    for sport_slug, league_slug in rows:
        ingest_fixtures_for_league.delay(sport_slug, league_slug)
    logger.info("Fanned out %d per-league fixture ingests", len(rows))


@celery_app.task(name="app.workers.ingest_fixtures.ingest_fixtures_for_league")
def ingest_fixtures_for_league(sport_slug: str, league_slug: str) -> None:
    """One league, so the process's peak memory is one league's worth.

    Also the manual entry point: enqueueing a single league is what you want when a specific
    competition needs re-ingesting, rather than paying for all 24.
    """
    run_task(_ingest_one_league(sport_slug, league_slug))


async def _ingest_one_league(sport_slug: str, league_slug: str) -> None:
    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == sport_slug))
        ).scalar_one_or_none()
        league = (
            await db.execute(
                select(League).where(
                    League.sport_id == sport.id if sport else False,
                    League.slug == league_slug,
                )
            )
        ).scalar_one_or_none()
    if sport is None or league is None:
        logger.warning("No active sport/league for %s/%s", sport_slug, league_slug)
        return
    await _ingest_fixtures_for_league(sport, league)


# How long before kickoff the reminder fires (TDD §5.4).
KICKOFF_REMINDER_MINUTES = 60


async def _queue_kickoff_reminders(db: AsyncSession, upcoming: list[Fixture]) -> None:
    """Schedule the T-60 push for any watched fixture that has not been reminded yet.

    Queued with Celery's `eta` rather than polled, so one task sleeps until its own kickoff
    instead of a scheduler waking every few minutes to ask whether anything is due.

    Only fixtures somebody actually saved are queued: a task per fixture regardless would be
    thousands of no-ops a day, since the overwhelming majority of fixtures are on nobody's
    watchlist. reminded_at is the idempotency guard at SEND time; this is the cheaper guard at
    QUEUE time, so a daily re-run does not stack duplicate etas for the same fixture.
    """
    if not upcoming:
        return

    now = datetime.now(UTC)
    by_id = {f.id: f for f in upcoming}
    watched = (
        (
            await db.execute(
                select(WatchlistItem.fixture_id)
                .where(
                    WatchlistItem.fixture_id.in_(by_id.keys()),
                    WatchlistItem.reminded_at.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    for fixture_id in watched:
        schedule_kickoff_reminder(by_id[fixture_id], now=now)


def schedule_kickoff_reminder(fixture: Fixture, now: datetime | None = None) -> bool:
    """Queue one fixture's T-60 reminder. Returns whether it was actually queued.

    Shared by the daily sweep above and by POST /user/watchlist, which needs it because the
    sweep alone leaves a real gap: it runs once a day at 02:00 UTC, so a fixture saved during
    the day for a match later the same day or the next morning would never be queued at all,
    and the reminder would simply not arrive. Saving is exactly when a user expects the
    reminder to be armed.

    Safe to call more than once for the same fixture: the send path's reminded_at guard means a
    duplicate eta notifies nobody twice.
    """
    from app.workers.notify_users import notify_kickoff_reminder

    now = now or datetime.now(UTC)
    # An estimated kickoff is a date, not a time — "starts in an hour" off a midnight
    # placeholder would be wrong, so it is skipped here as well as at send time.
    if fixture.kickoff_is_estimated or fixture.kickoff_utc is None:
        return False
    if fixture.status is not FixtureStatus.SCHEDULED:
        return False
    kickoff = fixture.kickoff_utc
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=UTC)
    eta = kickoff - timedelta(minutes=KICKOFF_REMINDER_MINUTES)
    # Already inside the window (a fixture saved 20 minutes before kickoff): send now rather
    # than scheduling an eta in the past, which Celery would run immediately anyway but less
    # legibly. Past kickoff entirely, and there is nothing to remind anyone about.
    if kickoff <= now:
        return False
    notify_kickoff_reminder.apply_async(args=[str(fixture.id)], eta=eta if eta > now else None)
    return True
