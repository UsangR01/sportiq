"""Shared tennis (ATP + WTA) feature-vector assembly — the single source of truth for both
training (ml/training/train_tennis.py, via assemble_from_game_log) and live inference
(app/workers/run_predictions.py, via assemble_from_live_db). Mirrors
app/models_ml/nba_features.py's role and train/serve-parity rationale exactly — the model is
NBA-shaped (single binary classifier, no draw), not football's two-layer xG stack, since
tennis is also a 2-outcome, no-draw sport.

Deliberate omissions, relative to the NBA/football templates:
  - No home_court_indicator: a real NBA signal (genuine home-court advantage) with no tennis
    analog — which player is "home" vs "away" is an arbitrary positional label (BallDontLie's
    own player1/player2 slots), not a venue. Including a fabricated constant here would
    violate this codebase's own "never fabricate a neutral value" convention.
  - No attack_str/defence_str/xg_for_5/xg_against_5/season_point_diff/home_win_rate/
    away_win_rate: all goals-scoring or home-court concepts with no tennis equivalent — the
    adapter's TeamStats already leaves these None (see app/adapters/balldontlie_tennis.py),
    so there's nothing to read here.
  - rank_diff (real ATP/WTA ranking points, TeamStats.rank_points/TeamFeatures.rank_points)
    is used INSTEAD of a hand-rolled Elo approximation, unlike football (which added Elo
    later from the user's own notebook) — real, provider-computed, simpler and more honest.
    Team.elo_rating still auto-populates for tennis via the generic, sport-agnostic
    settlement path in app/workers/ingest_fixtures.py (Outcome.home_score/away_score = sets
    won), so a real elo_diff feature is available as a free future addition — not included
    here for v1, matching the plan's explicit scope decision.
  - key_players_available_*/key_players_per_combined_*: TeamKeyPlayer models "a team is
    composed of multiple individually-tracked players" — not applicable to an individual
    sport. Simply never populated for tennis; every read path already degrades gracefully.

The 10 features (this module's FEATURE_NAMES, in order):
  rank_diff, form_win_rate_home, form_win_rate_away, days_since_last_match_home,
  days_since_last_match_away, win_streak_home, win_streak_away, h2h_win_rate_home,
  surface_win_rate_home, surface_win_rate_away, moneyline_implied_prob_home.

surface_win_rate_home/away is fixture-specific (the CURRENT tournament's surface) and is
deliberately NOT a cached TeamFeatures column — see
app/adapters/balldontlie_tennis.py:fetch_surface_win_rate's own docstring. It's fetched live
in assemble_from_live_db, and passed into assemble_from_game_log by the caller for training
(train_tennis.py already has each historical match's own surface in the game log).

moneyline_implied_prob_home is always None for v1 — tennis odds are an explicit fast-follow,
not v1 scope (BallDontLie's own /odds needs GOAT tier; TheRundown tennis coverage is
unconfirmed). The Odds-table read below is included anyway (harmless, mirrors nba_features.py
exactly) so this starts working automatically the moment odds are wired up, with zero change
to this module.

Missing data is represented as None throughout (never a fabricated neutral value), matching
nba_features.py's own rationale exactly.
"""

from datetime import date

import pandas as pd

FEATURE_NAMES = (
    "rank_diff",
    "form_win_rate_home",
    "form_win_rate_away",
    "days_since_last_match_home",
    "days_since_last_match_away",
    "win_streak_home",
    "win_streak_away",
    "h2h_win_rate_home",
    "surface_win_rate_home",
    "surface_win_rate_away",
    "moneyline_implied_prob_home",
)

LAST_N_FORM = 10  # matches nba_features.py's own window convention


def _rest_days(prior_sorted_desc: pd.DataFrame, as_of_date: date) -> float | None:
    if prior_sorted_desc.empty:
        return None
    return float((as_of_date - prior_sorted_desc["GAME_DATE"].max()).days)


def _last_n_win_rate(prior_sorted_desc: pd.DataFrame) -> float | None:
    recent = prior_sorted_desc.head(LAST_N_FORM)
    if recent.empty:
        return None
    return float((recent["WL"] == "W").mean())


def _current_streak(prior_sorted_desc: pd.DataFrame) -> tuple[float | None, float | None]:
    """Walking backward from the most recent prior match — exactly one of the two is ever
    positive, mirroring app/adapters/balldontlie_tennis.py:_current_streak's own convention
    (kept as a separate implementation since one operates on a DataFrame, the other on a list
    of dicts — same logic, different data shape)."""
    if prior_sorted_desc.empty:
        return None, None
    win_streak = 0
    losing_streak = 0
    for _, row in prior_sorted_desc.iterrows():
        won = row["WL"] == "W"
        if win_streak == 0 and losing_streak == 0:
            if won:
                win_streak = 1
            else:
                losing_streak = 1
        elif win_streak > 0:
            if won:
                win_streak += 1
            else:
                break
        else:
            if not won:
                losing_streak += 1
            else:
                break
    return float(win_streak), float(losing_streak)


def _h2h_win_rate(prior_sorted_desc: pd.DataFrame, opponent_id: str) -> float | None:
    meetings = prior_sorted_desc[prior_sorted_desc["OPPONENT_ID"] == opponent_id]
    if meetings.empty:
        return None
    return float((meetings["WL"] == "W").mean())


def _surface_win_rate(prior_sorted_desc: pd.DataFrame, surface: str | None) -> float | None:
    if not surface:
        return None
    same_surface = prior_sorted_desc[prior_sorted_desc["SURFACE"] == surface]
    if same_surface.empty:
        return None
    return float((same_surface["WL"] == "W").mean())


def assemble_from_game_log(
    games_df: pd.DataFrame,
    as_of_date: date,
    home_player: str,
    away_player: str,
    home_rank_points: float | None = None,
    away_rank_points: float | None = None,
    moneyline_implied_prob_home: float | None = None,
) -> dict:
    """games_df: one row per player per match (PLAYER_ID, GAME_DATE as a real date, WL
    ("W"/"L"), OPPONENT_ID, SURFACE) — ml/training/collect_tennis_data.py's shape.
    home_player/away_player are PLAYER_ID values.

    Strict leakage guard: every stat below is filtered to GAME_DATE < as_of_date — a match's
    own result is never visible to its own feature vector, same mechanism as
    nba_features.py's own GAME_DATE < as_of_date filtering.

    home_rank_points/away_rank_points are passed in by the caller (train_tennis.py), which
    already has each historical match's own point-in-time ranking from the collected rankings
    history — kept out of this function so it never has to guess which of several ranking
    snapshots was "current" as of a specific past date."""
    home_games = games_df[games_df["PLAYER_ID"] == home_player].sort_values(
        "GAME_DATE", ascending=False
    )
    away_games = games_df[games_df["PLAYER_ID"] == away_player].sort_values(
        "GAME_DATE", ascending=False
    )

    home_prior = home_games[home_games["GAME_DATE"] < as_of_date]
    away_prior = away_games[away_games["GAME_DATE"] < as_of_date]

    win_streak_home, _ = _current_streak(home_prior)
    win_streak_away, _ = _current_streak(away_prior)

    # The surface of the match actually being featurized — read off home_player's own row for
    # as_of_date (the training example's own game log entry), not a prior match's surface.
    surface_rows = games_df[
        (games_df["GAME_DATE"] == as_of_date) & (games_df["PLAYER_ID"] == home_player)
    ]
    surface = surface_rows.iloc[0]["SURFACE"] if not surface_rows.empty else None

    rank_diff = (
        (home_rank_points - away_rank_points)
        if home_rank_points is not None and away_rank_points is not None
        else None
    )

    return {
        "rank_diff": rank_diff,
        "form_win_rate_home": _last_n_win_rate(home_prior),
        "form_win_rate_away": _last_n_win_rate(away_prior),
        "days_since_last_match_home": _rest_days(home_prior, as_of_date),
        "days_since_last_match_away": _rest_days(away_prior, as_of_date),
        "win_streak_home": win_streak_home,
        "win_streak_away": win_streak_away,
        "h2h_win_rate_home": _h2h_win_rate(home_prior, away_player),
        "surface_win_rate_home": _surface_win_rate(home_prior, surface),
        "surface_win_rate_away": _surface_win_rate(away_prior, surface),
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }


async def assemble_from_live_db(db, fixture, home_features, away_features) -> dict:
    """Live-inference counterpart. home_features/away_features are TeamFeatures ORM rows
    (computed at the last ingest_fixtures.py run — see
    app/adapters/balldontlie_tennis.py:_compute_team_stats for what populates them).

    h2h_win_rate_home and surface_win_rate_home/away are live BallDontLie calls (fixture-
    specific, don't fit the generic TeamFeatures cache — see
    app/adapters/balldontlie_tennis.py's fetch_h2h_win_rate_tennis/fetch_surface_win_rate
    docstrings). moneyline_implied_prob_home reads the Odds table exactly like
    nba_features.py does — always None until tennis odds are wired up (fast-follow, not v1
    scope), but this starts working automatically the moment they are, with no code change
    here."""
    from sqlalchemy import select

    from app.adapters.balldontlie_tennis import fetch_h2h_win_rate_tennis, fetch_surface_win_rate
    from app.fixtures.models import Team
    from app.odds.models import Odds
    from app.sports.models import League

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

    rank_diff = (
        (home_features.rank_points - away_features.rank_points)
        if home_features
        and away_features
        and home_features.rank_points is not None
        and away_features.rank_points is not None
        else None
    )

    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    league = (
        await db.execute(select(League).where(League.id == fixture.league_id))
    ).scalar_one_or_none()

    h2h_win_rate_home = None
    surface_win_rate_home = None
    surface_win_rate_away = None
    if home_team and away_team and league and home_team.external_id and away_team.external_id:
        tour = league.slug
        h2h_win_rate_home = await fetch_h2h_win_rate_tennis(
            tour, home_team.external_id, away_team.external_id
        )
        if fixture.external_id:
            surface_win_rate_home = await fetch_surface_win_rate(
                tour, home_team.external_id, fixture.external_id
            )
            surface_win_rate_away = await fetch_surface_win_rate(
                tour, away_team.external_id, fixture.external_id
            )

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
        "rank_diff": rank_diff,
        "form_win_rate_home": home_features.form_pts_5 if home_features else None,
        "form_win_rate_away": away_features.form_pts_5 if away_features else None,
        "days_since_last_match_home": rest_days_home,
        "days_since_last_match_away": rest_days_away,
        "win_streak_home": home_features.win_streak if home_features else None,
        "win_streak_away": away_features.win_streak if away_features else None,
        "h2h_win_rate_home": h2h_win_rate_home,
        "surface_win_rate_home": surface_win_rate_home,
        "surface_win_rate_away": surface_win_rate_away,
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }
