"""Retrodicted predictions for completed basketball fixtures.

WHY THIS EXISTS. A fixture first seen as ALREADY FINISHED never gets a pre-match prediction:
ingest_fixtures excludes COMPLETED fixtures from its feature/prediction loop, which is correct
(there is nothing to forecast) but leaves the card blank on the screen users open specifically
to see whether their pick came in. Football and tennis have had a retrodiction path for months.
Basketball had NONE -- measured in production 2026-08-15, 23 completed NBA/WNBA fixtures with
no prediction at all.

LEAKAGE SAFETY IS THE WHOLE DESIGN, and it is the same one football uses. This does NOT go
through run_predictions: that assembles features from TeamFeatures, a snapshot of form taken at
INGEST time, which for a past game reflects every result that happened after it -- including
the game's own. Instead the game log is rebuilt from completed fixtures and fed through
nba_features.assemble_from_game_log, the same function train_nba.py trains on, whose internal
`GAME_DATE < as_of_date` filter is the guard.

THE GAME LOG IS SCOPED TO ONE LEAGUE, AND THAT IS LOAD-BEARING. NBA and WNBA share a Sport row
and assemble_from_game_log keys on TEAM_ABBREVIATION, and FOUR abbreviations are shared between
the two competitions -- measured, not assumed:

    ATL   Atlanta Hawks      / Atlanta Dream
    DAL   Dallas Mavericks   / Dallas Wings
    PHX   Phoenix Suns       / Phoenix Mercury
    POR   Portland Trail Blazers / Portland Fire

A pooled log would compute the Hawks' form from Dream results. Same hazard the `wnba:` external
id prefix exists to prevent, one layer up.

KEY-PLAYER FEATURES ARE LEFT NONE. Their historical counterpart is derived from box-score
presence, and BallDontLie's /stats returns 401 on this plan, so there is no honest way to fill
them for a past game. XGBoost handles the missing values natively; fabricating "all available"
would be a made-up input, not a neutral one.
"""

import logging
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.nba_features import assemble_from_game_log
from app.models_ml.runner import ModelRunner
from app.predictions.models import Prediction, PredictionKind
from app.predictions.service import confidence_tier_for_probability, feature_completeness
from app.sports.models import League, Sport
from app.workers.celery import celery_app, run_task

logger = logging.getLogger(__name__)

_model_runner = ModelRunner()


def _build_game_log_df(rows) -> pd.DataFrame:
    """One row per team per fixture, in nba_api's leaguegamelog shape -- the columns
    assemble_from_game_log actually reads: TEAM_ABBREVIATION, GAME_DATE, WL, PLUS_MINUS,
    SEASON, MATCHUP.

    MATCHUP is built in nba_api's own format because _h2h_win_rate matches on
    `.str.endswith(opponent_abbr)`: a home row reads "DET vs. PHX" and an away row "PHX @ DET",
    so both end with the opponent's abbreviation exactly as that lookup expects.

    A TIED score is dropped rather than recorded. Basketball games go to overtime, so a tie
    means the winner is not derivable -- the same reasoning that puts "nba" in
    ingest_fixtures.SPORTS_WITHOUT_DRAWS -- and WL has no honest value for it.
    """
    records = []
    for fixture, live_state, home_abbr, away_abbr in rows:
        if live_state.home_score is None or live_state.away_score is None:
            continue
        margin = live_state.home_score - live_state.away_score
        if margin == 0:
            continue
        game_date = fixture.kickoff_utc.date()
        records.append(
            {
                "TEAM_ABBREVIATION": home_abbr,
                "GAME_DATE": game_date,
                "SEASON": str(fixture.season),
                "MATCHUP": f"{home_abbr} vs. {away_abbr}",
                "WL": "W" if margin > 0 else "L",
                "PLUS_MINUS": float(margin),
            }
        )
        records.append(
            {
                "TEAM_ABBREVIATION": away_abbr,
                "GAME_DATE": game_date,
                "SEASON": str(fixture.season),
                "MATCHUP": f"{away_abbr} @ {home_abbr}",
                "WL": "W" if margin < 0 else "L",
                "PLUS_MINUS": float(-margin),
            }
        )
    return pd.DataFrame(
        records,
        columns=["TEAM_ABBREVIATION", "GAME_DATE", "SEASON", "MATCHUP", "WL", "PLUS_MINUS"],
    )


def _retrodict_one(db, model, fixture, live_state, home_abbr, away_abbr, game_log) -> int:
    """One fixture's retrodicted prediction, added to the session. Returns 1 when written.

    Split out so a failure can be isolated per fixture -- see _retrodict_basketball_league."""
    features = assemble_from_game_log(
        game_log,
        fixture.kickoff_utc.date(),
        str(fixture.season),
        home_abbr,
        away_abbr,
        moneyline_implied_prob_home=None,
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
            # Produced after the result was known -- legitimate for the feed, never evidence of
            # forecasting skill. See PredictionKind.
            kind=PredictionKind.RETRODICTION,
            created_at=datetime.now(UTC),
        )
    )
    return 1


async def _retrodict_basketball_league(sport: Sport, league: League) -> None:
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

        abbr_by_team_id: dict = {}
        for fixture, _live in completed:
            for team_id in (fixture.home_team_id, fixture.away_team_id):
                if team_id not in abbr_by_team_id:
                    team = (
                        await db.execute(select(Team).where(Team.id == team_id))
                    ).scalar_one_or_none()
                    abbr_by_team_id[team_id] = team.short_name if team else None

        rows = [
            (
                fixture,
                live,
                abbr_by_team_id[fixture.home_team_id],
                abbr_by_team_id[fixture.away_team_id],
            )
            for fixture, live in completed
        ]
        rows = [r for r in rows if r[2] and r[3]]
        if not rows:
            return

        game_log = _build_game_log_df(rows)

        existing = set(
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
        candidates = [r for r in rows if r[0].id not in existing]

        model = await _model_runner.get_model(db, sport.id)

        written = failed = 0
        for fixture, live_state, home_abbr, away_abbr in candidates:
            try:
                written += _retrodict_one(
                    db, model, fixture, live_state, home_abbr, away_abbr, game_log
                )
            except Exception:  # noqa: BLE001
                # One fixture must not cost the league -- the defect that left 96 football
                # cards blank in production. See backfill_predictions.py.
                failed += 1
                logger.warning(
                    "basketball retrodiction failed for fixture %s (%s) — continuing",
                    fixture.external_id,
                    league.slug,
                    exc_info=True,
                )

        await db.commit()

        log = logger.warning if (candidates and not written) else logger.info
        log(
            "basketball retrodiction %s: %d completed, %d needed a prediction, %d written, "
            "%d failed",
            league.slug,
            len(rows),
            len(candidates),
            written,
            failed,
        )


async def _backfill_basketball_predictions() -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "nba"))).scalar_one_or_none()
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
        await _retrodict_basketball_league(sport, league)


@celery_app.task(name="app.workers.backfill_basketball_predictions.backfill_basketball_predictions")
def backfill_basketball_predictions() -> None:
    """Every 2 hours, and also at the tail of each league's daily ingest — same arrangement as
    football and tennis, for the same reason: the daily run being the only chance meant one
    failure cost a full day."""
    run_task(_backfill_basketball_predictions())
