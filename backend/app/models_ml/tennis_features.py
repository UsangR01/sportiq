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

The 14 features (this module's FEATURE_NAMES, in order):
  rank_diff, form_win_rate_home, form_win_rate_away, days_since_last_match_home,
  days_since_last_match_away, win_streak_home, win_streak_away, h2h_win_rate_home,
  h2h_win_rate_surface_home, surface_win_rate_home, surface_win_rate_away,
  surface_streak_home, surface_streak_away, moneyline_implied_prob_home.

h2h_win_rate_surface_home and surface_streak_home/away were added per direct user request,
after the initial 11-feature version shipped — real tennis-domain signals distinct from their
overall counterparts: a player's H2H edge over a specific opponent, and their own form, can
both look meaningfully different on one surface than across their career as a whole (e.g. a
clay-court specialist's H2H record against a hard-court specialist skews toward whichever
surface most of their meetings happened on, which may not be the surface of THIS match).

surface_win_rate_home/away, h2h_win_rate_surface_home, and surface_streak_home/away are all
fixture-specific (the CURRENT tournament's surface) and deliberately NOT cached TeamFeatures
columns — see app/adapters/balldontlie_tennis.py:fetch_surface_stats/fetch_h2h_stats's own
docstrings. They're fetched live in assemble_from_live_db (via one shared
fetch_match_surface call, not re-fetched per feature), and passed into assemble_from_game_log
by the caller for training (train_tennis.py already has each historical match's own surface
in the game log).

moneyline_implied_prob_home is always None for v1 — tennis odds are an explicit fast-follow,
not v1 scope (BallDontLie's own /odds needs GOAT tier; TheRundown tennis coverage is
unconfirmed). The Odds-table read below is included anyway (harmless, mirrors nba_features.py
exactly) so this starts working automatically the moment odds are wired up, with zero change
to this module.

Missing data is represented as None throughout (never a fabricated neutral value), matching
nba_features.py's own rationale exactly.
"""

import math
import os
from datetime import date, timedelta

import pandas as pd

_OPPONENT_FORM_NAMES = (
    "form_vs_expected_home",
    "form_vs_expected_away",
    "opponent_quality_faced_home",
    "opponent_quality_faced_away",
    "rank_momentum_home",
    "rank_momentum_away",
)

# EXPERIMENT TOGGLE for opponent-adjusted form, default OFF pending measurement. Set
# SPORTIQ_TENNIS_OPPONENT_FORM=1 to include the six features above.
#
# THE PROBLEM THEY EXIST FOR: form_win_rate is a flat, opponent-blind win rate, so beating ten
# qualifiers and beating ten top-20 players score identically -- while ATP ranking points are
# explicitly weighted by tournament tier and round reached. That is why a flat win rate can
# never out-argue ranking, and it is the gap the failed rank-scale arm pointed at.
#
# SERVING IS NOT WIRED. Feasible in one batched /rankings?player_ids[]=... call (confirmed
# live, under the 100-id cap), but deliberately deferred until the measurement passes -- two
# consecutive tennis arms have failed. assemble_from_live_db emits these as None, so enabling
# this toggle without doing that work would be a silent train/serve mismatch;
# test_tennis_features.py pins the toggle off to stop that shipping by accident.
_OPPONENT_FORM_FEATURES = os.environ.get("SPORTIQ_TENNIS_OPPONENT_FORM", "0") != "0"

# Fitted on the TRAIN SEASONS ONLY (2021-2023, 19,594 real player-match pairs) and pinned, so
# no fitting touches validation or test: P(win) = logistic(BETA * [log points - log opp points]).
# Calibration on that split: predicted .149/.288/.432/.568/.712/.851 vs actual
# .186/.265/.427/.573/.735/.814 across log-ratio bands.
EXPECTED_WIN_BETA = 0.6460

# Lookback for rank_momentum. Eight weeks spans a couple of tournaments and still means
# "lately" rather than restating the 52-week ranking.
RANK_MOMENTUM_WEEKS = 8

# EXPERIMENT TOGGLE, DEFAULT OFF BECAUSE THE EXPERIMENT FAILED. Set
# SPORTIQ_TENNIS_RANK_SCALE_FEATURES=1 to re-enable rank_position_diff and
# rank_log_points_ratio for a future arm.
#
# These two were built 2026-08-19 to fix a real, measured defect: the model agrees with "back
# the higher-ranked player" 88.4% of the time and 100% of the time once the ranking-points gap
# passes 2,000, because rank_diff is a RAW POINTS SUBTRACTION and points are non-linear in
# position (ten places costs 8,720 points at #1 and 119 at #50). The hypothesis was that
# scale-corrected rank signals would let form compete where the gap is genuinely small.
#
# THE HYPOTHESIS IS DEAD. Measured against a pre-registered bar (train_tennis.py), everything
# else identical and seeded:
#
#                          baseline (14)   treatment (16)   bar
#     ranking gap             +2.15pp         +1.77pp       >= +3.15pp   FAIL (got worse)
#     RPS                      0.2275          0.2302       <= 0.2295    FAIL
#     accuracy                 0.6377          0.6338       >= 0.6327    pass
#     agreement (diagnostic)    87.8%           88.2%        --          rose, did not fall
#
# Reverted by the letter of the pre-registration. The defect is REAL and remains unfixed --
# what is now known is that re-scaling the ranking signal is not the fix, so a future attempt
# should target something else (the honest candidates: features the ranking cannot contain at
# all, such as opponent-adjusted recent form or fatigue within a tournament).
#
# Safe as an env toggle only because app/models_ml/tennis.py reads feature_names FROM THE
# ARTEFACT rather than from this tuple, so a served model always consumes the vector it was
# trained on. Always pass --no-activate when running an arm regardless: this project has twice
# had an experiment's losing arm promote itself, once leaving a sport with no active model at
# all.
_RANK_SCALE_FEATURES = os.environ.get("SPORTIQ_TENNIS_RANK_SCALE_FEATURES", "0") != "0"

FEATURE_NAMES = tuple(
    name
    for name in (
        "rank_diff",
        "rank_position_diff",
        "rank_log_points_ratio",
        "form_win_rate_home",
        "form_win_rate_away",
        "days_since_last_match_home",
        "days_since_last_match_away",
        "win_streak_home",
        "win_streak_away",
        "h2h_win_rate_home",
        "h2h_win_rate_surface_home",
        "surface_win_rate_home",
        "surface_win_rate_away",
        "surface_streak_home",
        "surface_streak_away",
        "moneyline_implied_prob_home",
        *_OPPONENT_FORM_NAMES,
    )
    if (_RANK_SCALE_FEATURES or name not in ("rank_position_diff", "rank_log_points_ratio"))
    and (_OPPONENT_FORM_FEATURES or name not in _OPPONENT_FORM_NAMES)
)

# MEASURED 2026-08-13, having been inherited rather than chosen: this was 10 purely because
# nba_features.py used 10, which itself was 10 because football's columns were named _5. Tennis
# was the most suspect of the three -- players compete in bursts inside a tournament, so ten
# matches can reach back across surfaces and months -- and CLAUDE.md already recorded that
# borrowing NBA's base rates for tennis was indefensible while its window was borrowed in
# silence.
#
# THE INHERITED VALUE SURVIVED. Everything else identical, seeded, n=5 against n=10:
#
#     n=5    test accuracy 0.6312   RPS 0.2298   ranking-baseline gap +1.62pp
#     n=10   test accuracy 0.6377   RPS 0.2275   ranking-baseline gap +2.15pp
#
# 10 wins on all three, so the hypothesis that motivated the test -- players compete in bursts,
# so a shorter window should be fresher -- is DEAD for tennis, exactly as "shorter is fresher"
# died for football at n=3. Kept at 10 on evidence now rather than on inheritance.
#
# The env override exists so the next such test needs no code edit. Pass --no-activate when
# running one: this experiment's LOSING arm registered itself as the active tennis model, and
# because serving reads this default back at 10, it would have fed 10-match form into a model
# trained on 5.
LAST_N_FORM = int(os.environ.get("SPORTIQ_TENNIS_LAST_N_FORM", "10"))


def _expected_win_probability(player_points: float, opponent_points: float) -> float:
    """P(player beats opponent) from ranking points alone — the yardstick the actual result is
    scored against. Logistic on the LOG points ratio rather than the raw difference, because
    points are non-linear in position (ten places costs 8,720 points at #1 and 119 at #50)."""
    return 1.0 / (
        1.0 + math.exp(-EXPECTED_WIN_BETA * (math.log(player_points) - math.log(opponent_points)))
    )


def _form_vs_expected(prior_sorted_desc, as_of_date, player_id, rank_points_at) -> float | None:
    """Wins ABOVE EXPECTATION over the last LAST_N_FORM matches — the opponent-adjusted answer
    to "is this player in form".

    Plain form_win_rate treats beating ten qualifiers and beating ten top-20 players as the
    same 1.0, which is exactly the complaint this exists to fix. Here each match is scored
    against what the rankings said should happen:

        SUM(actual - expected) / n     0 = performed exactly to ranking
                                       + = beating better players than expected
                                       - = beating only who they should have

    A RESIDUAL by construction, which is the whole point: it carries what ranking does not
    already say, unlike the rank-scale arm that merely restated ranking in other units and
    failed. Matches whose opponent has no ranking are skipped rather than assumed average --
    an unranked opponent is genuinely unknown, and scoring them as a coin flip would credit a
    win over a qualifier as if it were a win over a peer."""
    recent = prior_sorted_desc.head(LAST_N_FORM)
    if recent.empty:
        return None
    total = 0.0
    scored = 0
    for row in recent.itertuples():
        own = rank_points_at(player_id, row.GAME_DATE)
        opponent = rank_points_at(row.OPPONENT_ID, row.GAME_DATE)
        if not own or not opponent or own <= 0 or opponent <= 0:
            continue
        actual = 1.0 if row.WL == "W" else 0.0
        total += actual - _expected_win_probability(own, opponent)
        scored += 1
    return (total / scored) if scored else None


def _opponent_quality_faced(prior_sorted_desc, player_id, rank_points_at) -> float | None:
    """Mean LOG ranking points of the last LAST_N_FORM opponents — the raw level of opposition,
    alongside the residual above. Log, not raw, for the same non-linearity reason."""
    recent = prior_sorted_desc.head(LAST_N_FORM)
    if recent.empty:
        return None
    values = []
    for row in recent.itertuples():
        opponent = rank_points_at(row.OPPONENT_ID, row.GAME_DATE)
        if opponent and opponent > 0:
            values.append(math.log(opponent))
    return (sum(values) / len(values)) if values else None


def _rank_momentum(player_id, as_of_date, rank_points_at) -> float | None:
    """log(points now) - log(points RANK_MOMENTUM_WEEKS ago): surging, or coasting on a ranking
    built ten months ago. Ranking points are a 52-week rolling total, so they cannot say this
    themselves — the level and its direction are different facts."""
    now = rank_points_at(player_id, as_of_date)
    then = rank_points_at(player_id, as_of_date - timedelta(weeks=RANK_MOMENTUM_WEEKS))
    if not now or not then or now <= 0 or then <= 0:
        return None
    return float(math.log(now) - math.log(then))


def _rank_position_diff(
    home_rank_position: float | None, away_rank_position: float | None
) -> float | None:
    """Ranking-PLACE difference, positive when the home player is ranked better (a LOWER
    position number). Linear in places, which raw points are emphatically not.

    Added 2026-08-19 alongside rank_log_points_ratio. rank_diff (a raw points subtraction)
    was measured to make the model agree with "back the higher-ranked player" 88.4% of the
    time and 100% of the time once the gap passed 2,000 points -- because ranking points are
    non-linear in position: dropping ten places costs 8,720 points at #1 and 119 at #50, so
    one number cannot separate "#3 v #5" from "#40 v #90". See train_tennis.py's
    pre-registration block for the full measurement and the adoption criteria."""
    if home_rank_position is None or away_rank_position is None:
        return None
    return float(away_rank_position - home_rank_position)


def _rank_log_points_ratio(
    home_rank_points: float | None, away_rank_points: float | None
) -> float | None:
    """log(home points / away points) — positive when the home player is stronger.

    Compresses the points scale so a given ratio means the same thing at the top of the
    field as in the hundreds, where a raw subtraction does not. Requires both sides strictly
    positive: an unranked player carries 0 or None points and log(0) is undefined, so this
    returns None rather than fabricating an extreme value."""
    if not home_rank_points or not away_rank_points:
        return None
    if home_rank_points <= 0 or away_rank_points <= 0:
        return None
    return float(math.log(home_rank_points) - math.log(away_rank_points))


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


def _h2h_win_rate_on_surface(
    prior_sorted_desc: pd.DataFrame, opponent_id: str, surface: str | None
) -> float | None:
    """H2H win rate against this specific opponent, further filtered to meetings played on
    the CURRENT match's surface — a player's overall H2H edge over an opponent can look very
    different on one surface than across their whole history together (see module
    docstring)."""
    if not surface:
        return None
    meetings = prior_sorted_desc[
        (prior_sorted_desc["OPPONENT_ID"] == opponent_id)
        & (prior_sorted_desc["SURFACE"] == surface)
    ]
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


def _surface_streak(prior_sorted_desc: pd.DataFrame, surface: str | None) -> float | None:
    """Current consecutive-WIN streak specifically on this surface (mirrors
    _current_streak's overall version, but pre-filtered to same-surface matches only, and
    only the win-streak side — losing streak is already implied by "not on a win streak",
    same convention as the overall win_streak_home/away features)."""
    if not surface:
        return None
    same_surface = prior_sorted_desc[prior_sorted_desc["SURFACE"] == surface]
    if same_surface.empty:
        return None
    streak = 0
    for _, row in same_surface.iterrows():
        if row["WL"] == "W":
            streak += 1
        else:
            break
    return float(streak)


def assemble_from_game_log(
    games_df: pd.DataFrame,
    as_of_date: date,
    home_player: str,
    away_player: str,
    home_rank_points: float | None = None,
    away_rank_points: float | None = None,
    moneyline_implied_prob_home: float | None = None,
    home_rank_position: float | None = None,
    away_rank_position: float | None = None,
    rank_points_at=None,
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

    # rank_points_at(player_id, on_date) -> points, most recent snapshot on or before that
    # date. Supplied by the caller (train_tennis.py) because it owns the collected rankings
    # history; absent, the six opponent-adjusted features score as missing, which is exactly
    # the tolerance the toggle needs while serving is unwired.
    if rank_points_at is None:
        opponent_form = dict.fromkeys(_OPPONENT_FORM_NAMES)
    else:
        opponent_form = {
            "form_vs_expected_home": _form_vs_expected(
                home_prior, as_of_date, home_player, rank_points_at
            ),
            "form_vs_expected_away": _form_vs_expected(
                away_prior, as_of_date, away_player, rank_points_at
            ),
            "opponent_quality_faced_home": _opponent_quality_faced(
                home_prior, home_player, rank_points_at
            ),
            "opponent_quality_faced_away": _opponent_quality_faced(
                away_prior, away_player, rank_points_at
            ),
            "rank_momentum_home": _rank_momentum(home_player, as_of_date, rank_points_at),
            "rank_momentum_away": _rank_momentum(away_player, as_of_date, rank_points_at),
        }

    return {
        **opponent_form,
        "rank_diff": rank_diff,
        "rank_position_diff": _rank_position_diff(home_rank_position, away_rank_position),
        "rank_log_points_ratio": _rank_log_points_ratio(home_rank_points, away_rank_points),
        "form_win_rate_home": _last_n_win_rate(home_prior),
        "form_win_rate_away": _last_n_win_rate(away_prior),
        "days_since_last_match_home": _rest_days(home_prior, as_of_date),
        "days_since_last_match_away": _rest_days(away_prior, as_of_date),
        "win_streak_home": win_streak_home,
        "win_streak_away": win_streak_away,
        "h2h_win_rate_home": _h2h_win_rate(home_prior, away_player),
        "h2h_win_rate_surface_home": _h2h_win_rate_on_surface(home_prior, away_player, surface),
        "surface_win_rate_home": _surface_win_rate(home_prior, surface),
        "surface_win_rate_away": _surface_win_rate(away_prior, surface),
        "surface_streak_home": _surface_streak(home_prior, surface),
        "surface_streak_away": _surface_streak(away_prior, surface),
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }


async def assemble_from_live_db(db, fixture, home_features, away_features) -> dict:
    """Live-inference counterpart. home_features/away_features are TeamFeatures ORM rows
    (computed at the last ingest_fixtures.py run — see
    app/adapters/balldontlie_tennis.py:_compute_team_stats for what populates them).

    h2h_win_rate_home/h2h_win_rate_surface_home and surface_win_rate_home/away/
    surface_streak_home/away are live BallDontLie calls (fixture-specific, don't fit the
    generic TeamFeatures cache — see app/adapters/balldontlie_tennis.py's fetch_h2h_stats/
    fetch_surface_stats docstrings). The current fixture's surface is fetched ONCE
    (fetch_match_surface) and threaded into both, rather than each independently re-fetching
    it. moneyline_implied_prob_home reads the Odds table exactly like nba_features.py does —
    always None until tennis odds are wired up (fast-follow, not v1 scope), but this starts
    working automatically the moment they are, with no code change here."""
    from sqlalchemy import select

    from app.adapters.balldontlie_tennis import (
        fetch_h2h_stats,
        fetch_match_surface,
        fetch_surface_stats,
    )
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
    h2h_win_rate_surface_home = None
    surface_win_rate_home = None
    surface_win_rate_away = None
    surface_streak_home = None
    surface_streak_away = None
    if home_team and away_team and league and home_team.external_id and away_team.external_id:
        tour = league.slug
        surface = (
            await fetch_match_surface(tour, fixture.external_id) if fixture.external_id else None
        )
        h2h_win_rate_home, h2h_win_rate_surface_home = await fetch_h2h_stats(
            tour, home_team.external_id, away_team.external_id, surface
        )
        surface_win_rate_home, surface_streak_home = await fetch_surface_stats(
            tour, home_team.external_id, surface
        )
        surface_win_rate_away, surface_streak_away = await fetch_surface_stats(
            tour, away_team.external_id, surface
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
        # NOT WIRED YET, and the toggle that would consume them defaults OFF so this is inert
        # rather than a silent mismatch. Serving them needs each of the last 10 opponents'
        # current ranking, which is ONE batched /rankings?player_ids[]=... call (confirmed
        # live, under the 100-id cap) — deferred until the measurement passes.
        **dict.fromkeys(_OPPONENT_FORM_NAMES),
        "rank_diff": rank_diff,
        "rank_position_diff": _rank_position_diff(
            home_features.rank_position if home_features else None,
            away_features.rank_position if away_features else None,
        ),
        "rank_log_points_ratio": _rank_log_points_ratio(
            home_features.rank_points if home_features else None,
            away_features.rank_points if away_features else None,
        ),
        "form_win_rate_home": home_features.form_pts_5 if home_features else None,
        "form_win_rate_away": away_features.form_pts_5 if away_features else None,
        "days_since_last_match_home": rest_days_home,
        "days_since_last_match_away": rest_days_away,
        "win_streak_home": home_features.win_streak if home_features else None,
        "win_streak_away": away_features.win_streak if away_features else None,
        "h2h_win_rate_home": h2h_win_rate_home,
        "h2h_win_rate_surface_home": h2h_win_rate_surface_home,
        "surface_win_rate_home": surface_win_rate_home,
        "surface_win_rate_away": surface_win_rate_away,
        "surface_streak_home": surface_streak_home,
        "surface_streak_away": surface_streak_away,
        "moneyline_implied_prob_home": moneyline_implied_prob_home,
    }
