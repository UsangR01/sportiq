"""The in-play at-risk alert (design spec §4.1).

The rules themselves are pinned in test_live_risk.py. What is pinned HERE is the delivery
behaviour, because every failure mode is a notification the user did not want: one per poll for
the rest of a match, an alert about a pick they never took, or one confirming a loss.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.sports.models import League, Sport
from app.users.models import User, WatchlistItem
from app.workers import notify_users
from app.workers.notify_users import _notify_at_risk

# Rows created by _seed, deleted after each test. TRUNCATION IS SESSION-SCOPED, so anything
# left behind is visible to every later test -- and these rows are not inert. A completed
# football fixture with no corner counts looks exactly like a corner-backfill candidate, which
# is how this file broke test_corner_recheck while passing perfectly well on its own.
_CREATED: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []


@pytest.fixture(autouse=True)
async def _cleanup():
    """ASYNC, not sync-with-asyncio.run().

    The shared engine binds to whichever loop first used it and pytest-asyncio gives each test
    its own, so purging on a freshly created loop raises "attached to a different loop" — the
    same failure conftest's engine-dispose fixture exists to prevent.
    """
    yield
    async with async_session_factory() as db:
        for fixture_id, league_id, user_id in _CREATED:
            await db.execute(delete(WatchlistItem).where(WatchlistItem.fixture_id == fixture_id))
            await db.execute(
                delete(FixtureLiveState).where(FixtureLiveState.fixture_id == fixture_id)
            )
            await db.execute(delete(Fixture).where(Fixture.id == fixture_id))
            await db.execute(delete(Team).where(Team.league_id == league_id))
            await db.execute(delete(League).where(League.id == league_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    _CREATED.clear()


async def _seed(
    db,
    *,
    saved_market="goals_total",
    saved_selection="under",
    saved_line=2.5,
    home_score=1,
    away_score=1,
    minute=60,
    status=FixtureStatus.LIVE,
):
    unique = uuid.uuid4().hex[:8]
    # THE SLUG MUST BE THE REAL ONE. live_risk keys its rules on "football"; a per-test slug
    # like "football-ab12" would fall through to UNKNOWN and every assertion below would pass
    # for the wrong reason. Truncation is session-scoped, so the row is shared across tests.
    sport = (await db.execute(select(Sport).where(Sport.slug == "football"))).scalar_one_or_none()
    if sport is None:
        sport = Sport(slug="football", name="Football", model_type="football_xgb_v1", active=True)
        db.add(sport)
    await db.flush()
    league = League(sport_id=sport.id, slug=f"epl-{unique}", name="EPL", country="England")
    db.add(league)
    await db.flush()
    home = Team(
        sport_id=sport.id,
        league_id=league.id,
        name="Home FC",
        short_name="Home FC",
        external_id=f"h{unique}",
    )
    away = Team(
        sport_id=sport.id,
        league_id=league.id,
        name="Away FC",
        short_name="Away FC",
        external_id=f"a{unique}",
    )
    db.add_all([home, away])
    await db.flush()
    fixture = Fixture(
        sport_id=sport.id,
        league_id=league.id,
        home_team_id=home.id,
        away_team_id=away.id,
        kickoff_utc=datetime.now(UTC) - timedelta(hours=1),
        status=status,
        season="2026",
        external_id=f"f{unique}",
    )
    user = User(
        email=f"{unique}@example.com", password_hash="x", expo_push_token="ExponentPushToken[x]"
    )
    db.add_all([fixture, user])
    await db.flush()
    db.add(
        FixtureLiveState(
            fixture_id=fixture.id,
            home_score=home_score,
            away_score=away_score,
            match_minute=minute,
            status="live",
            last_updated_utc=datetime.now(UTC),
        )
    )
    item = WatchlistItem(
        user_id=user.id,
        fixture_id=fixture.id,
        saved_market=saved_market,
        saved_selection=saved_selection,
        saved_line=saved_line,
        saved_probability=0.7,
    )
    db.add(item)
    await db.commit()
    _CREATED.append((fixture.id, league.id, user.id))
    return fixture, item


@pytest.fixture
def sent(monkeypatch):
    """Capture pushes at the boundary — the Expo call itself is third-party."""
    calls = []

    async def _capture(db, user, title, body, data):
        calls.append({"title": title, "body": body})

    monkeypatch.setattr(notify_users, "_send_push", _capture)
    return calls


@pytest.mark.asyncio
async def test_an_at_risk_saved_pick_alerts_once_and_only_once(sent):
    """THE GUARD THAT MATTERS. Live scores poll every five minutes and an at-risk pick usually
    stays at-risk, so without the stamp one bad scoreline sends the same user roughly six
    notifications about one match — the fastest way to have push switched off."""
    async with async_session_factory() as db:
        fixture, item = await _seed(db)

    await _notify_at_risk(fixture.id)
    await _notify_at_risk(fixture.id)  # the next poll, nothing changed

    assert len(sent) == 1
    assert "UNDER 2.5" in sent[0]["body"]
    # Names the reason, not just the fixture: "something happened in a match you saved" is not
    # actionable, and this is sold as an early warning.
    assert "1-1" in sent[0]["body"] and "60'" in sent[0]["body"]

    async with async_session_factory() as db:
        stored = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == item.id))
        ).scalar_one()
        assert stored.at_risk_alerted_at is not None


@pytest.mark.asyncio
async def test_a_pick_that_is_fine_raises_nothing(sent):
    async with async_session_factory() as db:
        fixture, _ = await _seed(db, home_score=0, away_score=0, minute=20)

    await _notify_at_risk(fixture.id)

    assert sent == []


@pytest.mark.asyncio
async def test_an_already_lost_pick_is_never_alerted(sent):
    """By then there is nothing to act on, and a notification that only confirms a loss is a
    worse product than silence."""
    async with async_session_factory() as db:
        fixture, _ = await _seed(db, home_score=2, away_score=1)  # under 2.5 is gone

    await _notify_at_risk(fixture.id)

    assert sent == []


@pytest.mark.asyncio
async def test_a_corners_pick_never_alerts_because_it_cannot_be_judged(sent):
    """Corner counts are written once, at settlement — there is no in-play value to read, so an
    alert here would be reacting to a number we do not have."""
    async with async_session_factory() as db:
        fixture, _ = await _seed(
            db, saved_market="corners_total", saved_selection="over", saved_line=9.5
        )

    await _notify_at_risk(fixture.id)

    assert sent == []


@pytest.mark.asyncio
async def test_nothing_is_sent_in_the_closing_minutes(sent):
    """An alert the user cannot act on is noise wearing a premium label. The CARD may still
    show AT RISK — this bound is about waking a phone."""
    async with async_session_factory() as db:
        fixture, _ = await _seed(
            db,
            saved_market="h2h",
            saved_selection="home",
            saved_line=None,
            home_score=0,
            away_score=1,
            minute=88,
        )

    await _notify_at_risk(fixture.id)

    assert sent == []


@pytest.mark.asyncio
async def test_a_fixture_that_is_no_longer_live_alerts_nobody(sent):
    """The task is queued from the live poll and runs moments later; a match can finish in
    between, and an alert arriving after full time is worse than none."""
    async with async_session_factory() as db:
        fixture, _ = await _seed(db, status=FixtureStatus.COMPLETED)

    await _notify_at_risk(fixture.id)

    assert sent == []


@pytest.mark.asyncio
async def test_a_saved_row_with_no_recorded_pick_is_skipped(sent):
    """Rows saved before receipts existed carry no pick. Judging one against whatever the feed
    recommends NOW would alert on a pick the user never took."""
    async with async_session_factory() as db:
        fixture, _ = await _seed(db, saved_market=None, saved_selection=None, saved_line=None)

    await _notify_at_risk(fixture.id)

    assert sent == []
