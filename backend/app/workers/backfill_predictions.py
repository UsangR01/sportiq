"""Retrodicted predictions for completed fixtures (football only, for now) — added so the
Home feed can show "what the model would have called" alongside a real final score, not just
for upcoming fixtures.

Deliberately NOT the same code path as run_predictions.py's live inference: that assembles
features from TeamFeatures (a live snapshot of "current" team form/win-rate/etc, computed at
ingest time), which for a past game reflects form as of NOW rather than as of the game's
actual kickoff — using it would leak information from every game that happened AFTER the one
being predicted, including the game's own eventual outcome once enough time has passed.

Rebuilt (see CLAUDE.md) to use ALL real available historical data for the season, not just our
own DB's thin ~7-day fixture window: the same multi-season parquet cache
ml/training/train_football.py trains on (ml/data/football_game_log_{league}.parquet, one real
season set per league — currently EPL and Brasileirão, see collect_football_data.py) is loaded
here as the base game log, with any of our own DB's completed fixtures not already present in
that cache appended on top (keeps retrodiction fresh between historical-collection runs without
needing to re-run the full collection). Leagues with no cached parquet yet (the other 3
European leagues) fall back to DB-only history — thinner, but still real, same honest gap
pattern as before this rebuild.

Real, honest limitation, reduced but not eliminated by the above: a brand-new league/team with
no cached historical seasons AND little DB history yet still gets sparse/None rolling-form
features — the model's own missing-data handling (never a fabricated neutral value) covers it.

Key-player availability now uses REAL BOX-SCORE/LINEUP PRESENCE (who literally played), not
Stage 2's pre-game player_injury_status — legitimate here specifically because a retrodicted
fixture's outcome is already known and this is a one-off backtest-style prediction, never a
forecast (explicit user go-ahead; adopted from the Big5/Big3 approach in the user's own prior
NBA notebook). See app/models_ml/historical_key_players.py's module docstring for why this is
a clearly separate code path from the live Stage 2 lookup, mirroring the same
historical-label-vs-live-Stage-2 separation already established for NBA
(ml/training/train_nba.py vs app/models_ml/key_player_availability.py). Lineup presence is read
from the cached parquet ONLY -- the live per-fixture fetch was removed 2026-08-16 because the
four key-player features it feeds were pruned from the model vector in d0a24d9, so it spent a
real API call per retrodicted fixture to compute values nothing reads, and added a network
failure to a loop whose failures used to discard a whole league.

elo_diff now uses the same real, persistent Elo state serving does (app/models_ml/elo.py,
Team.elo_rating) rather than being omitted — a completed fixture's two teams' CURRENT Elo
overstates what they were rated at kickoff (some drift from later real matches), a small,
accepted imprecision given Elo moves gradually and this is a one-off backtest display value,
not a forecast being staked on.

Moneyline-implied-probability stays None here: no historical odds exist for arbitrary past DB
fixtures beyond EPL's bounded training-time sample (see collect_football_data.py), which isn't
indexed for point lookups by fixture — a real, same-shaped gap as before this rebuild.

Known follow-up, not attempted here: existing Prediction rows created by the OLD, thinner
version of this worker (before this rebuild) are left as-is — _retrodict_league only fills
fixtures with NO prediction at all, and there's no schema field distinguishing "a real live
pre-game forecast" from "an old thin retrodiction" for an already-completed fixture, so
blindly regenerating every existing row risked silently discarding genuine pre-game forecasts
along with the ones actually worth upgrading. A real backfill of specifically the old-style
retrodictions would need that provenance distinction added first.
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.elo import compute_elo_history
from app.models_ml.football_features import assemble_from_game_log
from app.models_ml.historical_key_players import (
    historical_key_player_availability,
    load_team_key_players_by_team_season,
)
from app.models_ml.runner import ModelRunner
from app.predictions.models import Prediction, PredictionKind
from app.predictions.service import confidence_tier_for_probability, feature_completeness
from app.sports.models import League, Sport
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

_model_runner = ModelRunner()

# Where the multi-season game logs live. The path-relative default is right for a checkout; the
# deployed image sets FOOTBALL_DATA_DIR because the default resolves to /ml/data there and does
# not exist.
#
# THAT ABSENCE WAS NOT HARMLESS, though it was designed to be. _load_cached_history degrades to
# DB-only history rather than raising, and the database holds ~7 days of fixtures -- too few
# prior matches for most teams to have a rolling stat at all. So every retrodicted prediction in
# production came out as the model's flat prior: H0.454 D0.294 A0.252, identical across every
# fixture in every league. Those then failed MIN_EDGE_OVER_BASE_RATE, correctly, because a
# prediction equal to the base rate says nothing -- and 97 finished cards showed no pick.
#
# The blank card was the guard working. The defect was upstream, in the features.
DATA_DIR = Path(
    os.getenv("FOOTBALL_DATA_DIR") or Path(__file__).resolve().parents[3] / "ml" / "data"
)


def _build_game_log_df(
    completed_rows: list[tuple[Fixture, FixtureLiveState, str, str]],
) -> pd.DataFrame:
    """Pure, DB-free — one row per team per fixture, the same TEAM_ID/OPPONENT_ID/GAME_DATE/
    GF/GA/WDL/HOME_AWAY/FIXTURE_ID/SEASON shape ml/training/collect_football_data.py produces,
    so assemble_from_game_log's internal filtering/rolling logic (and elo.compute_elo_history)
    need no changes at all to work against our own DB's fixture history."""
    rows = []
    for fixture, live_state, home_ext_id, away_ext_id in completed_rows:
        game_date = fixture.kickoff_utc.date()
        home_goals, away_goals = live_state.home_score, live_state.away_score
        fixture_ext_id = fixture.external_id

        def wdl(gf: int, ga: int) -> str:
            if gf > ga:
                return "W"
            if gf < ga:
                return "L"
            return "D"

        rows.append(
            {
                "FIXTURE_ID": fixture_ext_id,
                "SEASON": int(fixture.season),
                "GAME_DATE": game_date,
                "TEAM_ID": home_ext_id,
                "OPPONENT_ID": away_ext_id,
                "HOME_AWAY": "home",
                "GF": home_goals,
                "GA": away_goals,
                "WDL": wdl(home_goals, away_goals),
            }
        )
        rows.append(
            {
                "FIXTURE_ID": fixture_ext_id,
                "SEASON": int(fixture.season),
                "GAME_DATE": game_date,
                "TEAM_ID": away_ext_id,
                "OPPONENT_ID": home_ext_id,
                "HOME_AWAY": "away",
                "GF": away_goals,
                "GA": home_goals,
                "WDL": wdl(away_goals, home_goals),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "FIXTURE_ID",
            "SEASON",
            "GAME_DATE",
            "TEAM_ID",
            "OPPONENT_ID",
            "HOME_AWAY",
            "GF",
            "GA",
            "WDL",
        ],
    )


def _load_cached_history(league_slug: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Real multi-season game log + lineups from ml/training/collect_football_data.py's
    cache, if this league has one collected yet (currently epl/brasileirao — see CLAUDE.md).
    Returns empty frames (not an error) for a league with no cache, so callers fall back
    gracefully to DB-only history."""
    games_path = DATA_DIR / f"football_game_log_{league_slug}.parquet"
    lineups_path = DATA_DIR / f"football_lineups_{league_slug}.parquet"

    games = (
        pd.read_parquet(games_path)
        if games_path.exists()
        else pd.DataFrame(
            columns=[
                "FIXTURE_ID",
                "SEASON",
                "GAME_DATE",
                "TEAM_ID",
                "OPPONENT_ID",
                "HOME_AWAY",
                "GF",
                "GA",
                "WDL",
            ]
        )
    )
    lineups = (
        pd.read_parquet(lineups_path)
        if lineups_path.exists()
        else pd.DataFrame(columns=["FIXTURE_ID", "TEAM_ID", "PLAYER_NAME"])
    )
    return games, lineups


async def _lineup_presence_for_fixture(
    lineups: pd.DataFrame, fixture_external_id: str
) -> dict[str, set[str]]:
    """{team_external_id: {lowercased played names}} for one fixture — from the cached
    parquet if already collected there, else one real live fetch (fixture_external_id is a
    provider ID that's a string in our DB but an int in the cached parquet's own dtype, so
    both are tried)."""
    cached = lineups[
        (lineups["FIXTURE_ID"] == fixture_external_id)
        | (lineups["FIXTURE_ID"].astype(str) == str(fixture_external_id))
    ]
    if not cached.empty:
        by_team: dict[str, set[str]] = {}
        names_lower = cached["PLAYER_NAME"].str.lower()
        for team_id, name in zip(cached["TEAM_ID"], names_lower, strict=False):
            by_team.setdefault(str(team_id), set()).add(name)
        return by_team

    # NO LIVE FETCH. This used to fall back to fetch_lineup_presence, one real API call per
    # uncached fixture -- which in production meant EVERY fixture, since no lineup parquet ships
    # in the image. It bought nothing: the four key-player features it feeds were pruned from
    # the model vector in d0a24d9, confirmed against FEATURE_NAMES rather than assumed. So the
    # call spent quota to compute values nothing reads, while adding a per-fixture network
    # failure to a loop whose failures used to discard a whole league.
    return {}


async def _retrodict_league(sport: Sport, league: League) -> None:
    async with async_session_factory() as db:
        completed = (
            await db.execute(
                select(Fixture, FixtureLiveState)
                .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(Fixture.league_id == league.id, Fixture.status == FixtureStatus.COMPLETED)
            )
        ).all()
        if not completed:
            return

        team_ext_by_id: dict = {}
        for fixture, _live_state in completed:
            for team_id in (fixture.home_team_id, fixture.away_team_id):
                if team_id not in team_ext_by_id:
                    team = (
                        await db.execute(select(Team).where(Team.id == team_id))
                    ).scalar_one_or_none()
                    team_ext_by_id[team_id] = team.external_id if team else None

        rows = [
            (
                fixture,
                live_state,
                team_ext_by_id[fixture.home_team_id],
                team_ext_by_id[fixture.away_team_id],
            )
            for fixture, live_state in completed
        ]
        rows = [r for r in rows if r[2] is not None and r[3] is not None]

        cached_games, cached_lineups = _load_cached_history(league.slug)
        db_game_log = _build_game_log_df(rows)
        # Append only DB fixtures not already in the cached parquet (by external fixture id) —
        # avoids duplicate rows double-weighting rolling-form/Elo for a fixture collected both
        # ways, while still picking up anything completed since the last historical collection
        # run.
        cached_fixture_ids = (
            set(cached_games["FIXTURE_ID"].astype(str)) if not cached_games.empty else set()
        )
        new_rows = db_game_log[~db_game_log["FIXTURE_ID"].astype(str).isin(cached_fixture_ids)]
        game_log = pd.concat([cached_games, new_rows], ignore_index=True)
        # FIXTURE_ID arrives as int64 from the parquet and as str from the DB (external_id), so
        # the concatenated frame carries BOTH types. compute_elo_history keys its result on
        # whatever each row holds, while the lookup below always uses the str external_id — so
        # every fixture sourced from the parquet silently returned elo_diff=None. The value was
        # there; only the key type differed.
        #
        # Silent in the worst way: a missing Elo does not raise, it just drops the single
        # strongest team-strength signal and leaves a confident-looking prediction behind.
        # Measured on the nine newly-collected leagues, whose 2026 fixtures moved from
        # DB-sourced to parquet-sourced and so lost Elo: predicted away-win probability
        # collapsed to 5.7% against an actual 36.6%, and mean home xG rose to 2.43 against an
        # actual 1.73. Note the dedup filter directly above already normalised with
        # .astype(str) -- the same fix, applied to one of the two places that needed it.
        game_log["FIXTURE_ID"] = game_log["FIXTURE_ID"].astype(str)

        team_key_players_by_team_season = await load_team_key_players_by_team_season(db)
        elo_history = compute_elo_history(game_log) if not game_log.empty else {}

        existing_prediction_fixture_ids = set(
            (
                await db.execute(
                    select(Prediction.fixture_id).where(
                        Prediction.fixture_id.in_([f.id for f, *_ in rows])
                    )
                )
            )
            .scalars()
            .all()
        )

        model = await _model_runner.get_model(db, sport.id)

        candidates = [r for r in rows if r[0].id not in existing_prediction_fixture_ids]
        written = failed = 0

        for fixture, _live_state, home_ext, away_ext in candidates:
            try:
                written += await _retrodict_one(
                    db,
                    model,
                    fixture,
                    home_ext,
                    away_ext,
                    game_log,
                    cached_lineups,
                    elo_history,
                    team_key_players_by_team_season,
                )
            except Exception:  # noqa: BLE001 - see below
                # ONE FIXTURE MUST NOT COST THE LEAGUE. Until 2026-08-15 this loop had no
                # per-fixture guard and db.commit() sat after it, so a single failure -- a live
                # fetch_lineup_presence timeout, an unparseable season, a feature the model
                # rejects -- discarded every prediction the league had just produced. The
                # exception then surfaced to _ingest_fixtures's per-league
                # `except (httpx.HTTPError, ValueError)` as one warning line, and the next
                # day's run failed the same way.
                #
                # Measured cost of that silence in production: 96 completed football fixtures
                # across 13 of 18 leagues showing no pick at all -- 35% of every finished card.
                failed += 1
                logger.warning(
                    "retrodiction failed for fixture %s (%s) — continuing with the league",
                    fixture.external_id,
                    league.slug,
                    exc_info=True,
                )

        await db.commit()

        # ALWAYS LOGGED, including the zero case. A retrodiction that produces nothing is
        # indistinguishable from one that had nothing to do, and that is exactly how this went
        # unnoticed: nothing errored, nothing logged, and a third of finished cards were blank.
        log = logger.warning if (candidates and not written) else logger.info
        log(
            "retrodiction %s: %d completed, %d needed a prediction, %d written, %d failed",
            league.slug,
            len(rows),
            len(candidates),
            written,
            failed,
        )


async def _retrodict_one(
    db,
    model,
    fixture,
    home_ext: str,
    away_ext: str,
    game_log,
    cached_lineups,
    elo_history: dict,
    team_key_players_by_team_season: dict,
) -> int:
    """One fixture's retrodicted prediction, added to the session. Returns 1 when written.

    Split out of _retrodict_league solely so a failure can be caught per fixture — the body is
    otherwise unchanged."""
    season = int(fixture.season)
    fixture_ext = fixture.external_id

    played_by_team = await _lineup_presence_for_fixture(cached_lineups, fixture_ext)
    # historical_key_player_availability expects a (fixture_id, team_id) -> names index, not a
    # per-fixture dict — build a tiny one-fixture index inline rather than importing
    # index_played_names for a shape that's already resolved.
    played_names_index = {
        (fixture_ext, team_id): names for team_id, names in played_by_team.items()
    }

    key_avail_home, key_per_home = historical_key_player_availability(
        played_names_index, team_key_players_by_team_season, home_ext, season, fixture_ext
    )
    key_avail_away, key_per_away = historical_key_player_availability(
        played_names_index, team_key_players_by_team_season, away_ext, season, fixture_ext
    )

    elo_home = elo_history.get((fixture_ext, home_ext))
    elo_away = elo_history.get((fixture_ext, away_ext))
    elo_diff = (elo_home - elo_away) if elo_home is not None and elo_away is not None else None

    features = assemble_from_game_log(
        game_log,
        fixture.kickoff_utc.date(),
        home_ext,
        away_ext,
        moneyline_implied_prob_home=None,
        key_players_available_home=key_avail_home,
        key_players_available_away=key_avail_away,
        key_players_per_combined_home=key_per_home,
        key_players_per_combined_away=key_per_away,
        elo_diff=elo_diff,
    )
    result = model.predict(features)
    probability = max(result.home_prob, result.away_prob, result.draw_prob or 0.0)
    db.add(
        Prediction(
            fixture_id=fixture.id,
            model_version=model.version,
            home_prob=result.home_prob,
            draw_prob=result.draw_prob,
            away_prob=result.away_prob,
            confidence_tier=confidence_tier_for_probability(probability),
            feature_completeness=feature_completeness(features),
            # Produced after the result was known — legitimate for the feed, never evidence of
            # skill. See PredictionKind.
            kind=PredictionKind.RETRODICTION,
            xg_home=result.xg_home,
            xg_away=result.xg_away,
            corners_xg_home=result.corners_xg_home,
            corners_xg_away=result.corners_xg_away,
            created_at=datetime.now(UTC),
        )
    )
    return 1


async def _backfill_predictions() -> None:
    async with async_session_factory() as db:
        # Football only for now — assemble_from_game_log's shape is football-specific
        # (home/away team id + moneyline), and NBA's own equivalent expects a season-labelled
        # multi-team DataFrame, not this league-scoped one. A real, documented scope cut, not
        # an oversight.
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
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
        await _retrodict_league(sport, league)


@celery_app.task(name="app.workers.backfill_predictions.backfill_predictions")
def backfill_predictions() -> None:
    """Standalone entry point (e.g. a manual/one-off run across every football league at
    once) — the per-league _retrodict_league is also called directly from
    ingest_fixtures.py's own daily backfill, so newly-completed fixtures normally get a real
    retrodicted prediction without needing this task scheduled separately at all."""
    run_task(_backfill_predictions())
