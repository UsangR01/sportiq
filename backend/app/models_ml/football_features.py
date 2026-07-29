"""Shared football feature-vector assembly — the single source of truth for both training
(ml/training/train_football.py, via assemble_from_game_log) and live inference
(app/workers/run_predictions.py, via assemble_from_live_db). Mirrors
app/models_ml/nba_features.py's role and train/serve-parity rationale exactly — see that
module's docstring for why this split exists at all.

This is the Layer-1 (Poisson xG engine) input vector per TDD §3.2 — FootballModel.predict
loads these features, runs them through the trained Poisson regressors to get expected goals
for both sides, then feeds those xG values plus a subset of these same contextual features
into the Layer-2 1X2 classifier. See app/models_ml/football.py.

No elo_rating/xg_for_5/xg_against_5 fields: confirmed live (see CLAUDE.md) that API-Football's
/teams/statistics has no xG data at any tier, and no Elo source exists either — same honest
per-sport omission as NBA's pace differential, not a silent gap.

The 16 features (this module's FEATURE_NAMES, in order):
  attack_str_home, attack_str_away, defence_str_home, defence_str_away, form_pts_home,
  form_pts_away, home_win_rate_home, away_win_rate_away, rest_days_home, rest_days_away,
  h2h_win_rate_home, key_players_available_home, key_players_available_away,
  key_players_per_combined_home, key_players_per_combined_away, moneyline_implied_prob_home.

Missing data is represented as None throughout (never a fabricated neutral value) — mirrors
nba_features.py's own rationale exactly.
"""

from datetime import date

import pandas as pd

FEATURE_NAMES = (
    "attack_str_home",
    "attack_str_away",
    "defence_str_home",
    "defence_str_away",
    "form_pts_home",
    "form_pts_away",
    "home_win_rate_home",
    "away_win_rate_away",
    "rest_days_home",
    "rest_days_away",
    "h2h_win_rate_home",
    "key_players_available_home",
    "key_players_available_away",
    "key_players_per_combined_home",
    "key_players_per_combined_away",
    "moneyline_implied_prob_home",
)

# TeamStats/TeamFeatures' existing "_5" column-naming convention (form_pts_5, xg_for_5,
# xg_against_5) already implies football's own "last 5" rolling-form window — NBA reused the
# same columns with a documented "actually last 10" override (see nba_features.py); football
# is the sport those names were originally shaped around.
LAST_N_FORM = 5
POINTS = {"W": 3, "D": 1, "L": 0}


def _rest_days(team_games: pd.DataFrame, as_of_date: date) -> float | None:
    prior = team_games[team_games["GAME_DATE"] < as_of_date]
    if prior.empty:
        return None
    return float((as_of_date - prior["GAME_DATE"].max()).days)


def _rolling_form(team_games: pd.DataFrame, as_of_date: date) -> tuple[float | None, float | None]:
    """Returns (attack_str, form_pts) over the last LAST_N_FORM matches strictly before
    as_of_date — leakage guard identical to nba_features.py's own filter."""
    prior = team_games[team_games["GAME_DATE"] < as_of_date].sort_values(
        "GAME_DATE", ascending=False
    )
    recent = prior.head(LAST_N_FORM)
    if recent.empty:
        return None, None
    attack_str = float(recent["GF"].mean())
    form_pts = float(recent["WDL"].map(POINTS).mean())
    return attack_str, form_pts


def _defence_str(team_games: pd.DataFrame, as_of_date: date) -> float | None:
    prior = team_games[team_games["GAME_DATE"] < as_of_date].sort_values(
        "GAME_DATE", ascending=False
    )
    recent = prior.head(LAST_N_FORM)
    if recent.empty:
        return None
    return float(recent["GA"].mean())


def _side_win_rate(team_games: pd.DataFrame, as_of_date: date, home_away: str) -> float | None:
    prior = team_games[
        (team_games["GAME_DATE"] < as_of_date) & (team_games["HOME_AWAY"] == home_away)
    ]
    if prior.empty:
        return None
    return float((prior["WDL"] == "W").mean())


def _h2h_win_rate(team_games: pd.DataFrame, as_of_date: date, opponent_id: str) -> float | None:
    prior = team_games[team_games["GAME_DATE"] < as_of_date]
    meetings = prior[prior["OPPONENT_ID"] == opponent_id]
    if meetings.empty:
        return None
    return float((meetings["WDL"] == "W").mean())


def assemble_from_game_log(
    games_df: pd.DataFrame,
    as_of_date: date,
    home_team_id: str,
    away_team_id: str,
    moneyline_implied_prob_home: float | None = None,
    key_players_available_home: float | None = None,
    key_players_available_away: float | None = None,
    key_players_per_combined_home: float | None = None,
    key_players_per_combined_away: float | None = None,
) -> dict:
    """games_df: one row per team per fixture (ml/training/collect_football_data.py's own
    shape — TEAM_ID/OPPONENT_ID/GAME_DATE/GF/GA/WDL/HOME_AWAY), analogous to nba_features.py's
    nba_api leaguegamelog shape. home_team_id/away_team_id are API-Football external team IDs
    (strings), not internal UUIDs — matches how the rest of this codebase keys off provider
    IDs during training (see TEAM_ABBREVIATION's role in nba_features.py).

    Strict leakage guard: every stat is filtered to GAME_DATE < as_of_date.

    key_players_available_*/key_players_per_combined_* are passed in, computed by the caller
    from box-score/lineup presence (ml/training/train_football.py's historical backtest-label
    function) — kept out of this module deliberately, mirroring nba_features.py's own
    separation from app/models_ml/key_player_availability.py's live Stage 2 lookup."""
    home_games = games_df[games_df["TEAM_ID"] == home_team_id]
    away_games = games_df[games_df["TEAM_ID"] == away_team_id]

    attack_home, form_home = _rolling_form(home_games, as_of_date)
    attack_away, form_away = _rolling_form(away_games, as_of_date)
    defence_home = _defence_str(home_games, as_of_date)
    defence_away = _defence_str(away_games, as_of_date)

    return {
        "attack_str_home": attack_home,
        "attack_str_away": attack_away,
        "defence_str_home": defence_home,
        "defence_str_away": defence_away,
        "form_pts_home": form_home,
        "form_pts_away": form_away,
        "home_win_rate_home": _side_win_rate(home_games, as_of_date, "home"),
        "away_win_rate_away": _side_win_rate(away_games, as_of_date, "away"),
        "rest_days_home": _rest_days(home_games, as_of_date),
        "rest_days_away": _rest_days(away_games, as_of_date),
        "h2h_win_rate_home": _h2h_win_rate(home_games, as_of_date, away_team_id),
        "key_players_available_home": key_players_available_home,
        "key_players_available_away": key_players_available_away,
        "key_players_per_combined_home": key_players_per_combined_home,
        "key_players_per_combined_away": key_players_per_combined_away,
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }


async def assemble_from_live_db(db, fixture, home_features, away_features) -> dict:
    """Live-inference counterpart — mirrors nba_features.py:assemble_from_live_db's structure
    exactly. home_features/away_features are TeamFeatures ORM rows already computed at the
    last ingest_fixtures.py run (see app/adapters/api_football.py:_compute_team_stats for what
    populates attack_str/defence_str/form_pts_5/home_win_rate/away_win_rate).

    Two features TeamFeatures doesn't carry are fetched fresh here:
    - h2h_win_rate_home: a live API-Football /fixtures/headtohead call.
    - moneyline_implied_prob_home: a DB read from the Odds table.
    """
    from sqlalchemy import select

    from app.adapters.api_football import fetch_h2h_win_rate
    from app.fixtures.models import Team
    from app.odds.models import Odds

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
        "attack_str_home": home_features.attack_str if home_features else None,
        "attack_str_away": away_features.attack_str if away_features else None,
        "defence_str_home": home_features.defence_str if home_features else None,
        "defence_str_away": away_features.defence_str if away_features else None,
        "form_pts_home": home_features.form_pts_5 if home_features else None,
        "form_pts_away": away_features.form_pts_5 if away_features else None,
        "home_win_rate_home": home_features.home_win_rate if home_features else None,
        "away_win_rate_away": away_features.away_win_rate if away_features else None,
        "rest_days_home": (
            float(home_features.days_since_last_match)
            if home_features and home_features.days_since_last_match is not None
            else None
        ),
        "rest_days_away": (
            float(away_features.days_since_last_match)
            if away_features and away_features.days_since_last_match is not None
            else None
        ),
        "h2h_win_rate_home": h2h_win_rate_home,
        "key_players_available_home": (
            home_features.key_players_available if home_features else None
        ),
        "key_players_available_away": (
            away_features.key_players_available if away_features else None
        ),
        "key_players_per_combined_home": (
            home_features.key_players_per_combined if home_features else None
        ),
        "key_players_per_combined_away": (
            away_features.key_players_per_combined if away_features else None
        ),
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }
