"""Blend the corners model toward a historical attack/defence reference.

The corners model does not predict corners. Measured on 1,277 held-out 2025 fixtures it
correlates with the real count at +0.067, and the picks that actually reached a user's card
hit 52.1% at the 10.5 line while simply always backing the more common side hit 59.4%. The
market was shipping worse-than-a-coin-flip recommendations with a confident percentage on them.

A rolling historical reference does better, and blending the two does better than either alone:

    line 10.5, gated picks    lambda=0.00  52.1%   <- model only, today
                              lambda=0.75  61.7%   <- chosen
                              lambda=1.00  60.0%   <- reference only
    line  9.5, gated picks    lambda=0.00  52.5%
                              lambda=0.75  56.9%
                              lambda=1.00  56.2%

Brier is jointly best at 0.50-0.75 on both lines. 0.75 wins on the gated hit rate, which is
the number that matters -- those are the only picks anybody sees.

WHAT THE REFERENCE IS, AND WHAT IT IS NOT. It pairs each team's ATTACK against the other's
DEFENCE over their last 20 matches:

    (home_corners_won + away_corners_conceded) / 2
  + (away_corners_won + home_corners_conceded) / 2

That construction matters more than it looks. Simply adding both teams' corners-won scored
MAE 2.97 against a 2.75 baseline -- worse than predicting the league average -- and an earlier
analysis wrongly concluded from it that no historical reference could work.

HEAD-TO-HEAD IS DELIBERATELY ABSENT. It is the intuitive candidate and it measured worst of
everything tried: MAE 3.27 alone, and it degraded the blend at every weight (2.744 -> 2.774 at
25%, -> 2.875 at 50%). Every window tested was worse than the all-time mean, and shorter
windows were worse still, because with a median of three prior meetings the estimate is
dominated by noise rather than by any style the two teams share.

Blending happens at PROBABILITY level, per line -- each rate is pushed through its own Poisson
first and the two probabilities are mixed. That is exactly what the backtest measured; mixing
the rates instead and running one Poisson is a different (unmeasured) operation.
"""

from __future__ import annotations

import uuid

CORNERS_HISTORY_BLEND = 0.75
CORNERS_REFERENCE_WINDOW = 20
# Two prior matches is the floor the backtest used (rolling min_periods=2). Below that the
# average is one match, which is noise wearing an average's clothing.
MIN_REFERENCE_MATCHES = 2


def blend_probability(model_prob: float | None, reference_prob: float | None) -> float | None:
    """Mix a model probability with the historical reference's.

    Falls back to whichever side exists. A fixture whose teams have no corner history yet --
    a newly promoted side, a league we only just started ingesting -- keeps the model's own
    number rather than being dropped, which is the same graceful degradation the rest of the
    feature set uses.
    """
    if reference_prob is None:
        return model_prob
    if model_prob is None:
        return reference_prob
    return (1 - CORNERS_HISTORY_BLEND) * model_prob + CORNERS_HISTORY_BLEND * reference_prob


async def bulk_corners_reference(db, fixtures: list) -> dict[uuid.UUID, float | None]:
    """Expected total corners per fixture, from each side's own accumulated history.

    ONE query for a whole page rather than one per team: the per-team helper in
    football_features.py (_corners_rolling_live) would cost two round trips per fixture, which
    is fine at inference time but not on a request path rendering fifty cards.

    Reads our OWN fixture_live_state.home_corners/away_corners, written once per fixture at
    settlement (ingest_fixtures.py:_maybe_fetch_corner_stats). No provider exposes a team's
    historical corners as an aggregate, so this is the only source that does not cost an API
    call per fixture.

    Only matches STRICTLY BEFORE each fixture's own kickoff count, so a completed fixture being
    reviewed in the feed is scored on what was knowable beforehand -- otherwise a past card's
    reference would quietly include the match it is predicting.
    """
    from sqlalchemy import or_, select

    from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus

    if not fixtures:
        return {}

    team_ids = {f.home_team_id for f in fixtures} | {f.away_team_id for f in fixtures}
    team_ids.discard(None)
    if not team_ids:
        return {f.id: None for f in fixtures}

    rows = (
        await db.execute(
            select(
                Fixture.home_team_id,
                Fixture.away_team_id,
                Fixture.kickoff_utc,
                FixtureLiveState.home_corners,
                FixtureLiveState.away_corners,
            )
            .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
            .where(
                Fixture.status == FixtureStatus.COMPLETED,
                FixtureLiveState.home_corners.is_not(None),
                FixtureLiveState.away_corners.is_not(None),
                or_(
                    Fixture.home_team_id.in_(team_ids),
                    Fixture.away_team_id.in_(team_ids),
                ),
            )
            .order_by(Fixture.kickoff_utc.desc())
        )
    ).all()

    # team -> [(kickoff, corners_won, corners_conceded)], newest first.
    history: dict = {}
    for home_id, away_id, kickoff, home_corners, away_corners in rows:
        if home_id in team_ids:
            history.setdefault(home_id, []).append((kickoff, home_corners, away_corners))
        if away_id in team_ids:
            history.setdefault(away_id, []).append((kickoff, away_corners, home_corners))

    def averages(team_id, before) -> tuple[float, float] | None:
        entries = history.get(team_id) or []
        if before is not None:
            entries = [e for e in entries if e[0] < before]
        entries = entries[:CORNERS_REFERENCE_WINDOW]
        if len(entries) < MIN_REFERENCE_MATCHES:
            return None
        won = sum(e[1] for e in entries) / len(entries)
        conceded = sum(e[2] for e in entries) / len(entries)
        return won, conceded

    result: dict[uuid.UUID, float | None] = {}
    for fixture in fixtures:
        home = averages(fixture.home_team_id, fixture.kickoff_utc)
        away = averages(fixture.away_team_id, fixture.kickoff_utc)
        if home is None or away is None:
            result[fixture.id] = None
            continue
        home_won, home_conceded = home
        away_won, away_conceded = away
        # Each side of the total pairs one team's attack with the other's defence.
        result[fixture.id] = (home_won + away_conceded) / 2 + (away_won + home_conceded) / 2
    return result
