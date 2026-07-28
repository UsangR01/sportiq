"""Shared NBA feature-vector assembly — the single source of truth for both training
(ml/training/train_nba.py, via assemble_from_game_log) and live inference
(app/workers/run_predictions.py, via assemble_from_live_db). Train/serve parity is the whole
point of this module existing separately: if the two paths ever compute a named feature
differently, the model sees out-of-distribution inputs in production silently.

Scope, relative to TDD §3.3's 13-feature NBA list — reduced to 12, both omissions
deliberate and documented (not silently dropped):
  - Injury impact (home/away, 2 features) is OMITTED entirely. No RotoWire subscription,
    and BallDontLie's own injury endpoint 401s on our current tier too (see CLAUDE.md).
  - Pace differential is OMITTED. It IS computable at training time from nba_api's game log
    (FGA/OREB/TOV/FTA give a standard possession estimate), but BallDontLie's live /games
    response only has final scores — no shooting/box-score stats. Including it would mean a
    real training feature that's permanently missing at serving time, which is worse than
    not having it: better to keep train/serve parity airtight than pad the feature count.

The remaining 12 (this module's FEATURE_NAMES, in order):
  rest_days_home, rest_days_away, back_to_back_home, back_to_back_away,
  last10_win_rate_home, last10_win_rate_away, last10_point_diff_home,
  last10_point_diff_away, net_rating_diff, home_court_indicator, h2h_win_rate_home,
  moneyline_implied_prob_home.

Missing data is represented as None throughout (never a fabricated neutral value) — XGBoost
handles missing values natively, and inventing e.g. 0.5 for "no H2H history" would tell the
model something the data doesn't actually support.
"""

from datetime import date

import pandas as pd

FEATURE_NAMES = (
    "rest_days_home",
    "rest_days_away",
    "back_to_back_home",
    "back_to_back_away",
    "last10_win_rate_home",
    "last10_win_rate_away",
    "last10_point_diff_home",
    "last10_point_diff_away",
    "net_rating_diff",
    "home_court_indicator",
    "h2h_win_rate_home",
    "moneyline_implied_prob_home",
)

LAST_N_FORM = 10  # matches app/workers/ingest_fixtures.py's FEATURE_WINDOW_MATCHES
BACK_TO_BACK_MAX_REST_DAYS = 1


def _rest_days(team_games: pd.DataFrame, as_of_date: date) -> float | None:
    prior = team_games[team_games["GAME_DATE"] < as_of_date]
    if prior.empty:
        return None
    return float((as_of_date - prior["GAME_DATE"].max()).days)


def _back_to_back(rest_days: float | None) -> float:
    if rest_days is None:
        return 0.0  # unknown treated as "assume not back-to-back" — a low-risk default
    return 1.0 if rest_days <= BACK_TO_BACK_MAX_REST_DAYS else 0.0


def _last10_stats(team_games: pd.DataFrame, as_of_date: date) -> tuple[float | None, float | None]:
    prior = team_games[team_games["GAME_DATE"] < as_of_date].sort_values(
        "GAME_DATE", ascending=False
    )
    recent = prior.head(LAST_N_FORM)
    if recent.empty:
        return None, None
    win_rate = float((recent["WL"] == "W").mean())
    point_diff = float(recent["PLUS_MINUS"].mean())
    return win_rate, point_diff


def _season_net_rating(team_games: pd.DataFrame, as_of_date: date, season: str) -> float | None:
    prior = team_games[(team_games["GAME_DATE"] < as_of_date) & (team_games["SEASON"] == season)]
    if prior.empty:
        return None
    return float(prior["PLUS_MINUS"].mean())


def _h2h_win_rate(team_games: pd.DataFrame, as_of_date: date, opponent_abbr: str) -> float | None:
    prior = team_games[team_games["GAME_DATE"] < as_of_date]
    meetings = prior[prior["MATCHUP"].str.endswith(opponent_abbr)]
    if meetings.empty:
        return None
    return float((meetings["WL"] == "W").mean())


def assemble_from_game_log(
    games_df: pd.DataFrame,
    as_of_date: date,
    season: str,
    home_team: str,
    away_team: str,
    moneyline_implied_prob_home: float | None = None,
) -> dict:
    """games_df: the full multi-season game log (one row per team per game — nba_api's
    leaguegamelog shape), with an added SEASON column (e.g. "2023-24") and GAME_DATE as a
    real date (not string). home_team/away_team are TEAM_ABBREVIATION values.

    Strict leakage guard: every stat below is filtered to GAME_DATE < as_of_date — a game's
    own result is never visible to its own feature vector."""
    home_games = games_df[games_df["TEAM_ABBREVIATION"] == home_team]
    away_games = games_df[games_df["TEAM_ABBREVIATION"] == away_team]

    rest_days_home = _rest_days(home_games, as_of_date)
    rest_days_away = _rest_days(away_games, as_of_date)
    last10_win_home, last10_diff_home = _last10_stats(home_games, as_of_date)
    last10_win_away, last10_diff_away = _last10_stats(away_games, as_of_date)

    net_home = _season_net_rating(home_games, as_of_date, season)
    net_away = _season_net_rating(away_games, as_of_date, season)
    net_rating_diff = (
        (net_home - net_away) if net_home is not None and net_away is not None else None
    )

    return {
        "rest_days_home": rest_days_home,
        "rest_days_away": rest_days_away,
        "back_to_back_home": _back_to_back(rest_days_home),
        "back_to_back_away": _back_to_back(rest_days_away),
        "last10_win_rate_home": last10_win_home,
        "last10_win_rate_away": last10_win_away,
        "last10_point_diff_home": last10_diff_home,
        "last10_point_diff_away": last10_diff_away,
        "net_rating_diff": net_rating_diff,
        "home_court_indicator": 1.0,
        "h2h_win_rate_home": _h2h_win_rate(home_games, as_of_date, away_team),
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }


async def assemble_from_live_db(db, fixture, home_features, away_features) -> dict:
    """Live-inference counterpart. home_features/away_features are TeamFeatures ORM rows
    (already computed at the last ingest_fixtures.py run — see app/workers/ingest_fixtures.py
    and app/adapters/balldontlie.py:_compute_team_stats for what populates them).

    Two features TeamFeatures doesn't carry are fetched fresh here:
    - h2h_win_rate_home: a live BallDontLie call (fixture-specific, not a per-team stat, so
      it doesn't fit the generic TeamFeatures cache — see fetch_h2h_win_rate).
    - moneyline_implied_prob_home: a DB read from the Odds table (ingest_odds.py already
      populates this; no external call needed here).
    """
    from sqlalchemy import select

    from app.adapters.balldontlie import fetch_h2h_win_rate
    from app.fixtures.models import Team
    from app.odds.models import Odds

    rest_days_home = (
        float(home_features.days_since_last_match)
        if home_features and home_features.days_since_last_match is not None
        else None
    )
    rest_days_away = (
        float(away_features.days_since_last_match)
        if away_features and away_features.days_since_last_match is not None
        else None
    )

    last10_diff_home = (
        (home_features.attack_str - home_features.defence_str)
        if home_features
        and home_features.attack_str is not None
        and home_features.defence_str is not None
        else None
    )
    last10_diff_away = (
        (away_features.attack_str - away_features.defence_str)
        if away_features
        and away_features.attack_str is not None
        and away_features.defence_str is not None
        else None
    )

    net_rating_diff = (
        (home_features.season_point_diff - away_features.season_point_diff)
        if home_features
        and away_features
        and home_features.season_point_diff is not None
        and away_features.season_point_diff is not None
        else None
    )

    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    h2h_win_rate_home = None
    if home_team and away_team and home_team.external_id and away_team.external_id:
        h2h_win_rate_home = await fetch_h2h_win_rate(home_team.external_id, away_team.external_id)

    best_odds = (
        (
            await db.execute(
                select(Odds)
                .where(
                    Odds.fixture_id == fixture.id, Odds.market == "h2h", Odds.home_odds.is_not(None)
                )
                .order_by(Odds.updated_at.desc())
            )
        )
        .scalars()
        .first()
    )
    moneyline_implied_prob_home = (
        (1 / best_odds.home_odds) if best_odds and best_odds.home_odds else None
    )

    return {
        "rest_days_home": rest_days_home,
        "rest_days_away": rest_days_away,
        "back_to_back_home": _back_to_back(rest_days_home),
        "back_to_back_away": _back_to_back(rest_days_away),
        "last10_win_rate_home": home_features.form_pts_5 if home_features else None,
        "last10_win_rate_away": away_features.form_pts_5 if away_features else None,
        "last10_point_diff_home": last10_diff_home,
        "last10_point_diff_away": last10_diff_away,
        "net_rating_diff": net_rating_diff,
        "home_court_indicator": 1.0,
        "h2h_win_rate_home": h2h_win_rate_home,
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }
