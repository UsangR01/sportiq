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


# How many fixtures one sweep will freeze, and how many go into a single _bulk_best_picks call.
#
# CHUNK IS THE LOAD-BEARING ONE. That helper's corners reference scales with distinct TEAMS and
# has OOM-killed a 512MB container twice at larger batches (see the note on measure_pick_flips in
# CLAUDE.md). 40 is the size measured safe there. PER_RUN then bounds a cold start: the first
# sweeps after this shipped had ~3,300 fixtures to catch up on, and draining that in one cycle
# would spend the whole five minutes -- and the whole memory budget -- on a backfill while live
# scores waited behind it. At 200 per five-minute cycle the catch-up finishes in under two hours
# and no single cycle is heavy.
FREEZE_CHUNK = 40
FREEZE_PER_RUN = 200


async def freeze_started_fixtures(db: AsyncSession, limit: int = FREEZE_PER_RUN) -> int:
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
    frozen = 0
    for start in range(0, len(started), FREEZE_CHUNK):
        frozen += await _freeze(db, list(started[start : start + FREEZE_CHUNK]), reason="kickoff")
    return frozen


async def backfill_frozen_candidates(db: AsyncSession, limit: int = FREEZE_PER_RUN) -> int:
    """Give a candidate set to rows frozen before that column existed.

    Roughly 3,300 rows were written by the winner-only version, and each one is a card whose
    slider can only delete rather than swap. They cannot be left: that IS the reported bug.

    ON THE BEAT RATHER THAN IN A SCRIPT, because repairing production has repeatedly depended on
    a Render shell that keeps dropping its connection -- the registry outage a few days ago sat
    unfixed for forty minutes for exactly that reason. A sweep that heals itself needs nobody to
    be available.

    STATED PLAINLY: this re-derives the candidate set with TODAY's guards, because the set was
    never recorded. Those rows are days old at most, but it is a reconstruction and the row's
    frozen_at is left untouched so the original capture time survives.
    """
    stale = (
        (
            await db.execute(
                select(FrozenPick.fixture_id)
                .where(FrozenPick.candidates.is_(None))
                .order_by(FrozenPick.frozen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0

    from app.fixtures.router import _bulk_best_picks

    repaired = 0
    for start in range(0, len(stale), FREEZE_CHUNK):
        chunk = list(stale[start : start + FREEZE_CHUNK])
        _best, _all, surviving = await _bulk_best_picks(db, chunk, with_survivors=True)
        rows = (
            (await db.execute(select(FrozenPick).where(FrozenPick.fixture_id.in_(chunk))))
            .scalars()
            .all()
        )
        for row in rows:
            row.candidates = [
                {
                    "market": c.market,
                    "selection": c.selection,
                    "line": c.line,
                    "probability": c.probability,
                    "odds": c.odds,
                    "feature_completeness": c.feature_completeness,
                }
                for c in surviving.get(row.fixture_id, [])
            ]
            repaired += 1
        await db.commit()
    logger.info("gave a candidate set to %d frozen cards", repaired)
    return repaired


async def _freeze(db: AsyncSession, fixture_ids: list[uuid.UUID], *, reason: str) -> int:
    """Compute each fixture's card exactly as it renders now, and write it down once.

    Imported here rather than at module scope: app.fixtures.router imports this module for the
    read path, so a top-level import in either direction is a cycle.
    """
    from app.fixtures.router import _bulk_best_picks

    # NO CALLER THRESHOLDS. The sliders are the user's and run at read time; baking one user's
    # slider position into a stored record would make the card personal to whoever happened to
    # trigger the freeze.
    #
    # `surviving` is the whole point -- see FrozenPick.candidates. The winner is stored too, but
    # only so a row written before that column existed still renders.
    best, _all, surviving = await _bulk_best_picks(db, fixture_ids, with_survivors=True)

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
                candidates=[
                    {
                        "market": c.market,
                        "selection": c.selection,
                        "line": c.line,
                        "probability": c.probability,
                        "odds": c.odds,
                        "feature_completeness": c.feature_completeness,
                    }
                    for c in surviving.get(fixture_id, [])
                ],
                frozen_reason=reason,
            )
        )
        written += 1
    await db.commit()
    logger.info("froze %d cards (%s)", written, reason)
    return written


def apply_frozen(
    pick_class,
    frozen: FrozenPick,
    *,
    min_probability: float | None = None,
    min_odds: float | None = None,
):
    """The card this fixture showed, chosen from its FROZEN candidates against the caller's own
    sliders. None when nothing the user allows survived.

    THE SLIDERS PICK AMONG CANDIDATES, THEY DO NOT VETO A WINNER, and getting that backwards is
    what broke the feed for a day: a frozen 1.02 favourite was simply deleted at a 1.20 floor
    instead of yielding to the goals line sitting behind it.

    `pick_class` is passed in rather than imported so this module stays free of the schemas
    package -- the same cycle avoidance as above.
    """
    if frozen.candidates:
        from app.fixtures.router import _MarketCandidate, _rank_survivors

        chosen = _rank_survivors(
            [
                _MarketCandidate(
                    selection=c["selection"],
                    probability=c["probability"],
                    odds=c["odds"],
                    market=c["market"],
                    line=c["line"],
                    feature_completeness=c.get("feature_completeness"),
                    as_of=frozen.prediction_created_at,
                )
                for c in frozen.candidates
            ],
            min_probability=min_probability,
            min_odds=min_odds,
        )
        if chosen is None:
            return None
        return pick_class(
            market=chosen.market,
            selection=chosen.selection,
            line=chosen.line,
            probability=chosen.probability,
            odds=chosen.odds,
            feature_completeness=chosen.feature_completeness,
            as_of=frozen.prediction_created_at,
            previous_probability=None,
            live_status=None,
            drivers=None,
            drivers_are_market_blind=False,
        )

    # A row written before `candidates` existed. Fall back to the stored winner rather than
    # going blank, and let the caller's own filter judge it as it always did.
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
