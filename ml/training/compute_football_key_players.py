"""Stage 1 (TDD §3.3, "Big3/Top5 Key Player Availability Feature"): season-level Top 5 key
player identification per EPL team, using API-Football's real Pro-tier data (see CLAUDE.md).

Mirrors ml/training/compute_key_players.py's structure (NBA's Stage 1 driver): this script is
the thin API-fetching + DB-writing wrapper; the pure ranking logic lives in
app/models_ml/football_key_players.py. Run once per season; safe to re-run (upserts by
deleting and re-inserting each team+season's rows).

Scope: EPL only for the first real trained model (see CLAUDE.md's scope decision) — the other
4 leagues' Sport/League rows are seeded and generically supported, but historical player data
collection currently targets league_id 39 (EPL) across 5 seasons only.

Usage (from repo root):
    backend/.venv/Scripts/python ml/training/compute_football_key_players.py
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is needed explicitly

from sqlalchemy import delete, select  # noqa: E402

from app.adapters.api_football import BASE_URL, LEAGUE_IDS  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import TeamKeyPlayer  # noqa: E402
from app.fixtures.service import get_or_create_team  # noqa: E402
from app.models_ml.football_key_players import select_top5  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402

TARGET_LEAGUE_SLUG = "epl"
TARGET_LEAGUE_ID = LEAGUE_IDS[TARGET_LEAGUE_SLUG]

# TRAIN=[2021,2022,2023], VAL=2024, TEST=2025 — same 5-season window
# ml/training/collect_football_data.py/train_football.py use.
SEASONS = [2021, 2022, 2023, 2024, 2025]

MAX_PAGES = 30  # safety cap on /players pagination per team


def _client() -> httpx.AsyncClient:
    api_key = get_settings().api_football_key
    return httpx.AsyncClient(base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0)


async def _fetch_teams(client: httpx.AsyncClient, season: int) -> list[dict]:
    response = await client.get(
        "/teams", params={"league": TARGET_LEAGUE_ID, "season": season}
    )
    response.raise_for_status()
    return [row["team"] for row in response.json().get("response", [])]


async def _fetch_team_players(client: httpx.AsyncClient, team_id: int, season: int) -> list[dict]:
    """Paginated — a squad's full player list rarely exceeds 2-3 pages at this endpoint's page
    size. Filters each player's per-competition `statistics` array to our target league (a
    player can have entries for a cup competition too — never assume statistics[0], confirmed
    live, see CLAUDE.md)."""
    players: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = await client.get(
            "/players", params={"team": team_id, "season": season, "page": page}
        )
        response.raise_for_status()
        payload = response.json()
        for row in payload.get("response", []):
            player = row["player"]
            league_stats = next(
                (
                    s
                    for s in row.get("statistics", [])
                    if s.get("league", {}).get("id") == TARGET_LEAGUE_ID
                ),
                None,
            )
            if league_stats is None:
                continue
            games = league_stats.get("games", {})
            minutes = games.get("minutes")
            rating_raw = games.get("rating")
            appearances = games.get("appearences") or 0  # API-Football's own spelling
            if minutes is None or rating_raw is None:
                continue
            players.append(
                {
                    "player_id": str(player["id"]),
                    "player_name": player["name"],
                    "minutes": float(minutes),
                    "rating": float(rating_raw),
                    # Per-appearance average, distinct from the season-total "minutes" gating
                    # field above — TeamKeyPlayer.mpg is genuinely "minutes per game/
                    # appearance" (matches NBA's PerGame-mode MIN), not a season total.
                    "mpg": (float(minutes) / appearances) if appearances else 0.0,
                }
            )

        paging = payload.get("paging", {})
        if page >= paging.get("total", 1):
            break
    return players


async def compute_and_store_season(season: int) -> None:
    print(f"computing key players for EPL {season}-{season + 1}...")

    async with async_session_factory() as db:
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one_or_none()
        if sport is None:
            raise RuntimeError(
                "football Sport row doesn't exist yet — run scripts/seed_sports.py first"
            )
        league = (
            await db.execute(
                select(League).where(
                    League.sport_id == sport.id, League.slug == TARGET_LEAGUE_SLUG
                )
            )
        ).scalar_one_or_none()
        if league is None:
            raise RuntimeError(
                "epl League row doesn't exist yet — run scripts/seed_sports.py first"
            )

        async with _client() as client:
            teams = await _fetch_teams(client, season)

            now = datetime.now(UTC)
            written = 0
            for team in teams:
                team_row = await get_or_create_team(
                    db,
                    sport_id=sport.id,
                    league_id=league.id,
                    external_id=str(team["id"]),
                    name=team["name"],
                    short_name=team.get("code") or team["name"],
                )

                players = await _fetch_team_players(client, team["id"], season)
                top5 = select_top5(players)

                await db.execute(
                    delete(TeamKeyPlayer).where(
                        TeamKeyPlayer.team_id == team_row.id,
                        TeamKeyPlayer.season_year == season,
                    )
                )
                for rank, player in enumerate(top5, start=1):
                    db.add(
                        TeamKeyPlayer(
                            team_id=team_row.id,
                            season_year=season,
                            player_rank=rank,
                            player_id=player["player_id"],
                            player_name=player["player_name"],
                            rank_metric=player["rating"],
                            combined_metric=player["rating"],
                            mpg=player["mpg"],
                            computed_at=now,
                        )
                    )
                written += len(top5)

            await db.commit()

    print(f"  {written} team_key_players rows written across {len(teams)} teams")


async def main_async() -> None:
    for season in SEASONS:
        await compute_and_store_season(season)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
