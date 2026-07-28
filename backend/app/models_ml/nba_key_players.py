"""Big3/Top5 key player availability feature (TDD §2.1/§3.3). Two deliberately separate
stages, computed at two separate times, per the TDD's own design:

Stage 1 (season-level, backward-looking, leakage-safe): compute_uper/compute_ws48_approx/
select_top5 — pure functions, no network/DB, run offline by ml/training/compute_key_players.py
once per season. Ranks players by trailing WS/48 among a 26+ MPG pool (falling back to 18-26
MPG if fewer than 5 qualify) and writes the result to team_key_players.

Stage 2 (pre-game, forward-looking): get_key_player_availability — reads ONLY
player_injury_status (RotoWire, or the BallDontLie fallback), joined to team_key_players by
player name (the two tables have no shared player ID space — see CLAUDE.md). Never reads a
box score, lineup table, or any record of who actually played: that reflects an outcome, not
a forecast, and using it as a live pre-game input would be target leakage (TDD §3.3 PITFALL).

The historical training-label counterpart — "did this named Top-5 player appear in this
*already-completed* game's box score" — is intentionally NOT in this module. It lives in
ml/training/train_nba.py as a distinctly-named, clearly-commented function, precisely so it
can never be casually imported into the live Stage 2 path by mistake.

WS/48 and PER here are simplified, clearly-labelled approximations, not the exact published
Basketball-Reference/Hollinger formulas — see each function's docstring.
"""

from sqlalchemy import func, select

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


async def get_key_player_availability(
    db, team_id, season_year: int
) -> tuple[int | None, float | None]:
    """Stage 2. Returns (key_players_available, key_players_per_combined). (None, None) only
    when this team has no team_key_players rows at all for the season (Stage 1 never ran for
    them) — that's genuinely unknown, unlike a per-player missing injury-status row (see
    below). Must only ever query player_injury_status — never a box score/lineup table."""
    from app.fixtures.models import PlayerInjuryStatus, TeamKeyPlayer

    key_players = (
        (
            await db.execute(
                select(TeamKeyPlayer).where(
                    TeamKeyPlayer.team_id == team_id, TeamKeyPlayer.season_year == season_year
                )
            )
        )
        .scalars()
        .all()
    )
    if not key_players:
        return None, None

    available_count = 0
    per_combined = 0.0
    for key_player in key_players:
        status_row = (
            (
                await db.execute(
                    select(PlayerInjuryStatus)
                    .where(
                        PlayerInjuryStatus.team_id == team_id,
                        func.lower(PlayerInjuryStatus.player_name)
                        == key_player.player_name.lower(),
                    )
                    .order_by(PlayerInjuryStatus.updated_at.desc())
                )
            )
            .scalars()
            .first()
        )

        # No record at all is treated as available: "not on any injury report" is itself
        # informative in real sports-data convention — a deliberate interpretation, since the
        # TDD only describes the case where a status row exists (see CLAUDE.md).
        is_available = status_row is None or status_row.status.value in ("ACTIVE", "PROBABLE")
        if is_available:
            available_count += 1
            per_combined += key_player.per

    return available_count, per_combined
