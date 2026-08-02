"""One-off repair for tennis fixtures corrupted by the pre-fix set-counting bug.

The bug (see app/adapters/balldontlie_tennis.py:_is_completed_set): _sets_won counted ANY set
where a player happened to be ahead, including a set abandoned mid-play when a player retired.
A real retirement like 6-4, 2-3 was therefore stored as 1-1 — an impossible tennis scoreline,
since tennis has no draws — which additionally INVERTED the win/loss verdict shown in the feed
(a correct prediction rendered as a failure) and settled an Outcome as MatchResult.DRAW.

Ordinary re-ingestion only covers the ±7-day window, so this repairs affected fixtures of any
age. It re-fetches each one from the real API and recomputes the score, result_type and
Outcome through the FIXED code path — it never guesses or patches values by hand.

Scope, deliberately: only fixtures whose stored score is an impossible draw (home == away).
A retirement where the eventual winner also led the abandoned set (e.g. 6-4, 3-2 ret.) was
stored as 2-0 instead of the correct 1-0 — a wrong score, but with the right winner and
therefore the right verdict. Those are left to ordinary re-ingestion rather than re-fetching
thousands of mostly-historical fixtures, since the visible verdict is already correct.

Elo is knowingly NOT recomputed: those 32 fixtures applied a draw-shaped Elo update that never
should have happened. Correcting it properly would mean replaying every team's Elo history in
order. It has zero effect on tennis predictions in practice — tennis's feature set uses
rank_diff, not Elo (see app/models_ml/tennis_features.py:FEATURE_NAMES) — so this is a real
but inert inaccuracy, recorded here rather than silently left undocumented.

Usage (from backend/):
    PYTHONPATH=. python scripts/repair_tennis_retirement_scores.py
"""

import asyncio
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.adapters.balldontlie_tennis import (
    _home_away_players,
    _match_result_type,
    _sets_won,
    _strip_tour_prefix,
    _tour_prefix,
)
from app.core.config import get_settings
from app.core.database import async_session_factory, engine
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus
from app.history.models import MatchResult, Outcome
from app.sports.models import League, Sport

REQUEST_DELAY_SECONDS = 1.1  # stay under ALL-STAR's documented 60 req/min
MAX_RETRIES = 5


async def _get_match(client: httpx.AsyncClient, raw_id: str) -> httpx.Response:
    """Retries on 429, honouring Retry-After — needed in practice, not theoretically: running
    this straight after a full re-ingest (which had already consumed the minute's budget)
    produced a run of 429s that silently skipped 24 of 33 fixtures on the first attempt."""
    response = None
    for attempt in range(MAX_RETRIES):
        response = await client.get(f"/matches/{raw_id}")
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2**attempt, 30)
            print(f"    429, backing off {delay:.0f}s")
            await asyncio.sleep(delay)
            continue
        return response
    return response


async def main() -> None:
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.slug == "tennis"))).scalar_one()
        leagues = {
            row.id: row.slug
            for row in (
                await db.execute(select(League).where(League.sport_id == sport.id))
            ).scalars()
        }
        rows = (
            await db.execute(
                select(Fixture, FixtureLiveState)
                .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
                .where(
                    Fixture.sport_id == sport.id,
                    Fixture.status == FixtureStatus.COMPLETED,
                    FixtureLiveState.home_score == FixtureLiveState.away_score,
                )
            )
        ).all()
        print(f"{len(rows)} tennis fixtures with an impossible drawn scoreline to repair")

        api_key = get_settings().balldontlie_api_key
        repaired = 0
        skipped = 0
        for fixture, live_state in rows:
            tour = leagues.get(fixture.league_id)
            if tour is None or not fixture.external_id:
                skipped += 1
                continue
            raw_id = _strip_tour_prefix(fixture.external_id)
            async with httpx.AsyncClient(
                base_url=f"https://api.balldontlie.io{_tour_prefix(tour)}",
                headers={"Authorization": api_key},
                timeout=15.0,
            ) as client:
                response = await _get_match(client, raw_id)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            if response.status_code != 200:
                print(f"  skip {fixture.external_id}: HTTP {response.status_code}")
                skipped += 1
                continue
            match = response.json().get("data") or {}
            if not match.get("set_scores"):
                skipped += 1
                continue

            p1_sets, p2_sets = _sets_won(match)
            home_player, _away_player = _home_away_players(match)
            home_is_player1 = home_player is match["player1"]
            home_score = p1_sets if home_is_player1 else p2_sets
            away_score = p2_sets if home_is_player1 else p1_sets
            if home_score is None or away_score is None or home_score == away_score:
                # Still not resolvable to a real winner — leave it rather than fabricate one.
                skipped += 1
                continue

            live_state.home_score = home_score
            live_state.away_score = away_score
            live_state.result_type = _match_result_type(match)
            live_state.last_updated_utc = datetime.now(UTC)

            outcome = (
                await db.execute(select(Outcome).where(Outcome.fixture_id == fixture.id))
            ).scalar_one_or_none()
            if outcome is not None:
                outcome.home_score = home_score
                outcome.away_score = away_score
                outcome.result = (
                    MatchResult.HOME_WIN if home_score > away_score else MatchResult.AWAY_WIN
                )
                outcome.settled_at = datetime.now(UTC)
            repaired += 1
            print(
                f"  repaired {fixture.external_id}: {home_score}-{away_score} "
                f"({live_state.result_type})"
            )

        await db.commit()
        print(f"repaired={repaired} skipped={skipped}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
