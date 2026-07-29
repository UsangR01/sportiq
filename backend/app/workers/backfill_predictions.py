"""Retrodicted predictions for completed fixtures (football only, for now) — added so the
Home feed can show "what the model would have called" alongside a real final score, not just
for upcoming fixtures.

Deliberately NOT the same code path as run_predictions.py's live inference: that assembles
features from TeamFeatures (a live snapshot of "current" team form/win-rate/etc, computed at
ingest time), which for a past game reflects form as of NOW rather than as of the game's
actual kickoff — using it would leak information from every game that happened AFTER the one
being predicted, including the game's own eventual outcome once enough time has passed.

Instead this builds a real, leakage-safe "game log" from our own DB's completed fixtures
(exactly the shape app/models_ml/football_features.py:assemble_from_game_log already expects
and already filters strictly to GAME_DATE < as_of_date internally — the same function
ml/training/train_football.py used to train the model) — so the historical and retrodicted
feature-assembly code paths are the same code, not just the same idea.

Real, honest limitation: our own DB's fixture history only goes back as far as
FIXTURE_HISTORY_DAYS (7 days, see ingest_fixtures.py) plus however long ingestion has been
running since. A fixture near the start of that window has little or no prior in-DB history
to compute rolling form from — its features come back mostly/entirely None, and the model's
own missing-data handling (not a fabricated neutral value) determines the output. This
naturally improves as more days of real ingestion accumulate.

Key-player availability and moneyline-implied-probability are always None here: Stage 1/2's
own data only reflects current-season standing, not "as of that specific past date", and no
historical odds are ever ingested (ingest_odds.py only ever looks forward) — both are real,
same-shaped gaps as the "no elo/xG" omissions already documented elsewhere, not fabricated.
"""

import asyncio
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.football_features import assemble_from_game_log
from app.models_ml.runner import ModelRunner
from app.predictions.models import Prediction
from app.predictions.service import confidence_tier_for_probability
from app.sports.models import League, Sport
from app.workers.celery import celery_app

_model_runner = ModelRunner()


def _build_game_log_df(
    completed_rows: list[tuple[Fixture, FixtureLiveState, str, str]],
) -> pd.DataFrame:
    """Pure, DB-free — one row per team per fixture, the same TEAM_ID/OPPONENT_ID/GAME_DATE/
    GF/GA/WDL/HOME_AWAY shape ml/training/collect_football_data.py produces, so
    assemble_from_game_log's internal filtering/rolling logic needs no changes at all to work
    against our own DB's fixture history instead of the training-time parquet cache."""
    rows = []
    for fixture, live_state, home_ext_id, away_ext_id in completed_rows:
        game_date = fixture.kickoff_utc.date()
        home_goals, away_goals = live_state.home_score, live_state.away_score

        def wdl(gf: int, ga: int) -> str:
            if gf > ga:
                return "W"
            if gf < ga:
                return "L"
            return "D"

        rows.append(
            {
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
        rows, columns=["GAME_DATE", "TEAM_ID", "OPPONENT_ID", "HOME_AWAY", "GF", "GA", "WDL"]
    )


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
        game_log = _build_game_log_df([r for r in rows if r[2] is not None and r[3] is not None])

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

        for fixture, _live_state, home_ext, away_ext in rows:
            if fixture.id in existing_prediction_fixture_ids:
                continue
            if home_ext is None or away_ext is None:
                continue

            features = assemble_from_game_log(
                game_log,
                fixture.kickoff_utc.date(),
                home_ext,
                away_ext,
                moneyline_implied_prob_home=None,
                key_players_available_home=None,
                key_players_available_away=None,
                key_players_per_combined_home=None,
                key_players_per_combined_away=None,
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
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()


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
    asyncio.run(_backfill_predictions())
