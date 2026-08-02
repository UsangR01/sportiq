import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select

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
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

FEATURE_LOOKAHEAD_DAYS = 7
FEATURE_WINDOW_MATCHES = 10
# How far back to backfill completed fixtures for browsing/score display — symmetric with the
# forward lookahead above. Nothing ingested fixtures before this (only ever forward-looking),
# so a fresh backfill of the current window is needed once, not just going forward.
FIXTURE_HISTORY_DAYS = 7


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
                # Backfills tournament metadata onto fixtures ingested before these columns
                # existed, without clobbering real values if a later payload omits them.
                if payload.tournament_name is not None:
                    existing.tournament_name = payload.tournament_name
                if payload.tournament_surface is not None:
                    existing.tournament_surface = payload.tournament_surface
                if payload.tournament_location is not None:
                    existing.tournament_location = payload.tournament_location
                fixture = existing

            await _upsert_live_state(db, fixture.id, payload)
            await _maybe_settle_outcome(db, fixture.id, payload, home_team, away_team, sport.slug)
        await db.commit()

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
        from app.workers.run_predictions import run_predictions

        for fixture in upcoming:
            has_prediction = (
                await db.execute(select(Prediction.id).where(Prediction.fixture_id == fixture.id))
            ).first()
            if has_prediction is None:
                run_predictions.delay(str(fixture.id))

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
    """Celery beat triggers this daily at 02:00 UTC (TDD §2.3)."""
    run_task(_ingest_fixtures())
