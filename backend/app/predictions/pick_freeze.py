"""Freeze the card at kickoff, and serve it from there afterwards.

WHY THIS EXISTS: see FrozenPick's docstring for the measurement (19% of published cards altered
or deleted in five days). This module holds the two halves that must agree -- what gets written,
and what gets read back -- so they cannot drift apart the way the card and the watchlist receipt
did.

THE RULE IS "FREEZE WHAT RENDERS AT THE MOMENT OF FREEZING", not "freeze some canonical pick".
That is deliberate and it is why `is_settled` is passed through rather than hardcoded: at
kickoff a fixture renders as an upcoming card, and on the backfill path an already-settled one
renders as a settled card. Each is the truth for its own case, and picking one rule for both
would rewrite the other.

THE USER'S OWN SLIDERS ARE NOT FROZEN, by explicit decision (2026-09-04). min_probability and
min_odds keep filtering settled cards. That stays coherent because the PICK no longer moves: a
frozen probability cannot drift across a threshold on its own, so a card now only appears or
disappears when the user themselves moves the control -- which is that control working, and is
the distinction the earlier settled-card fix already drew.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fixtures.models import Fixture, FixtureStatus
from app.predictions.models import FrozenPick

logger = logging.getLogger(__name__)


async def frozen_picks_for(db: AsyncSession, fixture_ids: list[uuid.UUID]) -> dict:
    """Every frozen row among these fixtures, keyed by fixture id. One query, not per fixture."""
    if not fixture_ids:
        return {}
    rows = (
        (await db.execute(select(FrozenPick).where(FrozenPick.fixture_id.in_(fixture_ids))))
        .scalars()
        .all()
    )
    return {row.fixture_id: row for row in rows}


async def freeze_started_fixtures(db: AsyncSession, limit: int = 500) -> int:
    """Freeze any fixture whose kickoff has passed and that has no frozen row yet.

    Runs on the five-minute live-scores beat, so the gap between a real kickoff and the freeze is
    at most one cycle. Deliberately keyed on KICKOFF rather than on the status flipping to LIVE:
    a provider that never reports a match as live -- which is most of tennis -- would otherwise
    never freeze, and a fixture that is quietly withdrawn should still have whatever it showed
    recorded rather than nothing.

    POSTPONED fixtures are skipped: they have no pick by design, so there is nothing to protect,
    and freezing a null would only make a rescheduled match unable to regain one.
    """
    now = datetime.now(UTC)
    started = (
        (
            await db.execute(
                select(Fixture.id)
                .outerjoin(FrozenPick, FrozenPick.fixture_id == Fixture.id)
                .where(
                    Fixture.kickoff_utc <= now,
                    Fixture.status != FixtureStatus.POSTPONED,
                    FrozenPick.id.is_(None),
                )
                .order_by(Fixture.kickoff_utc.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not started:
        return 0
    return await _freeze(db, list(started), reason="kickoff")


async def _freeze(db: AsyncSession, fixture_ids: list[uuid.UUID], *, reason: str) -> int:
    """Compute each fixture's card exactly as it renders now, and write it down once.

    Imported here rather than at module scope: app.fixtures.router imports this module for the
    read path, so a top-level import in either direction is a cycle.
    """
    from app.fixtures.router import _bulk_best_picks

    # NO CALLER THRESHOLDS. The pick is frozen; the user's sliders filter it at read time. Baking
    # one user's slider position into a stored record would make the card personal to whoever
    # happened to trigger the freeze.
    best, _all = await _bulk_best_picks(db, fixture_ids)

    written = 0
    for fixture_id in fixture_ids:
        pick = best.get(fixture_id)
        db.add(
            FrozenPick(
                fixture_id=fixture_id,
                market=pick.market if pick else None,
                selection=pick.selection if pick else None,
                line=pick.line if pick else None,
                probability=pick.probability if pick else None,
                odds=pick.odds if pick else None,
                feature_completeness=pick.feature_completeness if pick else None,
                model_version=None,
                prediction_created_at=pick.as_of if pick else None,
                frozen_reason=reason,
            )
        )
        written += 1
    await db.commit()
    logger.info("froze %d cards (%s)", written, reason)
    return written


def apply_frozen(pick_class, frozen: FrozenPick):
    """Rebuild a BestPick from a frozen row, or None when the card showed no pick.

    `pick_class` is passed in rather than imported so this module stays free of the schemas
    package -- the same cycle avoidance as above.
    """
    if frozen.market is None:
        return None
    return pick_class(
        market=frozen.market,
        selection=frozen.selection,
        line=frozen.line,
        probability=frozen.probability,
        odds=frozen.odds,
        feature_completeness=frozen.feature_completeness,
        as_of=frozen.prediction_created_at,
        # DELIBERATELY DROPPED. `previous_probability` says how the number MOVED, and a frozen
        # number does not move; `live_status` and the driver rows are read-time commentary on a
        # match in play, not part of the bet that was shown.
        previous_probability=None,
        live_status=None,
        drivers=None,
        drivers_are_market_blind=False,
    )
