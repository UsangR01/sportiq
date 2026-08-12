"""Fixtures that vanish from the provider while their kickoff is still in the FUTURE.

ingest_live_scores._mark_abandoned_fixtures already retires vanished fixtures, but it infers
absence from elapsed time — 12 hours past kickoff, 30 for a Time-TBC placeholder. That cannot
see a fixture which disappears before it was ever due to start, and such a fixture stays a
live-looking pick until the clock catches up.

Measured 2026-08-12: 33 of 53 upcoming ATP fixtures returned HTTP 404 from BallDontLie, all
carrying a prediction, all dated the next day — a provisional Cincinnati draw the provider
withdrew and replaced. The time-based sweep would not have touched them for another ~30 hours
after a kickoff that had not happened.

The reconciliation tested here uses the provider's own current list as positive evidence
instead. The tests below are weighted toward the OPPOSITE error, because retiring a real
upcoming fixture removes a pick a user could have acted on, which is worse than the bug.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.adapters.base import FixturePayload
from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers.ingest_fixtures import _reconcile_vanished_fixtures


def _payload(external_id: str, kickoff: datetime) -> FixturePayload:
    return FixturePayload(
        external_id=external_id,
        league_external_id="l",
        home_team_external_id="h",
        away_team_external_id="a",
        kickoff_utc=kickoff,
        season="2026",
        status="scheduled",
    )


@pytest.fixture
async def world():
    """One league, four fixtures on the same future date, one on a date never reported."""
    suffix = uuid.uuid4().hex[:8]
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    other_day = tomorrow + timedelta(days=3)
    async with async_session_factory() as db:
        sport = Sport(slug=f"van-{suffix}", name="V", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(sport_id=sport.id, slug=f"vanl-{suffix}", name="L")
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{suffix}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{suffix}")
        db.add_all([home, away])
        await db.flush()

        def make(tag, kickoff):
            return Fixture(
                sport_id=sport.id,
                league_id=league.id,
                home_team_id=home.id,
                away_team_id=away.id,
                external_id=f"{tag}-{suffix}",
                kickoff_utc=kickoff,
                kickoff_is_estimated=True,
                status=FixtureStatus.SCHEDULED,
                season="2026",
            )

        alive = make("alive", tomorrow)  # still in the provider's list
        gone = make("gone", tomorrow)  # withdrawn: same date, absent from the list
        underway = make("underway", tomorrow)  # absent, but observed being played
        uncovered = make("uncovered", other_day)  # absent, on a date never reported
        db.add_all([alive, gone, underway, uncovered])
        await db.flush()
        db.add(
            FixtureLiveState(
                fixture_id=underway.id,
                home_score=1,
                away_score=0,
                status=FixtureStatus.LIVE,
                last_updated_utc=datetime.now(UTC),
            )
        )
        await db.commit()
        ids = {
            "sport_id": sport.id,
            "league_id": league.id,
            "suffix": suffix,
            "tomorrow": tomorrow,
            "other_day": other_day,
            "underway": underway.id,
        }
    yield ids
    async with async_session_factory() as db:
        await db.execute(
            delete(FixtureLiveState).where(FixtureLiveState.fixture_id == ids["underway"])
        )
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids["sport_id"]))
        await db.execute(delete(Team).where(Team.sport_id == ids["sport_id"]))
        await db.execute(delete(League).where(League.sport_id == ids["sport_id"]))
        await db.execute(delete(Sport).where(Sport.id == ids["sport_id"]))
        await db.commit()


async def _run(world, payloads):
    async with async_session_factory() as db:
        sport = (await db.execute(select(Sport).where(Sport.id == world["sport_id"]))).scalar_one()
        league = (
            await db.execute(select(League).where(League.id == world["league_id"]))
        ).scalar_one()
        return await _reconcile_vanished_fixtures(db, sport, league, payloads)


async def _status(world, tag) -> FixtureStatus:
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(Fixture.status).where(
                    Fixture.external_id == f"{tag}-{world['suffix']}",
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_future_fixture_the_provider_no_longer_lists_is_retired(world):
    """THE reported case. Its kickoff is tomorrow, so the time-based sweep cannot help for
    another day and a half — by which point it has already been most of a day's feed."""
    retired = await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    assert retired == 1
    assert await _status(world, "gone") is FixtureStatus.POSTPONED
    assert await _status(world, "alive") is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_an_empty_payload_retires_nothing(world):
    """THE guard that matters most. A rate-limited or failed fetch returns an empty list that
    reads exactly like "this league has no fixtures" — CLAUDE.md already records that false
    negative nearly writing off two real leagues. Acting on it would retire every upcoming
    fixture in the league at once."""
    assert await _run(world, []) == 0
    for tag in ("alive", "gone", "underway", "uncovered"):
        assert await _status(world, tag) is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_a_date_the_provider_did_not_report_on_is_left_alone(world):
    """Absence of evidence is not evidence of absence: the provider returning nothing for a
    date says nothing about fixtures on it. Those stay with the time-based sweep."""
    await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    assert await _status(world, "uncovered") is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_a_fixture_observed_underway_is_never_retired(world):
    """A FixtureLiveState row is proof the match is real and happening, which outranks the
    provider dropping it from a list."""
    await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    assert await _status(world, "underway") is FixtureStatus.SCHEDULED


@pytest.mark.asyncio
async def test_it_is_idempotent(world):
    """Runs on every ingest. The second pass finds nothing because POSTPONED is outside its
    SCHEDULED filter."""
    first = await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    second = await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    assert (first, second) == (1, 0)


@pytest.mark.asyncio
async def test_a_reappearing_fixture_is_not_permanently_lost(world):
    """Retirement must be reversible — a provisional draw can be republished, and ingest's
    update branch sets status straight from the payload. Asserted here as the property the
    reconciliation depends on: once POSTPONED, the fixture is still matched by external_id."""
    await _run(world, [_payload(f"alive-{world['suffix']}", world["tomorrow"])])
    assert await _status(world, "gone") is FixtureStatus.POSTPONED
    async with async_session_factory() as db:
        fixture = (
            await db.execute(
                select(Fixture).where(Fixture.external_id == f"gone-{world['suffix']}")
            )
        ).scalar_one()
        fixture.status = FixtureStatus.SCHEDULED  # what the update branch does on reappearance
        await db.commit()
    assert await _status(world, "gone") is FixtureStatus.SCHEDULED
