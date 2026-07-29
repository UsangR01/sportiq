"""Big3/Top5 key player availability feature (TDD §2.1/§3.3), NBA's Stage 1. See
app/models_ml/football_key_players.py for football's Stage 1 counterpart, and
app/models_ml/key_player_availability.py for the shared, sport-agnostic Stage 2 that both
sports use unchanged.

Stage 1 (season-level, backward-looking, leakage-safe): compute_uper/compute_ws48_approx/
select_top5 — pure functions, no network/DB, run offline by ml/training/compute_key_players.py
once per season. Ranks players by trailing WS/48 among a 26+ MPG pool (falling back to 18-26
MPG if fewer than 5 qualify) and writes the result to team_key_players (rank_metric=ws_48,
combined_metric=per — see TeamKeyPlayer's docstring for why those columns are named
generically).

Stage 2 (pre-game, forward-looking) lives in app/models_ml/key_player_availability.py, not
here — it's identical DB-query logic regardless of sport (reads only player_injury_status,
joined to team_key_players by player name), so it isn't duplicated per sport.

The historical training-label counterpart — "did this named Top-5 player appear in this
*already-completed* game's box score" — is intentionally NOT in this module. It lives in
ml/training/train_nba.py as a distinctly-named, clearly-commented function, precisely so it
can never be casually imported into the live Stage 2 path by mistake.

WS/48 and PER here are simplified, clearly-labelled approximations, not the exact published
Basketball-Reference/Hollinger formulas — see each function's docstring.
"""

# Re-exported for backward compatibility with existing imports
# (app.models_ml.nba_key_players.get_key_player_availability) — the real implementation now
# lives in key_player_availability.py since it's shared, unchanged, by football too.
from app.models_ml.key_player_availability import get_key_player_availability  # noqa: F401

MPG_HIGH_BAND = 26.0
MPG_LOW_BAND = 18.0
TOP_N = 5


def compute_uper(player_row: dict, league_avg_raw_per48: float) -> float:
    """Simplified approximation of Hollinger's PER — NOT the exact uPER formula (which needs
    several league-wide constants: VOP, factor, DRB%, etc., derived from the full league's
    box score that aren't practically reconstructible/verifiable without a reference
    dataset). Rewards production, penalises empty possessions (missed shots, turnovers,
    fouls), normalised per-48-minutes and rescaled so the league average lands at PER's own
    15.0-average convention."""
    minutes = player_row.get("MIN") or 0.0
    if minutes <= 0:
        return 0.0

    raw = (
        player_row.get("PTS", 0.0)
        + player_row.get("REB", 0.0)
        + player_row.get("AST", 0.0)
        + player_row.get("STL", 0.0)
        + player_row.get("BLK", 0.0)
        - (player_row.get("FGA", 0.0) - player_row.get("FGM", 0.0))
        - (player_row.get("FTA", 0.0) - player_row.get("FTM", 0.0))
        - player_row.get("TOV", 0.0)
        - 0.5 * player_row.get("PF", 0.0)
    )
    raw_per48 = raw / minutes * 48.0

    if league_avg_raw_per48 == 0:
        return 15.0
    return raw_per48 * (15.0 / league_avg_raw_per48)


def compute_ws48_approx(player_row: dict, team_win_pct: float) -> float:
    """Simplified approximation of Win Shares/48 — NOT Basketball-Reference's actual
    methodology (separate offensive/defensive marginal-value calculations against a
    league-wide "marginal points per win" constant, which similarly aren't practically
    verifiable here). Uses nba_api's own PIE (Player Impact Estimate — an official all-in-one
    advanced stat) as a correlated proxy, scaled toward WS/48's typical ~0.000-0.300 range
    with a ~0.100 league-average convention, weighted modestly by team win rate since Win
    Shares is fundamentally about estimated contribution to wins, not raw box-score value."""
    pie = player_row.get("PIE") or 0.0
    return max(0.0, pie * 2.0 * (0.5 + 0.5 * team_win_pct))


def select_top5(players: list[dict]) -> list[dict]:
    """players: dicts with at least player_id/player_name/mpg/ws_48/per. Ranks by ws_48
    among the 26+ MPG pool (TDD §3.3); falls back to merging in the 18-26 MPG band if fewer
    than 5 qualify at 26+. Returns up to 5, best (rank 1) first — fewer than 5 only if the
    combined pool itself has fewer than 5 players."""
    high_pool = [p for p in players if p.get("mpg", 0.0) >= MPG_HIGH_BAND]
    if len(high_pool) >= TOP_N:
        candidates = high_pool
    else:
        mid_pool = [p for p in players if MPG_LOW_BAND <= p.get("mpg", 0.0) < MPG_HIGH_BAND]
        candidates = high_pool + mid_pool

    ranked = sorted(candidates, key=lambda p: p["ws_48"], reverse=True)
    return ranked[:TOP_N]
