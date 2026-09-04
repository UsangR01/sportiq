"""A published card is a promise. Once a match kicks off, nothing may change it.

Reported 2026-09-04: "Why are cards number changing after games... How will a user feel to see a
prediction and stake based on what he sees... after the game, the card that initial pushed him
to make a stake disappears. This is serious integrity."

MEASURED on cards pulled from production on 2026-08-30 at the app's own defaults -- provably on
screen -- re-checked five days later: of 396, 320 unchanged, 28 showed a DIFFERENT BET, 48 were
GONE. 19% altered or deleted.

The tests below are written against the two ways that happened, because a freeze that only
stops one of them is not a freeze:
  - a GUARD changing (barring corners on 2026-08-30 rewrote 17 published cards by itself), and
  - the PREDICTION being regenerated (578 of 740 settled fixtures rendered a prediction created
    after their own kickoff, because a LIVE fixture counted as "upcoming").
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureStatus, Team
from app.odds.models import Odds, OddsMarket
from app.predictions.models import ConfidenceTier, FrozenPick, Prediction, PredictionKind
from app.sports.models import League, Sport


@pytest.fixture
async def kicked_off_fixture():
    """One fixture that kicked off an hour ago, with a real prediction and real prices.

    Home 0.655 with a draw at 0.161 makes 1X = 0.816, so several markets are genuinely in
    contention -- the test needs a fixture where the ranking could plausibly move, or freezing
    it proves nothing.
    """
    async with async_session_factory() as db:
        slug = f"freeze-{uuid.uuid4().hex[:8]}"
        sport = Sport(slug=slug, name="Freeze Test", model_type="test", active=True)
        db.add(sport)
        await db.flush()
        league = League(
            sport_id=sport.id, slug=f"l-{slug}", name="L", country=None, tier=1, active=True
        )
        db.add(league)
        await db.flush()
        home = Team(sport_id=sport.id, league_id=league.id, name="Home", external_id=f"h{slug}")
        away = Team(sport_id=sport.id, league_id=league.id, name="Away", external_id=f"a{slug}")
        db.add_all([home, away])
        await db.flush()
        now = datetime.now(UTC)
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            external_id=f"fx-{slug}",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=now - timedelta(hours=1),
            status=FixtureStatus.LIVE,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            Prediction(
                fixture_id=fixture.id,
                model_version="freeze_test_v1",
                home_prob=0.655,
                draw_prob=0.161,
                away_prob=0.184,
                confidence_tier=ConfidenceTier.MEDIUM,
                created_at=now - timedelta(hours=6),
                kind=PredictionKind.PRE_MATCH,
                feature_completeness=0.8,
            )
        )
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="test-book",
                market=OddsMarket.DOUBLE_CHANCE,
                home_odds=1.30,
                away_odds=4.50,
                updated_at=now,
            )
        )
        db.add(
            Odds(
                fixture_id=fixture.id,
                bookmaker="test-book",
                market=OddsMarket.H2H,
                home_odds=1.55,
                draw_odds=4.00,
                away_odds=6.00,
                updated_at=now,
            )
        )
        await db.commit()
        ids = (sport.id, league.id, fixture.id)

    yield ids

    async with async_session_factory() as db:
        await db.execute(delete(FrozenPick).where(FrozenPick.fixture_id == ids[2]))
        await db.execute(delete(Odds).where(Odds.fixture_id == ids[2]))
        await db.execute(delete(Prediction).where(Prediction.fixture_id == ids[2]))
        await db.execute(delete(Fixture).where(Fixture.sport_id == ids[0]))
        await db.execute(delete(Team).where(Team.sport_id == ids[0]))
        await db.execute(delete(League).where(League.sport_id == ids[0]))
        await db.execute(delete(Sport).where(Sport.id == ids[0]))
        await db.commit()


async def _card(fixture_id):
    from app.fixtures.router import _bulk_best_picks

    async with async_session_factory() as db:
        best, _all = await _bulk_best_picks(db, [fixture_id])
    return best.get(fixture_id)


async def _freeze(fixture_id):
    from app.predictions.pick_freeze import freeze_started_fixtures

    async with async_session_factory() as db:
        return await freeze_started_fixtures(db)


async def test_a_started_fixture_is_frozen_on_the_beat(kicked_off_fixture):
    _sport, _league, fixture_id = kicked_off_fixture
    shown = await _card(fixture_id)
    assert shown is not None, "the fixture must have a real card, or this proves nothing"

    await _freeze(fixture_id)

    async with async_session_factory() as db:
        row = (
            await db.execute(select(FrozenPick).where(FrozenPick.fixture_id == fixture_id))
        ).scalar_one()
    assert (row.market, row.selection) == (shown.market, shown.selection)
    assert row.frozen_reason == "kickoff"


async def test_barring_the_frozen_market_afterwards_cannot_change_the_card(
    kicked_off_fixture, monkeypatch
):
    """THE REGRESSION, and it is not hypothetical: barring corners on 2026-08-30 changed 17
    already-published cards, 11 of them from a corners pick to X2."""
    _sport, _league, fixture_id = kicked_off_fixture
    before = await _card(fixture_id)
    await _freeze(fixture_id)

    # The exact act that rewrote history: a market this card had already shown is barred.
    import app.fixtures.router as router

    monkeypatch.setattr(router, "NO_DEMONSTRATED_SIGNAL_MARKETS", frozenset({before.market}))

    after = await _card(fixture_id)
    assert after is not None, "the published card must survive its market being barred"
    assert (after.market, after.selection, after.line) == (
        before.market,
        before.selection,
        before.line,
    )


async def test_a_new_prediction_cannot_change_the_card(kicked_off_fixture):
    """The other half. A prediction created after kickoff must not reach a published card --
    578 of 740 settled fixtures were rendering exactly that."""
    _sport, _league, fixture_id = kicked_off_fixture
    before = await _card(fixture_id)
    await _freeze(fixture_id)

    async with async_session_factory() as db:
        db.add(
            Prediction(
                fixture_id=fixture_id,
                model_version="freeze_test_v2",
                home_prob=0.10,
                draw_prob=0.15,
                away_prob=0.75,  # the opposite call entirely
                confidence_tier=ConfidenceTier.HIGH,
                created_at=datetime.now(UTC),
                kind=PredictionKind.PRE_MATCH,
                feature_completeness=0.9,
            )
        )
        await db.commit()

    after = await _card(fixture_id)
    assert (after.market, after.selection) == (before.market, before.selection)
    assert after.probability == pytest.approx(before.probability)


async def test_a_card_that_showed_no_pick_cannot_gain_one(kicked_off_fixture, monkeypatch):
    """ADDING a pick nobody saw is the same defect as deleting one they did -- both were caught
    in the same week, when relaxing a guard for settled fixtures surfaced a 1X pick that had
    been correctly hidden all along. A NULL market is a RECORD, not a gap."""
    _sport, _league, fixture_id = kicked_off_fixture
    import app.fixtures.router as router

    # Nothing qualifies at freeze time.
    monkeypatch.setattr(
        router,
        "NO_DEMONSTRATED_SIGNAL_MARKETS",
        frozenset({"h2h", "double_chance", "goals_total", "corners_total"}),
    )
    assert await _card(fixture_id) is None
    await _freeze(fixture_id)

    # The bar is lifted afterwards. The card must STAY empty.
    monkeypatch.setattr(router, "NO_DEMONSTRATED_SIGNAL_MARKETS", frozenset())

    assert await _card(fixture_id) is None
    async with async_session_factory() as db:
        row = (
            await db.execute(select(FrozenPick).where(FrozenPick.fixture_id == fixture_id))
        ).scalar_one()
    assert row.market is None


async def test_freezing_is_idempotent(kicked_off_fixture):
    """It runs every five minutes forever. A second pass must write nothing rather than
    violating the one-row-per-fixture constraint or re-recording a later answer."""
    _sport, _league, fixture_id = kicked_off_fixture
    await _freeze(fixture_id)
    async with async_session_factory() as db:
        first = (
            await db.execute(select(FrozenPick).where(FrozenPick.fixture_id == fixture_id))
        ).scalar_one()
        original = (first.market, first.selection, first.frozen_at)

    await _freeze(fixture_id)

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(FrozenPick).where(FrozenPick.fixture_id == fixture_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert (rows[0].market, rows[0].selection, rows[0].frozen_at) == original


async def test_a_fixture_that_has_not_started_is_not_frozen(kicked_off_fixture):
    """The freeze must not reach forward. A card is only a promise once it can no longer be
    acted on -- before kickoff the feed is deliberately live, and a near-kickoff freeze was
    measured on 2026-08-21 and rejected (0% flips at T-2, T-6 and T-12)."""
    _sport, _league, fixture_id = kicked_off_fixture
    async with async_session_factory() as db:
        fixture = (await db.execute(select(Fixture).where(Fixture.id == fixture_id))).scalar_one()
        fixture.kickoff_utc = datetime.now(UTC) + timedelta(hours=3)
        fixture.status = FixtureStatus.SCHEDULED
        await db.commit()

    await _freeze(fixture_id)

    async with async_session_factory() as db:
        assert (
            await db.execute(select(FrozenPick).where(FrozenPick.fixture_id == fixture_id))
        ).scalar_one_or_none() is None


def test_ingest_no_longer_treats_a_started_fixture_as_upcoming():
    """The source-level half of "no alterations after the game starts". The status filter alone
    lets LIVE through, so a match in play had its features recomputed and its prediction
    replaced while it was being played.

    Pinned by source because the failure is silent: nothing errors, the numbers simply move.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "workers" / "ingest_fixtures.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assign = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "upcoming" for t in node.targets)
    )
    query = ast.unparse(assign)
    assert "kickoff_utc" in query, "a kicked-off fixture must be excluded by TIME, not by status"
    assert "COMPLETED" in query and "POSTPONED" in query
