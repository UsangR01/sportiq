"""GET /sports offers leagues as filters only where there are few enough to be useful.

WNBA prompted this: it is a LEAGUE under Sport(slug="nba"), sharing the NBA's trained model, so
it could never appear in a sport-level dropdown however many WNBA fixtures were ingested. Tennis
had the same latent gap -- ATP and WTA are one Sport row too.

The threshold is the whole design. Expanding every sport would put football's 18 leagues into a
dropdown that is meant to answer "what am I looking at" in one line, and the feed already groups
by league internally, which is the right affordance at that count. A threshold rather than a
per-sport allowlist means a third basketball or tennis competition appears on its own, and
nothing has to be edited in two places when a league is added.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.database import async_session_factory
from app.main import create_app
from app.sports.models import League, Sport
from app.sports.router import LEAGUE_PICKER_MAX


@pytest.fixture
async def api_client():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def sports_either_side_of_the_threshold():
    """Two sports: one just inside the picker limit, one just past it."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_factory() as db:
        small = Sport(slug=f"small-{suffix}", name="Small", model_type="t", active=True)
        big = Sport(slug=f"big-{suffix}", name="Big", model_type="t", active=True)
        db.add_all([small, big])
        await db.flush()
        for i in range(LEAGUE_PICKER_MAX):
            db.add(League(sport_id=small.id, slug=f"s{i}-{suffix}", name=f"Small League {i}"))
        for i in range(LEAGUE_PICKER_MAX + 1):
            db.add(League(sport_id=big.id, slug=f"b{i}-{suffix}", name=f"Big League {i}"))
        await db.commit()
        ids = (small.id, big.id, small.slug, big.slug)
    yield ids
    async with async_session_factory() as db:
        await db.execute(delete(League).where(League.sport_id.in_([ids[0], ids[1]])))
        await db.execute(delete(Sport).where(Sport.id.in_([ids[0], ids[1]])))
        await db.commit()


async def test_a_sport_at_the_limit_offers_its_leagues(
    api_client, sports_either_side_of_the_threshold
):
    _, _, small_slug, _ = sports_either_side_of_the_threshold
    rows = (await api_client.get("/sports")).json()
    small = next(r for r in rows if r["slug"] == small_slug)
    assert small["league_count"] == LEAGUE_PICKER_MAX
    assert len(small["leagues"]) == LEAGUE_PICKER_MAX


async def test_a_sport_past_the_limit_stays_collapsed(
    api_client, sports_either_side_of_the_threshold
):
    """Football is the real case: 18 leagues would make this dropdown a scrolling list of
    everything, and the feed already groups by league."""
    _, _, _, big_slug = sports_either_side_of_the_threshold
    rows = (await api_client.get("/sports")).json()
    big = next(r for r in rows if r["slug"] == big_slug)
    assert big["league_count"] == LEAGUE_PICKER_MAX + 1
    assert big["leagues"] == []


async def test_every_sport_still_reports_its_true_league_count(
    api_client, sports_either_side_of_the_threshold
):
    """league_count is the real number either way -- collapsing the PICKER must not make a
    sport look like it has fewer leagues than it does."""
    rows = (await api_client.get("/sports")).json()
    for row in rows:
        assert row["league_count"] >= len(row["leagues"])
