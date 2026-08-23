"""Corner counts read at settlement can be PROVISIONAL, and nothing could ever correct them.

Reported as: "Austria league had 10 corners and over 9.5 predicted. It shows success correctly
in the local but failed in the mobile app." Production held 5+4 = 9 for Austria Lustenau v
Wolfsberger AC; API-Football and TheStatsAPI both say 6+4 = 10. The pick won and the card
showed a red cross.

Measured across 45 recently-settled fixtures: 8 stored counts disagreed with the provider
(18%), EVERY ONE AN UNDERCOUNT -- the signature of reading while the statistics were still
being filled in -- and 4 of them flipped a shown verdict.

The mechanism: _maybe_fetch_corner_stats reads ONCE at settlement behind an idempotency guard,
and this sweep previously only ever filled a NULL. A count read too early was frozen for good.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.sports.models import League, Sport
from app.workers import ingest_live_scores as ils


@pytest.fixture
async def settled_football_fixture():
    """One completed football fixture, kicked off two hours ago, with stored corners."""
    async with async_session_factory() as db:
        slug = f"corner-{uuid.uuid4().hex[:8]}"
        sport = (
            await db.execute(select(Sport).where(Sport.slug == "football"))
        ).scalar_one_or_none()
        created_sport = None
        if sport is None:
            sport = Sport(slug="football", name="Football", model_type="test", active=True)
            db.add(sport)
            await db.flush()
            created_sport = sport.id
        league = League(
            sport_id=sport.id, slug=f"lg-{slug}", name="L", country="XX", tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h-{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a-{slug}")
        db.add_all([home, away])
        await db.flush()
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id=f"fx-{slug}",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=datetime.now(UTC) - timedelta(hours=2),
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            FixtureLiveState(
                fixture_id=fixture.id,
                home_score=2,
                away_score=1,
                home_corners=5,
                away_corners=4,
                status="completed",
                last_updated_utc=datetime.now(UTC),
            )
        )
        await db.commit()
        await db.refresh(fixture)
        ids = (fixture.id, league.id, home.external_id, away.external_id, created_sport)

    yield ids

    async with async_session_factory() as db:
        await db.execute(delete(FixtureLiveState).where(FixtureLiveState.fixture_id == ids[0]))
        await db.execute(delete(Fixture).where(Fixture.league_id == ids[1]))
        await db.execute(delete(Team).where(Team.league_id == ids[1]))
        await db.execute(delete(League).where(League.id == ids[1]))
        if ids[4]:
            await db.execute(delete(Sport).where(Sport.id == ids[4]))
        await db.commit()


async def corners_for(fixture_id):
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id)
            )
        ).scalar_one()
        return row.home_corners, row.away_corners


async def test_a_provisional_count_is_corrected(settled_football_fixture, monkeypatch):
    """THE REGRESSION. Stored 5+4 read too early; the provider's final figure is 6+4. Before
    this change the sweep skipped any fixture that already had a value, so the wrong count --
    and the red cross it produced on a winning pick -- stayed forever."""
    fixture_id, _league_id, home_ext, away_ext, _ = settled_football_fixture

    async def final_counts(fixture_external_id):
        return {home_ext: 6, away_ext: 4}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", final_counts)

    await ils._backfill_missing_corner_counts()

    assert await corners_for(fixture_id) == (6, 4)


async def test_a_provider_with_nothing_to_say_never_erases_a_stored_count(
    settled_football_fixture, monkeypatch
):
    """The dangerous direction. Veikkausliiga's counts come from TheStatsAPI because
    API-Football has ZERO corner coverage for it, so every re-read there returns nothing --
    and clearing on an empty response would delete the only counts that league will ever
    have."""
    fixture_id, _league_id, _home, _away, _ = settled_football_fixture

    async def nothing(fixture_external_id):
        return {}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", nothing)

    await ils._backfill_missing_corner_counts()

    assert await corners_for(fixture_id) == (5, 4)


async def test_an_unchanged_count_is_left_alone(settled_football_fixture, monkeypatch):
    fixture_id, _league_id, home_ext, away_ext, _ = settled_football_fixture

    async def same(fixture_external_id):
        return {home_ext: 5, away_ext: 4}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", same)

    await ils._backfill_missing_corner_counts()

    assert await corners_for(fixture_id) == (5, 4)


async def test_an_old_fixture_is_not_re_read(settled_football_fixture, monkeypatch):
    """Past the window the provider's figure is final, and asking again spends a call to be
    told what we already hold."""
    fixture_id, _league_id, home_ext, away_ext, _ = settled_football_fixture
    async with async_session_factory() as db:
        fixture = (await db.execute(select(Fixture).where(Fixture.id == fixture_id))).scalar_one()
        fixture.kickoff_utc = datetime.now(UTC) - timedelta(hours=ils.CORNER_RECHECK_HOURS + 6)
        await db.commit()

    called = {"n": 0}

    async def counting(fixture_external_id):
        called["n"] += 1
        return {home_ext: 9, away_ext: 9}

    monkeypatch.setattr("app.adapters.api_football.fetch_corner_stats", counting)

    await ils._backfill_missing_corner_counts()

    assert called["n"] == 0
    assert await corners_for(fixture_id) == (5, 4)


# === The name tiebreak, added 2026-08-23 ==========================================================


def test_two_matches_with_the_same_scoreline_are_separated_by_team_name():
    """THE REPORTED BUG. Cruzeiro v Flamengo finished 2-1 and stayed ungraded for a day. The
    fallback matched on DATE + SCORE alone, Brasileirao had a second 2-1 that day (Fluminense v
    Remo), and an ambiguous set was refused outright — so a pick that had WON (3-3, six corners,
    under 10.5) showed no verdict while the data was available the whole time.

    Names are a TIEBREAK ONLY: the score must match first, so a cross-provider spelling
    difference can only fail to disambiguate, never mis-select.
    """
    from app.adapters.thestatsapi import _narrow_by_name

    matches = [
        {"id": "a", "home_team": {"name": "Cruzeiro"}, "away_team": {"name": "Flamengo"}},
        {"id": "b", "home_team": {"name": "Fluminense"}, "away_team": {"name": "Remo"}},
    ]

    assert [m["id"] for m in _narrow_by_name(matches, "Cruzeiro", "Flamengo")] == ["a"]
    assert [m["id"] for m in _narrow_by_name(matches, "Fluminense", "Remo")] == ["b"]


def test_a_spelling_difference_gives_no_help_rather_than_mis_selecting():
    """THE LIMITATION, asserted rather than assumed — my first version of this test got it wrong.

    The tiebreak needs a SHARED WORD on both sides. "Wolves" and "Wolverhampton Wanderers" have
    none, so it narrows to nothing, the caller keeps the ambiguous set, and the fixture stays
    ungraded. That is the safe direction and NOT a fix for every case: it rescues fixtures whose
    names roughly agree — which covers the reported Brasileirão one — and leaves genuinely
    divergent spellings exactly where they were.
    """
    from app.adapters.thestatsapi import _narrow_by_name

    matches = [
        {
            "id": "a",
            "home_team": {"name": "Wolverhampton Wanderers"},
            "away_team": {"name": "Brentford"},
        },
        {"id": "b", "home_team": {"name": "Fulham"}, "away_team": {"name": "Everton"}},
    ]

    # A shared word is required on BOTH sides, so agreeing on one is not enough.
    assert _narrow_by_name(matches, "Wolves", "Brentford") == []
    # The fuller spelling does resolve it.
    assert _narrow_by_name(matches, "Wolverhampton Wanderers", "Brentford") == [matches[0]]
    # Nothing in common at all: no help, never a guess.
    assert _narrow_by_name(matches, "Napoli", "Torino") == []


def test_missing_names_give_no_help_at_all():
    """The caller may have no names; that must degrade to the old refuse-the-ambiguity path."""
    from app.adapters.thestatsapi import _narrow_by_name

    matches = [{"id": "a", "home_team": {"name": "X"}, "away_team": {"name": "Y"}}]
    assert _narrow_by_name(matches, None, "Y") == []
    assert _narrow_by_name(matches, "X", None) == []
