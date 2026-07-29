"""Real, standard iterative Elo rating — the approach adopted from the user's own prior NBA
notebook (feature_engineering.ipynb/"Running - NBA Games Prediction Project.ipynb", both added
to the repo root), applied to football rather than re-deriving it from scratch.

Elo is architecturally different from every other football feature in
app/models_ml/football_features.py: attack_str/form_pts/h2h/etc. are all independently
re-derivable from a filtered rolling window over past games (no running state needed between
calls). Elo cannot be: a team's rating is the accumulated result of a sequential walk over
EVERY one of its past games, each one updating both participants at once. That means:

- Training (ml/training/train_football.py): the full historical game log must be walked once,
  in chronological order, to produce each team's Elo rating AS OF just before each fixture —
  see compute_elo_history below. This can't be recomputed independently per training row the
  way _rolling_form etc. are; it's computed once up front and merged in by fixture id.
- Live serving: recomputing the full history on every prediction would be correct but wasteful
  and doesn't fit TeamFeatures' snapshot-per-ingest-run architecture at all. Instead, Team.
  elo_rating is real, persistent, per-team state, updated incrementally exactly once per real
  completed match (app/workers/ingest_fixtures.py:_maybe_settle_outcome, guarded by the same
  "has this fixture already been settled" idempotency check already used for the Outcome row) —
  a genuinely different code path from training's one-shot historical walk, but computing the
  exact same real quantity.
"""

INITIAL_ELO = 1500.0
K_FACTOR = 32.0


def expected_score(rating_a: float, rating_b: float) -> float:
    """Standard Elo expected-score formula — the probability `a` beats `b`."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def apply_match_result(
    elo_home: float, elo_away: float, home_score: int, away_score: int
) -> tuple[float, float]:
    """Standard Elo update for one completed match. Draws count as 0.5 each side, per the
    standard convention (matches the notebook's own approach, extended here to handle football's
    3-way outcome — the notebook's own NBA data had no draws to handle)."""
    if home_score > away_score:
        actual_home = 1.0
    elif home_score < away_score:
        actual_home = 0.0
    else:
        actual_home = 0.5

    expected_home = expected_score(elo_home, elo_away)
    new_elo_home = elo_home + K_FACTOR * (actual_home - expected_home)
    new_elo_away = elo_away + K_FACTOR * ((1.0 - actual_home) - (1.0 - expected_home))
    return new_elo_home, new_elo_away


def compute_elo_history(games_df) -> dict:
    """Walks a full games_df (one row per team per fixture — collect_football_data.py's shape)
    in chronological order, computing each team's Elo rating AS OF just before each fixture.
    Returns {(FIXTURE_ID, TEAM_ID): elo_pre_game} — a pure lookup, not a live/persistent value;
    callers (ml/training/train_football.py) merge this into their per-fixture training rows.

    Only "home"-perspective rows are walked (one row per real match, not two) — games_df's own
    HOME_AWAY column already tags which row that is for a given FIXTURE_ID."""
    home_rows = (
        games_df[games_df["HOME_AWAY"] == "home"]
        .sort_values(["GAME_DATE", "FIXTURE_ID"])
        .itertuples(index=False)
    )

    ratings: dict[str, float] = {}
    elo_pre: dict[tuple, float] = {}

    for row in home_rows:
        home_id = row.TEAM_ID
        away_id = row.OPPONENT_ID
        elo_home = ratings.get(home_id, INITIAL_ELO)
        elo_away = ratings.get(away_id, INITIAL_ELO)

        elo_pre[(row.FIXTURE_ID, home_id)] = elo_home
        elo_pre[(row.FIXTURE_ID, away_id)] = elo_away

        new_home, new_away = apply_match_result(elo_home, elo_away, row.GF, row.GA)
        ratings[home_id] = new_home
        ratings[away_id] = new_away

    return elo_pre
