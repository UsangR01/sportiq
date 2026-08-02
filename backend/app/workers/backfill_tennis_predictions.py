"""Retrodicted predictions for completed tennis fixtures — mirrors
app/workers/backfill_predictions.py's football design and rationale exactly: a live-inference
pass (run_predictions.py) assembles features from TeamFeatures/live BallDontLie calls
reflecting team state "as of now", which for a past match leaks every result that happened
*after* it, including the match's own eventual outcome once enough time has passed. This
instead builds a leakage-safe game log and feeds it through the SAME
app/models_ml/tennis_features.py:assemble_from_game_log function ml/training/train_tennis.py
trains with — its own internal GAME_DATE < as_of_date filter is the leakage guard.

Much simpler than football's retrodiction: tennis has no Elo (uses real historical rank_points
instead, see below) and no key-player-availability feature at all (app/models_ml/
tennis_features.py's own docstring — TeamKeyPlayer/PlayerInjuryStatus are never populated for
tennis), so this needs neither app/models_ml/elo.py nor historical_key_players.py.

Real historical data: the same ml/data/tennis_game_log_{tour}.parquet /
tennis_rank_points_{tour}.parquet ml/training/train_tennis.py trains on (currently ATP,
2021-2025 — see CLAUDE.md), with any of the live DB's completed fixtures not already present
in that cache appended on top (deduped by the provider's own numeric match id — Fixture.
external_id is tour-prefixed, e.g. "atp:9001", stripped before comparing to the cached
parquet's bare MATCH_ID). A fixture not already in the cache needs two real, one-off live
calls retrodiction can afford that live serving can't: its own surface
(fetch_match_surface) and, for any (player, ISO week) not already in the cached rank-points
parquet, a real point-in-time ranking lookup.

Deliberately bounded, not run unconditionally: the live DB currently also holds several
thousand tennis fixtures from 2007-2020 (leftover test/verification data from this feature's
own build-out — see CLAUDE.md's "separate real oddity" note), well before the cached
2021-2025 training window and with no real product value in retrodicting. Live-fetching a
surface for every one of those would be thousands of pointless API calls for noise data, not
real inventory. RETRODICT_LOOKBACK_DAYS caps the live-fetch fallback to fixtures that
completed recently (i.e. genuinely new since the last historical-collection run); anything
older that's ALSO missing from the cached parquet is skipped and counted, never silently
dropped (see the "no silent caps" logging in _retrodict_tennis_league).

Moneyline-implied-probability stays None here — no tennis odds exist yet at all (an explicit
fast-follow, not v1 scope, see CLAUDE.md) — the same honest, non-fabricated gap as football's
own retrodiction.

ATP only for now, matching every other real tennis feature's scope cut (WTA is blocked on that
tour's own BallDontLie subscription, still a live 401) — re-runnable unchanged for WTA once
that's confirmed, nothing about this module is ATP-specific beyond the league filter below.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy import select

from app.adapters.balldontlie_tennis import _strip_tour_prefix, fetch_match_surface
from app.core.config import get_settings
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.runner import ModelRunner
from app.models_ml.tennis_features import assemble_from_game_log
from app.predictions.models import Prediction
from app.predictions.service import confidence_tier_for_probability, feature_completeness
from app.sports.models import League, Sport
from app.workers.celery import celery_app, run_task

_model_runner = ModelRunner()

DATA_DIR = Path(__file__).resolve().parents[3] / "ml" / "data"

GAME_LOG_COLUMNS = [
    "MATCH_ID",
    "TOURNAMENT_ID",
    "SEASON",
    "PLAYER_ID",
    "OPPONENT_ID",
    "HOME_AWAY",
    "GAME_DATE",
    "WL",
    "SURFACE",
]

# Fixtures completed before this window that aren't already in the cached parquet are skipped
# rather than triggering a live surface-fetch — see module docstring for why (mostly pre-2021
# leftover test data, not real product inventory worth thousands of live API calls).
RETRODICT_LOOKBACK_DAYS = 60

# Proactive pacing for every real per-fixture live call below (surface + rank fallback) —
# mirrors RANK_REQUEST_DELAY_SECONDS in ml/training/collect_tennis_data.py, added after that
# script's own real lesson: firing requests back-to-back with no pacing burns ALL-STAR's whole
# 60 req/min budget in under a second, then waits out an entire ~50s cooldown per subsequent
# request. Retrodiction over hundreds of fixtures makes the same mistake just as costly here.
RETRODICT_REQUEST_DELAY_SECONDS = 1.1


def _load_cached_tennis_history(tour: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Real multi-season game log + rank-points cache from ml/training/collect_tennis_data.py,
    if collected for this tour yet (currently ATP only). Empty frames (not an error) for a tour
    with no cache — callers fall back gracefully to DB-only, live-fetched history."""
    games_path = DATA_DIR / f"tennis_game_log_{tour}.parquet"
    ranks_path = DATA_DIR / f"tennis_rank_points_{tour}.parquet"
    games = (
        pd.read_parquet(games_path)
        if games_path.exists()
        else pd.DataFrame(columns=GAME_LOG_COLUMNS)
    )
    ranks = (
        pd.read_parquet(ranks_path)
        if ranks_path.exists()
        else pd.DataFrame(columns=["PLAYER_ID", "WEEK", "RANK_POINTS"])
    )
    return games, ranks


def _iso_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _build_new_game_log_rows(
    rows: list[tuple[Fixture, FixtureLiveState, str, str]], tour: str
) -> pd.DataFrame:
    """rows: (fixture, live_state, home_raw_player_id, away_raw_player_id) for fixtures not
    already in the cached parquet. One real, one-off live call per fixture for its own surface
    (fetch_match_surface) — retrodiction can afford this the way live serving can't, same
    tradeoff football's own real per-fixture lineup-presence fetch makes."""
    out = []
    for fixture, live_state, home_id, away_id in rows:
        game_date = fixture.kickoff_utc.date()
        home_sets, away_sets = live_state.home_score, live_state.away_score
        surface = await fetch_match_surface(tour, fixture.external_id)
        await asyncio.sleep(RETRODICT_REQUEST_DELAY_SECONDS)
        raw_match_id = int(_strip_tour_prefix(fixture.external_id))
        for player, opponent, home_away, won in (
            (home_id, away_id, "home", home_sets > away_sets),
            (away_id, home_id, "away", away_sets > home_sets),
        ):
            out.append(
                {
                    "MATCH_ID": raw_match_id,
                    "TOURNAMENT_ID": None,
                    "SEASON": int(fixture.season),
                    "PLAYER_ID": player,
                    "OPPONENT_ID": opponent,
                    "HOME_AWAY": home_away,
                    "GAME_DATE": game_date,
                    "WL": "W" if won else "L",
                    "SURFACE": surface,
                }
            )
    return pd.DataFrame(out, columns=GAME_LOG_COLUMNS)


_RANK_LOOKUP_MAX_RETRIES = 5


async def _rank_points_for(
    cached_ranks: pd.DataFrame,
    player_id: str,
    week: date,
    tour: str,
    live_cache: dict[tuple[str, date], float | None],
) -> float | None:
    """Cached rank-points parquet first, then an in-memory live_cache (many fixtures in the
    same tournament share the same ISO week, so a per-run dict avoids redundant real lookups
    for a pair already fetched earlier in this same retrodiction pass — the cached parquet
    itself is a static, point-in-time snapshot and is never written back to). Only on a genuine
    miss in both does this make one real, live single-player /rankings lookup — the batched
    player_ids[] form (see ml/training/collect_tennis_data.py) is for bulk collection; a single
    retrodiction lookup has nothing else to batch with. Retries on 429 (honouring Retry-After
    when present, else capped exponential backoff) — the same pattern as
    balldontlie_tennis.py:_get_with_retry, copied rather than cross-imported since that's a
    private adapter helper and this project's own established precedent is to copy this
    handful of lines per call site rather than factor out a shared HTTP module (see that
    function's own docstring)."""
    hit = cached_ranks[(cached_ranks["PLAYER_ID"] == player_id) & (cached_ranks["WEEK"] == week)]
    if not hit.empty:
        points = hit.iloc[0]["RANK_POINTS"]
        return None if pd.isna(points) else float(points)

    cache_key = (player_id, week)
    if cache_key in live_cache:
        return live_cache[cache_key]

    api_key = get_settings().balldontlie_api_key
    params = {"player_ids[]": player_id, "date": week.isoformat(), "per_page": 1}
    async with httpx.AsyncClient(
        base_url=f"https://api.balldontlie.io/{tour}/v1",
        headers={"Authorization": api_key},
        timeout=10.0,
    ) as client:
        response = None
        for attempt in range(_RANK_LOOKUP_MAX_RETRIES):
            response = await client.get("/rankings", params=params)
            if response.status_code == 429 and attempt < _RANK_LOOKUP_MAX_RETRIES - 1:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(2**attempt, 30)
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            break
        data = response.json().get("data", [])
    await asyncio.sleep(RETRODICT_REQUEST_DELAY_SECONDS)
    points = data[0].get("points") if data else None
    live_cache[cache_key] = points
    return points


async def _retrodict_tennis_league(sport: Sport, league: League) -> None:
    tour = league.slug
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
                    team_ext_by_id[team_id] = (
                        _strip_tour_prefix(team.external_id) if team and team.external_id else None
                    )

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
        rows = [r for r in rows if r[0].id not in existing_prediction_fixture_ids]
        if not rows:
            return

        cached_games, cached_ranks = _load_cached_tennis_history(tour)
        cached_match_ids = (
            set(cached_games["MATCH_ID"].astype(str)) if not cached_games.empty else set()
        )

        cutoff = datetime.now(UTC) - timedelta(days=RETRODICT_LOOKBACK_DAYS)
        rows_in_cache = [
            r for r in rows if str(int(_strip_tour_prefix(r[0].external_id))) in cached_match_ids
        ]
        rows_needing_fetch = [
            r
            for r in rows
            if str(int(_strip_tour_prefix(r[0].external_id))) not in cached_match_ids
            and r[0].kickoff_utc >= cutoff
        ]
        skipped = len(rows) - len(rows_in_cache) - len(rows_needing_fetch)
        if skipped:
            print(
                f"tennis retrodiction ({tour}): skipping {skipped} completed fixtures older than "
                f"{RETRODICT_LOOKBACK_DAYS}d and not in the cached training data (real historical "
                f"test-collection pollution, not live-fetched to avoid a live-call storm — see "
                f"backfill_tennis_predictions.py's module docstring)"
            )

        # Checked before any live per-fixture fetch below (mirrors
        # backfill_predictions.py's own ordering) — a sport with no registered model fails
        # fast, without wasting real API calls on a run that can't produce a prediction anyway.
        model = await _model_runner.get_model(db, sport.id)

        new_game_log = await _build_new_game_log_rows(rows_needing_fetch, tour)
        game_log = pd.concat([cached_games, new_game_log], ignore_index=True)

        live_rank_cache: dict[tuple[str, date], float | None] = {}
        for fixture, _live_state, home_id, away_id in rows_in_cache + rows_needing_fetch:
            as_of_date = fixture.kickoff_utc.date()
            week = _iso_monday(as_of_date)
            home_rank = await _rank_points_for(cached_ranks, home_id, week, tour, live_rank_cache)
            away_rank = await _rank_points_for(cached_ranks, away_id, week, tour, live_rank_cache)

            features = assemble_from_game_log(
                game_log,
                as_of_date,
                home_id,
                away_id,
                home_rank_points=home_rank,
                away_rank_points=away_rank,
                moneyline_implied_prob_home=None,
            )
            result = model.predict(features)
            probability = max(result.home_prob, result.away_prob)
            db.add(
                Prediction(
                    fixture_id=fixture.id,
                    model_version=model.version,
                    home_prob=result.home_prob,
                    draw_prob=result.draw_prob,
                    away_prob=result.away_prob,
                    confidence_tier=confidence_tier_for_probability(probability),
                    feature_completeness=feature_completeness(features),
                    xg_home=result.xg_home,
                    xg_away=result.xg_away,
                    corners_xg_home=result.corners_xg_home,
                    corners_xg_away=result.corners_xg_away,
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()


async def _backfill_tennis_predictions() -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "tennis"))).scalar_one_or_none()
        if sport is None:
            return
        leagues = (
            (
                await db.execute(
                    select(League).where(
                        League.sport_id == sport.id, League.active.is_(True), League.slug == "atp"
                    )
                )
            )
            .scalars()
            .all()
        )
    for league in leagues:
        await _retrodict_tennis_league(sport, league)


@celery_app.task(name="app.workers.backfill_tennis_predictions.backfill_tennis_predictions")
def backfill_tennis_predictions() -> None:
    """Standalone entry point (a manual/one-off run across every real tennis league at once) —
    the per-league _retrodict_tennis_league is also called directly from ingest_fixtures.py's
    own daily backfill, so newly-completed fixtures normally get a real retrodicted prediction
    without needing this task scheduled separately at all."""
    run_task(_backfill_tennis_predictions())
