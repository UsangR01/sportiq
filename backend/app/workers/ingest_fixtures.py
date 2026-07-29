import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.adapters.base import FixturePayload, TeamStats
from app.adapters.factory import AdapterFactory
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team, TeamFeatures
from app.fixtures.service import get_or_create_team
from app.history.models import MatchResult, Outcome
from app.models_ml.key_player_availability import get_key_player_availability
from app.sports.models import League, Sport
from app.workers.celery import celery_app

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
    live_state.last_updated_utc = datetime.now(UTC)


async def _maybe_settle_outcome(db, fixture_id, payload: FixturePayload) -> None:
    """Writes a real settled Outcome row once a fixture completes — the outcomes table TDD's
    own schema already defines but nothing has ever written to (GET /history's real blocker
    per CLAUDE.md: "no settled outcomes exist"). Idempotent: both this worker's daily backfill
    and ingest_live_scores.py's 5-minute poll can observe the same fixture completing, so this
    only ever inserts once. Aggregating these into /history itself (real model-performance
    rollups) is a separate, larger task — not attempted here, just unblocking the raw data."""
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
                )
                db.add(fixture)
                await db.flush()  # populate fixture.id for the live-state upsert below
            else:
                # TODO: TDD §2.3 says "cancelled/postponed fixtures are updated", but the
                # fixtures.status enum in §2.1 only defines scheduled|live|completed — no
                # cancelled/postponed value. Status transitions we DO support get applied here.
                new_status = FixtureStatus(payload.status)
                if existing.status != new_status:
                    existing.status = new_status
                fixture = existing

            await _upsert_live_state(db, fixture.id, payload)
            await _maybe_settle_outcome(db, fixture.id, payload)
        await db.commit()

        # Team feature vectors, computed at ingest time for not-yet-played fixtures (TDD
        # §2.3) — last 10 matches, xG, H2H via the stats adapter. Completed fixtures (now
        # real thanks to the backfill above) don't need pre-game features or a prediction —
        # skipping them avoids wasted fetch_team_stats calls on games that already happened.
        upcoming = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.league_id == league.id,
                        Fixture.status != FixtureStatus.COMPLETED,
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
                db.add(
                    TeamFeatures(
                        team_id=team_id,
                        fixture_id=fixture.id,
                        elo_rating=stats.elo_rating,
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
                    )
                )
        await db.commit()

    # Retrodicted predictions for newly-backfilled completed fixtures (TDD has no equivalent
    # step — added so the Home feed can show "what the model would have called" alongside a
    # real final score). Football only for now — see
    # app/workers/backfill_predictions.py's module docstring for the leakage-safety design and
    # why this is deliberately not the same code path as live inference.
    if sport.slug == "football":
        from app.workers.backfill_predictions import _retrodict_league

        await _retrodict_league(sport, league)


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
            await _ingest_fixtures_for_league(sport, league)


@celery_app.task(name="app.workers.ingest_fixtures.ingest_fixtures")
def ingest_fixtures() -> None:
    """Celery beat triggers this daily at 02:00 UTC (TDD §2.3)."""
    asyncio.run(_ingest_fixtures())
