"""Big3/Top5 key player availability feature (TDD §2.1/§3.3), football's Stage 1 — the
direct analogue of app/models_ml/nba_key_players.py's Stage 1, using API-Football's Pro-tier
/players endpoint (confirmed live: real per-match `games.rating` per player — see CLAUDE.md).

Unlike NBA, which had to hand-derive two separate approximations (WS/48, PER) because
Basketball-Reference's real advanced-stat formulas aren't practically reconstructible here,
API-Football's own `rating` is already a real, provider-computed per-match value — simpler
AND more defensible than an approximation, so it's used directly as BOTH the ranking metric
and the combined metric (TeamKeyPlayer.rank_metric == TeamKeyPlayer.combined_metric for every
football row, by design, not an oversight).

Gating pool is season-TOTAL minutes, not a per-game average like NBA's 26/18 MPG bands — a
90-minute match and a ~38-match domestic season don't map onto NBA's 48-minute/82-game
convention, and squad rotation makes a per-appearance average a less meaningful cutoff than
"how much of the season did this player actually play." ~900 minutes is roughly 10 full
matches (a clear starter); the 450-minute fallback band is roughly 5 full matches, mirroring
NBA's two-band high/fallback structure.

Stage 2 (pre-game, forward-looking) lives in app/models_ml/key_player_availability.py, shared
unchanged with NBA — see that module and TeamKeyPlayer's docstring in app/fixtures/models.py.
"""

MINUTES_HIGH_BAND = 900.0
MINUTES_LOW_BAND = 450.0
TOP_N = 5


def select_top5(players: list[dict]) -> list[dict]:
    """players: dicts with at least player_id/player_name/minutes/rating. Ranks by rating
    among the 900+ season-minutes pool; falls back to merging in the 450-900 minute band if
    fewer than 5 qualify at 900+. Returns up to 5, best (rank 1) first — fewer than 5 only if
    the combined pool itself has fewer than 5 players."""
    high_pool = [p for p in players if p.get("minutes", 0.0) >= MINUTES_HIGH_BAND]
    if len(high_pool) >= TOP_N:
        candidates = high_pool
    else:
        mid_pool = [
            p for p in players if MINUTES_LOW_BAND <= p.get("minutes", 0.0) < MINUTES_HIGH_BAND
        ]
        candidates = high_pool + mid_pool

    ranked = sorted(candidates, key=lambda p: p["rating"], reverse=True)
    return ranked[:TOP_N]
