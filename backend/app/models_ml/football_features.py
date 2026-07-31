"""Shared football feature-vector assembly — the single source of truth for both training
(ml/training/train_football.py, via assemble_from_game_log) and live inference
(app/workers/run_predictions.py, via assemble_from_live_db). Mirrors
app/models_ml/nba_features.py's role and train/serve-parity rationale exactly — see that
module's docstring for why this split exists at all.

This is the Layer-1 (Poisson xG engine) input vector per TDD §3.2 — FootballModel.predict
loads these features, runs them through the trained Poisson regressors to get expected goals
for both sides, then feeds those xG values plus a subset of these same contextual features
into the Layer-2 1X2 classifier. See app/models_ml/football.py.

No xg_for_5/xg_against_5 fields: confirmed live (see CLAUDE.md) that API-Football's
/teams/statistics has no xG data at any tier — same honest per-sport omission as NBA's pace
differential, not a silent gap. elo_rating DOES have a real source now (app/models_ml/elo.py) —
adopted from the user's own prior NBA notebook's iterative-Elo approach (feature_engineering
.ipynb/"Running - NBA Games Prediction Project.ipynb"), unlike xG which has no source at all.

The 21 features (this module's FEATURE_NAMES, in order):
  attack_str_home, attack_str_away, defence_str_home, defence_str_away, form_pts_home,
  form_pts_away, home_win_rate_home, away_win_rate_away, rest_days_home, rest_days_away,
  h2h_win_rate_home, h2h_avg_goals_scored_home, h2h_avg_goals_allowed_home,
  key_players_available_home, key_players_available_away, key_players_per_combined_home,
  key_players_per_combined_away, moneyline_implied_prob_home, elo_diff, win_streak_home,
  win_streak_away.

4 more, CORNERS-ONLY (CORNERS_FEATURE_NAMES = FEATURE_NAMES + these 4 — NOT part of
FEATURE_NAMES itself, so Layer 1's goals regressors and Layer 2's 1X2 classifier are
completely unaffected by this addition): corners_for_home, corners_against_home,
corners_for_away, corners_against_away — each team's own rolling average corners won/conceded
over its last LAST_N_FORM real matches. Deliberately kept out of the shared 21: reusing Layer
1's goals-shaped feature vector for the corners regressors was a documented simplification
(see app/models_ml/football.py's prior history), not a permanent design choice — corners
generation correlates with attacking strength but isn't the same signal, and no live provider
exposes a team's own historical corners as an aggregate the way /teams/statistics does for
goals (confirmed live research). The live-serving equivalent instead reads our OWN
fixture_live_state.home_corners/away_corners, populated once per fixture at real settlement
time (app/workers/ingest_fixtures.py:_maybe_fetch_corner_stats) — no new live API call needed,
just our own accumulating history. Training-side, merge_corners_into_game_log() must be called
on the raw game log BEFORE assemble_from_game_log (see ml/training/train_football.py) to attach
the CORNERS_FOR/CORNERS_AGAINST columns _corners_rolling reads; a games_df that never had this
merge applied (e.g. app/workers/backfill_predictions.py's own retrodiction game log) simply
gets None for all four — an honest, accepted gap, same as that module's existing
moneyline_implied_prob_home-is-always-None note, not a crash.

elo_diff (elo_rating_home - elo_rating_away), not two separate elo_rating_home/away columns:
Elo ratings are only meaningful relative to each other (a 1500 vs 1500 matchup and a 1700 vs
1700 matchup are equally "even" despite different absolute levels) — the difference is the
actual signal, matching how the adopted notebook itself only ever used elo_rating relative to
elo_rating_opp. win_streak_home/away (not losing_streak too): a team's losing_streak is already
implied by NOT being on a win_streak in this same match, so carrying both would be redundant
for a two-outcome-adjacent signal — losing_streak is still stored on TeamFeatures/TeamStats for
transparency/debugging, just not duplicated into the model's own feature vector.

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
    "h2h_avg_goals_scored_home",
    "h2h_avg_goals_allowed_home",
    "key_players_available_home",
    "key_players_available_away",
    "key_players_per_combined_home",
    "key_players_per_combined_away",
    "moneyline_implied_prob_home",
    "elo_diff",
    "win_streak_home",
    "win_streak_away",
)

# Corners-regressor-only additions (see module docstring) — never fed to Layer 1's goals
# regressors or Layer 2's 1X2 classifier, only to app/models_ml/football.py's
# corners_home_model/corners_away_model.
CORNERS_FEATURE_NAMES = FEATURE_NAMES + (
    "corners_for_home",
    "corners_against_home",
    "corners_for_away",
    "corners_against_away",
)

# TeamStats/TeamFeatures' existing "_5" column-naming convention (form_pts_5, xg_for_5,
# xg_against_5) already implies football's own "last 5" rolling-form window — NBA reused the
# same columns with a documented "actually last 10" override (see nba_features.py); football
# is the sport those names were originally shaped around.
LAST_N_FORM = 5
POINTS = {"W": 3, "D": 1, "L": 0}


def merge_corners_into_game_log(games: pd.DataFrame, corners: pd.DataFrame) -> pd.DataFrame:
    """Attaches CORNERS_FOR/CORNERS_AGAINST columns to a game log (ml/training/
    collect_football_data.py's games_df shape) from its separately-collected corners frame
    (FIXTURE_ID/TEAM_ID/CORNERS — collect_corner_stats' own shape). Must be called once,
    before assemble_from_game_log, on the FULL pooled game log (mirrors
    app/models_ml/elo.py:compute_elo_history's own "call once on the whole log" requirement) —
    _corners_rolling below reads these two columns directly, no merge logic of its own.

    Left joins (not inner): a fixture whose /fixtures/statistics call never returned a real
    corner count (a genuine, documented ~26-30% historical gap, see collect_corner_stats'
    own docstring) gets NaN here, which pandas' own .mean() already skips — same "never
    fabricate a neutral value" outcome as every other missing-data path in this module,
    with no special-case code needed."""
    own = corners.rename(columns={"CORNERS": "CORNERS_FOR"})[
        ["FIXTURE_ID", "TEAM_ID", "CORNERS_FOR"]
    ]
    games = games.merge(own, on=["FIXTURE_ID", "TEAM_ID"], how="left")

    opponent = corners.rename(columns={"TEAM_ID": "OPPONENT_ID", "CORNERS": "CORNERS_AGAINST"})[
        ["FIXTURE_ID", "OPPONENT_ID", "CORNERS_AGAINST"]
    ]
    return games.merge(opponent, on=["FIXTURE_ID", "OPPONENT_ID"], how="left")


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


def _corners_rolling(
    team_games: pd.DataFrame, as_of_date: date
) -> tuple[float | None, float | None]:
    """Returns (corners_for, corners_against) over the last LAST_N_FORM matches strictly
    before as_of_date — same leakage guard as _rolling_form/_defence_str. Requires
    CORNERS_FOR/CORNERS_AGAINST columns (see merge_corners_into_game_log); a team_games frame
    that never had corners merged in simply returns (None, None) rather than raising a
    KeyError — a real, accepted gap for callers that don't merge (see module docstring)."""
    if "CORNERS_FOR" not in team_games.columns or "CORNERS_AGAINST" not in team_games.columns:
        return None, None
    prior = team_games[team_games["GAME_DATE"] < as_of_date].sort_values(
        "GAME_DATE", ascending=False
    )
    recent = prior.head(LAST_N_FORM)
    corners_for = recent["CORNERS_FOR"].dropna()
    corners_against = recent["CORNERS_AGAINST"].dropna()
    return (
        float(corners_for.mean()) if not corners_for.empty else None,
        float(corners_against.mean()) if not corners_against.empty else None,
    )


def _side_win_rate(team_games: pd.DataFrame, as_of_date: date, home_away: str) -> float | None:
    prior = team_games[
        (team_games["GAME_DATE"] < as_of_date) & (team_games["HOME_AWAY"] == home_away)
    ]
    if prior.empty:
        return None
    return float((prior["WDL"] == "W").mean())


def _h2h_stats(
    team_games: pd.DataFrame, as_of_date: date, opponent_id: str
) -> tuple[float | None, float | None, float | None]:
    """Returns (win_rate, avg_goals_scored, avg_goals_allowed) vs this specific opponent —
    richer than a bare win rate (adopted from the notebook's own H2H approach, which tracked
    both win ratio AND average points scored/allowed vs the specific opponent). All three
    computed from the same filtered meetings — no extra pass needed."""
    prior = team_games[team_games["GAME_DATE"] < as_of_date]
    meetings = prior[prior["OPPONENT_ID"] == opponent_id]
    if meetings.empty:
        return None, None, None
    win_rate = float((meetings["WDL"] == "W").mean())
    avg_scored = float(meetings["GF"].mean())
    avg_allowed = float(meetings["GA"].mean())
    return win_rate, avg_scored, avg_allowed


def _win_streak(team_games: pd.DataFrame, as_of_date: date) -> float | None:
    """Consecutive-win count strictly before as_of_date (0.0 if the most recent result wasn't
    a win) — the training-time analogue of app/adapters/api_football.py:_parse_streaks, adopted
    from the notebook's own boolean-mask-cumsum-groupby streak trick (reimplemented here as a
    plain reversed scan since games_df's per-team slice is small)."""
    prior = team_games[team_games["GAME_DATE"] < as_of_date].sort_values("GAME_DATE")
    if prior.empty:
        return None
    results = prior["WDL"].tolist()
    if results[-1] != "W":
        return 0.0
    streak = 0
    for result in reversed(results):
        if result != "W":
            break
        streak += 1
    return float(streak)


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
    elo_diff: float | None = None,
) -> dict:
    """games_df: one row per team per fixture (ml/training/collect_football_data.py's own
    shape — TEAM_ID/OPPONENT_ID/GAME_DATE/GF/GA/WDL/HOME_AWAY), analogous to nba_features.py's
    nba_api leaguegamelog shape. home_team_id/away_team_id are API-Football external team IDs
    (strings), not internal UUIDs — matches how the rest of this codebase keys off provider
    IDs during training (see TEAM_ABBREVIATION's role in nba_features.py).

    Strict leakage guard: every stat is filtered to GAME_DATE < as_of_date.

    key_players_available_*/key_players_per_combined_*/elo_diff are passed in, computed by the
    caller — key players from box-score/lineup presence (ml/training/train_football.py's
    historical backtest-label function, kept out of this module deliberately, mirroring
    nba_features.py's own separation from app/models_ml/key_player_availability.py's live
    Stage 2 lookup); elo_diff from app/models_ml/elo.py:compute_elo_history, which must walk
    the ENTIRE games_df once in chronological order (a genuinely different, stateful
    computation from every other feature in this function — see elo.py's own docstring for
    why it can't just be another per-call filter like the rest of these)."""
    home_games = games_df[games_df["TEAM_ID"] == home_team_id]
    away_games = games_df[games_df["TEAM_ID"] == away_team_id]

    attack_home, form_home = _rolling_form(home_games, as_of_date)
    attack_away, form_away = _rolling_form(away_games, as_of_date)
    defence_home = _defence_str(home_games, as_of_date)
    defence_away = _defence_str(away_games, as_of_date)
    h2h_win_rate, h2h_avg_scored, h2h_avg_allowed = _h2h_stats(home_games, as_of_date, away_team_id)
    corners_for_home, corners_against_home = _corners_rolling(home_games, as_of_date)
    corners_for_away, corners_against_away = _corners_rolling(away_games, as_of_date)

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
        "h2h_win_rate_home": h2h_win_rate,
        "h2h_avg_goals_scored_home": h2h_avg_scored,
        "h2h_avg_goals_allowed_home": h2h_avg_allowed,
        "key_players_available_home": key_players_available_home,
        "key_players_available_away": key_players_available_away,
        "key_players_per_combined_home": key_players_per_combined_home,
        "key_players_per_combined_away": key_players_per_combined_away,
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
        "elo_diff": elo_diff,
        "win_streak_home": _win_streak(home_games, as_of_date),
        "win_streak_away": _win_streak(away_games, as_of_date),
        "corners_for_home": corners_for_home,
        "corners_against_home": corners_against_home,
        "corners_for_away": corners_for_away,
        "corners_against_away": corners_against_away,
    }


async def _corners_rolling_live(
    db, team_id, n: int = LAST_N_FORM
) -> tuple[float | None, float | None]:
    """Live counterpart to _corners_rolling. No live provider exposes a team's own historical
    corners as an aggregate the way /teams/statistics does for goals (confirmed live research,
    see CLAUDE.md) — so rather than N extra per-fixture API calls, this reads our OWN
    accumulating fixture_live_state.home_corners/away_corners, written once per fixture at real
    settlement time (app/workers/ingest_fixtures.py:_maybe_fetch_corner_stats). team_id is our
    internal UUID (Fixture.home_team_id/away_team_id's own type), not a provider external_id."""
    from sqlalchemy import or_, select

    from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus

    rows = (
        await db.execute(
            select(Fixture, FixtureLiveState)
            .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
            .where(
                or_(Fixture.home_team_id == team_id, Fixture.away_team_id == team_id),
                Fixture.status == FixtureStatus.COMPLETED,
                FixtureLiveState.home_corners.is_not(None),
                FixtureLiveState.away_corners.is_not(None),
            )
            .order_by(Fixture.kickoff_utc.desc())
            .limit(n)
        )
    ).all()
    if not rows:
        return None, None

    corners_for, corners_against = [], []
    for fixture, live_state in rows:
        if fixture.home_team_id == team_id:
            corners_for.append(live_state.home_corners)
            corners_against.append(live_state.away_corners)
        else:
            corners_for.append(live_state.away_corners)
            corners_against.append(live_state.home_corners)
    return float(sum(corners_for) / len(corners_for)), float(
        sum(corners_against) / len(corners_against)
    )


async def assemble_from_live_db(db, fixture, home_features, away_features) -> dict:
    """Live-inference counterpart — mirrors nba_features.py:assemble_from_live_db's structure
    exactly. home_features/away_features are TeamFeatures ORM rows already computed at the
    last ingest_fixtures.py run (see app/adapters/api_football.py:_compute_team_stats for what
    populates attack_str/defence_str/form_pts_5/home_win_rate/away_win_rate).

    Features TeamFeatures doesn't carry, fetched/derived fresh here:
    - h2h_win_rate_home/h2h_avg_goals_scored_home/h2h_avg_goals_allowed_home: one live
      API-Football /fixtures/headtohead call (fetch_h2h_stats — richer than the old win-rate-only
      fetch_h2h_win_rate, same endpoint, no extra call).
    - moneyline_implied_prob_home: a DB read from the Odds table.
    - elo_diff: home_features.elo_rating/away_features.elo_rating are themselves a snapshot,
      taken at ingest time, of Team.elo_rating — the real persistent, incrementally-updated
      value (see app/models_ml/elo.py and app/workers/ingest_fixtures.py:_maybe_settle_outcome).
      No live call needed; this is a live DB value like the other TeamFeatures-sourced fields.
    - corners_for_home/corners_against_home/corners_for_away/corners_against_away
      (CORNERS_FEATURE_NAMES-only, see _corners_rolling_live): our own rolling average from
      fixture_live_state, not a live provider call — see module docstring.
    """
    from sqlalchemy import select

    from app.adapters.api_football import fetch_h2h_stats
    from app.fixtures.models import Team
    from app.odds.models import Odds

    home_team = (
        await db.execute(select(Team).where(Team.id == fixture.home_team_id))
    ).scalar_one_or_none()
    away_team = (
        await db.execute(select(Team).where(Team.id == fixture.away_team_id))
    ).scalar_one_or_none()
    h2h_win_rate_home = None
    h2h_avg_goals_scored_home = None
    h2h_avg_goals_allowed_home = None
    if home_team and away_team and home_team.external_id and away_team.external_id:
        h2h = await fetch_h2h_stats(home_team.external_id, away_team.external_id)
        if h2h is not None:
            h2h_win_rate_home = h2h.win_rate_home
            h2h_avg_goals_scored_home = h2h.avg_goals_scored_home
            h2h_avg_goals_allowed_home = h2h.avg_goals_allowed_home

    elo_diff = None
    if (
        home_features
        and away_features
        and home_features.elo_rating is not None
        and away_features.elo_rating is not None
    ):
        elo_diff = home_features.elo_rating - away_features.elo_rating

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

    corners_for_home = corners_against_home = corners_for_away = corners_against_away = None
    if home_team:
        corners_for_home, corners_against_home = await _corners_rolling_live(db, home_team.id)
    if away_team:
        corners_for_away, corners_against_away = await _corners_rolling_live(db, away_team.id)

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
        "h2h_avg_goals_scored_home": h2h_avg_goals_scored_home,
        "h2h_avg_goals_allowed_home": h2h_avg_goals_allowed_home,
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
        "elo_diff": elo_diff,
        "win_streak_home": home_features.win_streak if home_features else None,
        "win_streak_away": away_features.win_streak if away_features else None,
        "corners_for_home": corners_for_home,
        "corners_against_home": corners_against_home,
        "corners_for_away": corners_for_away,
        "corners_against_away": corners_against_away,
    }
