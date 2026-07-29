"""Stage 1 (TDD §3.3, "Big3/Top5 Key Player Availability Feature"): season-level Top 5 key
player identification per team, using API-Football's real Pro-tier data (see CLAUDE.md).

Mirrors ml/training/compute_key_players.py's structure (NBA's Stage 1 driver): this script is
the thin API-fetching + DB-writing wrapper; the pure ranking logic lives in
app/models_ml/football_key_players.py. Run once per season; safe to re-run (upserts by
deleting and re-inserting each team+season's rows).

Scope: both EPL and Brasileirão now get the full 5-season historical window (2021-2025) — the
retrodiction rebuild (see CLAUDE.md, "past-game predictions use real historical data + real
lineup presence") needs Stage 1 key players for Brasileirão's historical seasons too, not just
its current one, to identify who each team's key players actually were in a given past season.
2026 stays in Brasileirão's list on top of that (its current, mid-season, calendar-year season
— needed for live fixtures/predictions, since a season only just underway wouldn't otherwise
have a Stage 1 row yet). The other 3 European leagues' Sport/League rows are seeded and
generically supported but have no Stage 1 data collected yet.

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
load_dotenv(
    BACKEND_DIR / ".env"
)  # see collect_nba_data.py for why this is needed explicitly

from sqlalchemy import delete, select  # noqa: E402

from app.adapters.api_football import BASE_URL, LEAGUE_IDS  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import TeamKeyPlayer  # noqa: E402
from app.fixtures.service import get_or_create_team  # noqa: E402
from app.models_ml.football_key_players import select_top5  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402

# (league_slug, [seasons]) — see module docstring for why Brasileirão now matches EPL's
# historical depth (plus its own current season on top).
TARGET_LEAGUES: list[tuple[str, list[int]]] = [
    ("epl", [2021, 2022, 2023, 2024, 2025]),
    ("brasileirao", [2021, 2022, 2023, 2024, 2025, 2026]),
]

MAX_PAGES = 30  # safety cap on /players pagination per team
MAX_RETRIES = 5  # mirrors collect_football_data.py's _get_with_retry backoff-on-429 pattern


def _client() -> httpx.AsyncClient:
    api_key = get_settings().api_football_key
    return httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    )


async def _get_with_retry(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    import asyncio

    response = None
    for attempt in range(MAX_RETRIES):
        response = await client.get(path, params=params)
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2**attempt, 30)
            print(f"  429 on {path}, backing off {delay:.0f}s")
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        return response
    response.raise_for_status()
    return response


async def _fetch_teams(
    client: httpx.AsyncClient, league_id: int, season: int
) -> list[dict]:
    response = await _get_with_retry(
        client, "/teams", {"league": league_id, "season": season}
    )
    return [row["team"] for row in response.json().get("response", [])]


async def _fetch_team_players(
    client: httpx.AsyncClient, team_id: int, season: int, league_id: int
) -> list[dict]:
    """Paginated — a squad's full player list rarely exceeds 2-3 pages at this endpoint's page
    size. Filters each player's per-competition `statistics` array to our target league (a
    player can have entries for a cup competition too — never assume statistics[0], confirmed
    live, see CLAUDE.md)."""
    players: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = await _get_with_retry(
            client, "/players", {"team": team_id, "season": season, "page": page}
        )
        payload = response.json()
        for row in payload.get("response", []):
            player = row["player"]
            league_stats = next(
                (
                    s
                    for s in row.get("statistics", [])
                    if s.get("league", {}).get("id") == league_id
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


async def compute_and_store_season(league_slug: str, season: int) -> None:
    league_id = LEAGUE_IDS[league_slug]
    print(f"computing key players for {league_slug} season {season}...")

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
                    League.sport_id == sport.id, League.slug == league_slug
                )
            )
        ).scalar_one_or_none()
        if league is None:
            raise RuntimeError(
                f"{league_slug} League row doesn't exist yet — run scripts/seed_sports.py first"
            )

        async with _client() as client:
            teams = await _fetch_teams(client, league_id, season)

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

                players = await _fetch_team_players(
                    client, team["id"], season, league_id
                )
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
    for league_slug, seasons in TARGET_LEAGUES:
        for season in seasons:
            await compute_and_store_season(league_slug, season)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
