"""The corners historical blend (app/models_ml/corners_reference.py).

Exists because the corners model, alone, shipped picks that were WORSE than always backing the
more common side: measured on 1,277 held-out fixtures, gated picks hit 52.1% at the 10.5 line
against a 59.4% always-best baseline. Blending toward a rolling attack/defence reference at
lambda=0.75 took those same picks to 61.7%.

These tests pin the parts that would silently undo that: the blend weight, the attack/defence
pairing (as opposed to simply adding both teams' corners won), and the leakage guard that keeps
a fixture out of its own reference.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.database import async_session_factory
from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus, Team
from app.models_ml.corners_reference import (
    CORNERS_HISTORY_BLEND,
    MIN_REFERENCE_MATCHES,
    blend_probability,
    bulk_corners_reference,
)
from app.sports.models import League, Sport


@pytest.fixture(autouse=True)
async def _cleanup_seeded_rows():
    """Remove everything these tests create.

    The suite runs against the DEV database, and a leftover Sport row is not inert: fake
    sports seeded by an earlier test file reached the app's own sport dropdown once already.
    Mirrors tests/test_watchlist.py's own teardown.
    """
    yield
    from sqlalchemy import delete, select

    async with async_session_factory() as db:
        sport_ids = (
            (await db.execute(select(Sport.id).where(Sport.slug.like("cr-%")))).scalars().all()
        )
        if not sport_ids:
            return
        fixture_ids = (
            (await db.execute(select(Fixture.id).where(Fixture.sport_id.in_(sport_ids))))
            .scalars()
            .all()
        )
        if fixture_ids:
            await db.execute(
                delete(FixtureLiveState).where(FixtureLiveState.fixture_id.in_(fixture_ids))
            )
        await db.execute(delete(Fixture).where(Fixture.sport_id.in_(sport_ids)))
        await db.execute(delete(Team).where(Team.sport_id.in_(sport_ids)))
        await db.execute(delete(League).where(League.sport_id.in_(sport_ids)))
        await db.execute(delete(Sport).where(Sport.id.in_(sport_ids)))
        await db.commit()


def test_blend_weight_is_the_measured_one():
    """0.75 is not a taste. It won the gated hit rate at BOTH lines (9.5: 56.9%, 10.5: 61.7%)
    and tied for the best Brier, beating pure-model (lambda 0) and pure-history (lambda 1)."""
    assert CORNERS_HISTORY_BLEND == 0.75
    # model 0.80, reference 0.20 -> 0.25*0.80 + 0.75*0.20 = 0.35
    assert blend_probability(0.80, 0.20) == pytest.approx(0.35)
    # The reference dominates, which is the point: the model earned only a quarter weight.
    assert blend_probability(0.90, 0.10) < 0.5


def test_missing_history_keeps_the_model_number_rather_than_dropping_the_pick():
    """A newly promoted side, or a league we only just began ingesting, has no corner history.
    Degrading to the model's own number matches how every other feature here handles absence —
    the alternative is silently hiding fixtures for a data gap the user cannot see."""
    assert blend_probability(0.7, None) == 0.7
    assert blend_probability(None, 0.4) == 0.4
    assert blend_probability(None, None) is None


async def _seed(db, corner_history: list[tuple[int, int]], kickoff_offsets: list[int]):
    """A sport/league/two teams, plus completed fixtures carrying real corner counts.

    corner_history entries are (home_corners, away_corners) for HOME_TEAM vs AWAY_TEAM, and
    kickoff_offsets are days relative to now (negative = in the past).
    """
    suffix = uuid.uuid4().hex[:8]
    sport = Sport(slug=f"cr-{suffix}", name="Corners Test", model_type="none")
    db.add(sport)
    await db.flush()
    league = League(sport_id=sport.id, slug=f"crl-{suffix}", name="L")
    db.add(league)
    await db.flush()
    home = Team(sport_id=sport.id, league_id=league.id, name="H", external_id=f"h{suffix}")
    away = Team(sport_id=sport.id, league_id=league.id, name="A", external_id=f"a{suffix}")
    db.add_all([home, away])
    await db.flush()

    for i, ((hc, ac), offset) in enumerate(zip(corner_history, kickoff_offsets, strict=True)):
        fixture = Fixture(
            sport_id=sport.id,
            league_id=league.id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_id=f"crf-{suffix}-{i}",
            kickoff_utc=datetime.now(UTC) + timedelta(days=offset),
            status=FixtureStatus.COMPLETED,
            season="2026",
        )
        db.add(fixture)
        await db.flush()
        db.add(
            FixtureLiveState(
                fixture_id=fixture.id,
                home_score=0,
                away_score=0,
                status="completed",
                home_corners=hc,
                away_corners=ac,
                last_updated_utc=datetime.now(UTC),
            )
        )
    await db.commit()
    return sport, league, home, away


@pytest.mark.asyncio
async def test_reference_pairs_attack_against_defence_not_attack_plus_attack():
    """THE construction that decides whether any of this works.

    Adding both teams' corners-WON measured MAE 2.97 against a 2.75 baseline — worse than
    predicting the league average, and an earlier analysis wrongly concluded from it that no
    historical reference could work. Pairing each attack against the opposing defence measured
    2.74 and beat the baseline.

    Here HOME wins 8 and concedes 2 every match; AWAY (the same two clubs) therefore wins 2 and
    concedes 8. attack+attack would give 8 + 2 = 10 by coincidence, so the history below is
    deliberately asymmetric: a third team would break the tie. Instead we assert the exact
    arithmetic: (8 + 8)/2 + (2 + 2)/2 = 10.
    """
    async with async_session_factory() as db:
        _, _, home, away = await _seed(db, [(8, 2)] * 4, [-10, -8, -6, -4])
        upcoming = Fixture(
            sport_id=(await db.get(Team, home.id)).sport_id,
            league_id=(await db.get(Team, home.id)).league_id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_id=f"crf-up-{uuid.uuid4().hex[:8]}",
            kickoff_utc=datetime.now(UTC) + timedelta(days=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(upcoming)
        await db.commit()

        refs = await bulk_corners_reference(db, [upcoming])

    # home won 8 / conceded 2; away won 2 / conceded 8
    # (home_won + away_conceded)/2 + (away_won + home_conceded)/2 = (8+8)/2 + (2+2)/2 = 10
    assert refs[upcoming.id] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_a_fixture_never_enters_its_own_reference():
    """Only matches STRICTLY BEFORE the fixture count.

    Without this a completed fixture reviewed in the feed would be scored using the very match
    it is predicting, and the historical blend would look far better than it is — the same
    leakage class that put 100% "home won" into the first tennis training run.
    """
    async with async_session_factory() as db:
        _, _, home, away = await _seed(db, [(3, 3), (3, 3), (20, 20)], [-6, -4, -2])
        rows = (
            (
                await db.execute(
                    __import__("sqlalchemy").select(Fixture).where(Fixture.home_team_id == home.id)
                )
            )
            .scalars()
            .all()
        )
        latest = max(rows, key=lambda f: f.kickoff_utc)  # the 20-20 blowout
        refs = await bulk_corners_reference(db, [latest])

    # Only the two earlier 3-3 matches may contribute: (3+3)/2 + (3+3)/2 = 6.
    # Including the fixture itself would drag this toward 6 + its own 40 total.
    assert refs[latest.id] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_too_little_history_returns_none_rather_than_a_one_match_average():
    """One prior match is not an average, it is a single observation. Corners have sd ~3.4 per
    match, so a one-match reference would inject noise with the authority of history."""
    async with async_session_factory() as db:
        _, _, home, away = await _seed(db, [(5, 5)], [-3])
        upcoming = Fixture(
            sport_id=(await db.get(Team, home.id)).sport_id,
            league_id=(await db.get(Team, home.id)).league_id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_id=f"crf-thin-{uuid.uuid4().hex[:8]}",
            kickoff_utc=datetime.now(UTC) + timedelta(days=1),
            status=FixtureStatus.SCHEDULED,
            season="2026",
        )
        db.add(upcoming)
        await db.commit()
        refs = await bulk_corners_reference(db, [upcoming])

    assert MIN_REFERENCE_MATCHES == 2
    assert refs[upcoming.id] is None


def test_corners_candidates_are_blended_but_goals_and_h2h_are_not():
    """The blend is corners-ONLY. Goals Over/Under has a significant reliability trend
    (z=+3.35) and 1X2 beats its baseline, so pulling either toward a historical reference would
    degrade something that already works."""
    from app.fixtures.router import _all_market_candidates

    class _P:
        home_prob, draw_prob, away_prob = 0.5, 0.25, 0.25
        xg_home = xg_away = 1.4
        corners_xg_home = corners_xg_away = 6.0  # 12 total -> strongly OVER 10.5
        feature_completeness = 1.0

    plain = {
        c.market + str(c.selection) + str(c.line): c.probability
        for c in _all_market_candidates(_P(), {}, None)
    }
    # Reference says 6 total corners -> strongly UNDER, the opposite of the model.
    blended = {
        c.market + str(c.selection) + str(c.line): c.probability
        for c in _all_market_candidates(_P(), {}, 6.0)
    }

    assert blended["corners_totalunder10.5"] > plain["corners_totalunder10.5"]
    # Untouched markets must be identical.
    for key in ("h2hhomeNone", "h2hawayNone", "goals_totalunder2.5", "double_chance1XNone"):
        assert blended[key] == plain[key]
