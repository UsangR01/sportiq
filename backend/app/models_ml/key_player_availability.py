"""Stage 2 of the Big3/Top5 key player availability feature (TDD §2.1/§3.3) — shared,
unchanged, across every sport that has a Stage 1 implementation (NBA:
app/models_ml/nba_key_players.py, football: app/models_ml/football_key_players.py). Pre-game,
forward-looking: reads ONLY player_injury_status (RotoWire/BallDontLie for NBA, API-Football
for football), joined to team_key_players by player name (the two tables have no shared
player ID space across any provider pair — see CLAUDE.md). Never reads a box score, lineup
table, or any record of who actually played: that reflects an outcome, not a forecast, and
using it as a live pre-game input would be target leakage (TDD §3.3 PITFALL).

This module has no sport-specific logic at all — the table schema (team_key_players,
player_injury_status) was already sport-agnostic once TeamKeyPlayer's columns were renamed
away from NBA-specific names (ws_48/per -> rank_metric/combined_metric). Only Stage 1 (how a
team's Top 5 gets ranked/scored in the first place) differs per sport.
"""

from sqlalchemy import func, select


async def get_key_player_availability(
    db, team_id, season_year: int
) -> tuple[int | None, float | None]:
    """Returns (key_players_available, key_players_per_combined). (None, None) only when this
    team has no team_key_players rows at all for the season (Stage 1 never ran for them) —
    that's genuinely unknown, unlike a per-player missing injury-status row (see below). Must
    only ever query player_injury_status — never a box score/lineup table."""
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
    combined = 0.0
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
            combined += key_player.combined_metric

    return available_count, combined
