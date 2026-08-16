"""One-off: give the two WNBA expansion teams their full names.

BallDontLie ships Portland Fire and Toronto Tempo with an EMPTY city and a mascot-only
full_name, so `full_name or name` -- correctly -- produced "Fire" and "Tempo", and that is what
every card showed. Read live 2026-08-16:

    id 31  POR  name "Fire"    full_name "Fire"            city ""
    id 30  TOR  name "Tempo"   full_name "Tempo"           city ""
    id  4  ATL  name "Dream"   full_name "Atlanta Dream"   city "Atlanta"

The adapter now applies _WNBA_TEAM_NAME_OVERRIDES so newly-created rows are right. This fixes
the rows that already exist, because get_or_create_team only sets the name at CREATION.

Matched on external_id, which is `wnba:`-prefixed -- not on the abbreviation, since "POR" is
Portland Trail Blazers in the NBA namespace and both leagues share one Sport row.

Dry run by default. Pass --confirm to write.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.adapters.balldontlie import _WNBA_TEAM_NAME_OVERRIDES, league_external_id  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.fixtures.models import Team  # noqa: E402

# Imported for its side effect: Team.sport_id carries a foreign key to `sports`, and SQLAlchemy
# cannot resolve it unless that table is registered on the shared metadata first.
from app.sports.models import League, Sport  # noqa: E402,F401


async def main(confirm: bool) -> None:
    async with async_session_factory() as db:
        changed = 0
        for raw_id, full_name in _WNBA_TEAM_NAME_OVERRIDES.items():
            external_id = league_external_id("wnba", raw_id)
            team = (
                await db.execute(select(Team).where(Team.external_id == external_id))
            ).scalar_one_or_none()
            if team is None:
                print(f"  no team row for {external_id}")
                continue
            if team.name == full_name and team.short_name == full_name:
                print(f"  already correct: {external_id} -> {full_name}")
                continue
            print(f"  {external_id}: name {team.name!r} -> {full_name!r}")
            if confirm:
                team.name = full_name
                # short_name carries the abbreviation for basketball (used by cross-provider
                # odds matching), so it is deliberately NOT touched here.
                changed += 1
        if confirm and changed:
            await db.commit()
        print("WROTE" if confirm else "dry run — pass --confirm to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    asyncio.run(main(parser.parse_args().confirm))
