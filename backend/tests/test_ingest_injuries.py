"""app/workers/ingest_injuries.py — per-sport failure isolation.

The bug this guards, found the first time Celery beat actually ran on a schedule: the sport
loop had no exception handling, NBA's injury adapter is still a NotImplementedError stub, and
NBA is reached before football. So every scheduled run aborted before football — whose
API-Football injury path is entirely real and feeds key-player availability — was ever
touched. Same bug class as the tennis 401 killing shared ingest runs and the Brasileirao odds
error taking down every other league; ingest_odds.py and ingest_fixtures.py were both fixed
for it, this worker never was.

Patching the two per-sport helpers rather than the adapters keeps this a test of the LOOP's
isolation, independent of which providers happen to be stubbed at the time.
"""


import pytest
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.sports.models import Sport
from app.workers import ingest_injuries as worker


@pytest.fixture
async def nba_and_football():
    """Two real active Sport rows. Slugs must be exactly 'nba' and 'football' — the loop
    branches on them — so any pre-existing rows are reused rather than duplicated."""
    created = []
    async with async_session_factory() as db:
        for slug, name in (("nba", "NBA Basketball"), ("football", "Football")):
            existing = (
                await db.execute(Sport.__table__.select().where(Sport.slug == slug))
            ).first()
            if existing is None:
                sport = Sport(slug=slug, name=name, model_type="test", active=True)
                db.add(sport)
                created.append(slug)
        await db.commit()

    yield

    if created:
        async with async_session_factory() as db:
            await db.execute(delete(Sport).where(Sport.slug.in_(created)))
            await db.commit()


async def test_a_stubbed_nba_adapter_does_not_stop_football(monkeypatch, nba_and_football):
    """The regression itself: NBA raising NotImplementedError must not prevent football's
    injury ingest from running."""
    football_ran = False

    async def nba_stub(db, sport):
        raise NotImplementedError("BallDontLie injury fetch not yet implemented")

    async def football_spy(db, sport):
        nonlocal football_ran
        football_ran = True

    monkeypatch.setattr(worker, "_ingest_injuries_balldontlie", nba_stub)
    monkeypatch.setattr(worker, "_ingest_injuries_rotowire", nba_stub)
    monkeypatch.setattr(worker, "_ingest_injuries_api_football", football_spy)

    await worker._ingest_injuries()

    assert football_ran, "football injuries must still ingest when NBA's adapter is a stub"


async def test_an_unexpected_error_also_isolates(monkeypatch, nba_and_football):
    """Not just NotImplementedError — a live provider 401/timeout must isolate too, which is
    exactly how the tennis 401 took down unrelated sports before."""
    football_ran = False

    async def boom(db, sport):
        raise RuntimeError("provider returned 401")

    async def football_spy(db, sport):
        nonlocal football_ran
        football_ran = True

    monkeypatch.setattr(worker, "_ingest_injuries_balldontlie", boom)
    monkeypatch.setattr(worker, "_ingest_injuries_rotowire", boom)
    monkeypatch.setattr(worker, "_ingest_injuries_api_football", football_spy)

    await worker._ingest_injuries()

    assert football_ran, "an unexpected provider error must not abort the remaining sports"


async def test_the_whole_task_does_not_raise(monkeypatch, nba_and_football):
    """Celery marks the task FAILED if it propagates, which is what filled the worker log with
    a traceback every 30 minutes. A known-stubbed adapter is a gap, not an incident."""

    async def boom(db, sport):
        raise NotImplementedError("stub")

    monkeypatch.setattr(worker, "_ingest_injuries_balldontlie", boom)
    monkeypatch.setattr(worker, "_ingest_injuries_rotowire", boom)
    monkeypatch.setattr(worker, "_ingest_injuries_api_football", boom)

    await worker._ingest_injuries()  # must simply return
