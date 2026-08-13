"""Backfill fixtures.tournament_name / _surface / _location for tennis fixtures ingested
before those columns existed.

WHY IT MATTERS. Surface is not decoration: the tennis model reads surface_win_rate,
surface_streak and h2h_win_rate_surface, and the per-surface skill cut that gates the tennis
base-rate decision (train_tennis.py:_ranking_baseline_report) can only be reproduced on LIVE
results if settled fixtures carry a surface. Measured 2026-08-13: 328 of 2,367 tennis fixtures
had one, so 86% of the live record could not be cut by surface at all.

CHEAP, because /matches?season=X embeds the whole tournament object in every match — the same
field _map_match_to_fixture_payload already reads. One paginated sweep per season, no
per-fixture lookups, and BallDontLie's ATP tier allows 600 requests/minute.

Only ever FILLS NULLS. A fixture that already carries a surface is left untouched, so a rerun
cannot overwrite real ingested data with a later provider edit.

    PYTHONPATH=. python scripts/backfill_tennis_tournament_meta.py            # dry run
    PYTHONPATH=. python scripts/backfill_tennis_tournament_meta.py --confirm
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.adapters.balldontlie_tennis import (  # noqa: E402
    _external_id,
    _get_with_retry,
    _tour_prefix,
)
from app.core.config import get_settings  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import Fixture  # noqa: E402
from app.sports.models import League, Sport  # noqa: E402

PAGE_SIZE = 100


async def _tournament_meta_by_fixture(tour: str, seasons: list[int]) -> dict[str, dict]:
    """external_id -> {name, surface, location}, from the tournament embedded in each match."""
    settings = get_settings()
    meta: dict[str, dict] = {}
    async with httpx.AsyncClient(
        base_url="https://api.balldontlie.io",
        headers={"Authorization": settings.balldontlie_api_key},
        timeout=30.0,
    ) as client:
        for season in seasons:
            cursor = None
            while True:
                params = {"season": season, "per_page": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
                response = await _get_with_retry(client, f"{_tour_prefix(tour)}/matches", params)
                body = response.json()
                for match in body.get("data", []):
                    tournament = match.get("tournament") or {}
                    if not tournament:
                        continue
                    meta[_external_id(tour, match["id"])] = {
                        "name": tournament.get("name"),
                        # Stripped for the same reason collect_tennis_data.py strips it:
                        # "Grass" and "Grass " were separate values for 4,296 vs 386 rows, and
                        # every surface feature matches on this string.
                        "surface": (tournament.get("surface") or "").strip() or None,
                        "location": tournament.get("location"),
                    }
                cursor = (body.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            print(f"  {tour} season {season}: {len(meta)} matches mapped so far")
    return meta


async def main(confirm: bool, tour: str) -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "tennis"))).scalar_one_or_none()
        if sport is None:
            print("no tennis sport row — run scripts/seed_sports.py first")
            return
        league = (
            await db.execute(select(League).where(League.sport_id == sport.id, League.slug == tour))
        ).scalar_one_or_none()
        if league is None:
            print(f"no {tour} league row")
            return

        missing = (
            (
                await db.execute(
                    select(Fixture).where(
                        Fixture.sport_id == sport.id,
                        Fixture.league_id == league.id,
                        Fixture.tournament_surface.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not missing:
            print("nothing to backfill")
            return
        seasons = sorted({f.kickoff_utc.year for f in missing})
        print(f"{len(missing)} {tour} fixtures missing a surface, across seasons {seasons}")

        meta = await _tournament_meta_by_fixture(tour, seasons)
        print(f"provider returned tournament metadata for {len(meta)} matches")

        matched = 0
        for fixture in missing:
            info = meta.get(fixture.external_id)
            if info is None or info["surface"] is None:
                continue
            matched += 1
            if confirm:
                fixture.tournament_name = fixture.tournament_name or info["name"]
                fixture.tournament_surface = info["surface"]
                fixture.tournament_location = fixture.tournament_location or info["location"]

        print(f"{matched} of {len(missing)} can be filled ({len(missing) - matched} not returned)")
        if confirm:
            await db.commit()
            print("committed")
        else:
            print("DRY RUN — pass --confirm to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--tour", default="atp", choices=["atp", "wta"])
    asyncio.run(main(parser.parse_args().confirm, parser.parse_args().tour))
