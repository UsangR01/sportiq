"""League-level scoring and home-advantage baselines — partial pooling for the football model.

The model trains on five pooled leagues but has no feature that identifies which one a fixture
belongs to. Measured, those leagues are genuinely different: Brasileirao averages 2.411 goals
per match against EPL's 2.927 and MLS's 2.930. Pooling without that signal forces one blended
scoring level onto all of them, which is exactly what produced the Over/Under overconfidence
(P(under 3.5) pulled toward Brasileirao's 0.789 and applied to leagues truly at ~0.66).

Two continuous features rather than a categorical league id, deliberately:

  - XGBoost splits cleanly on a real number; a league id would need encoding and would carry
    no meaning for a league the model has never seen.
  - A newly added league gets its own measured rate immediately, with no retrain of an
    encoding and no code change — which matches this project's sport-agnostic design, where
    adding a league is a data insert rather than a schema change.
  - The features say WHAT differs (scoring level, home advantage) rather than merely WHICH
    league it is, so the model can generalise across leagues that behave alike.

This is partial pooling: one model keeps the sample size of five leagues while gaining the
per-league offsets that a separate model per league would give, without splitting ~1,100
training fixtures per league across five much noisier fits.

Leakage guard: every baseline is an EXPANDING mean over matches strictly BEFORE the fixture
being predicted. A league's full-season average would leak the very matches being scored.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date

import pandas as pd

# Below this many prior matches a league's own average is mostly noise, so the pooled
# all-league average is used instead. A brand-new league otherwise gets a baseline built from
# a handful of games, which is worse than borrowing the global level until it settles.
MIN_MATCHES_FOR_OWN_BASELINE = 30


@dataclass(frozen=True)
class LeagueBaseline:
    """What the model is told about the competition a fixture sits in."""

    avg_goals: float
    home_win_rate: float
    #: Mean TOTAL corners per match in this league, or None where corners are not collected.
    #:
    #: The missing sibling of avg_goals, and the asymmetry was doing real damage. The corners
    #: regressors saw league_avg_goals and league_home_win_rate -- both about GOALS -- and
    #: nothing at all about a league's corner level, while P(over 9.5) measured across 28k
    #: fixtures runs from 0.435 in Liga I to 0.607 in the Scottish Premiership. A 17-point
    #: spread the model could not see.
    #:
    #: Consequence, measured on real cards: every over-9.5 pick claimed on average +18.9pp
    #: ABOVE its own league's base rate, and the size of that claim carried no information --
    #: winners claimed +17.6pp, losers +19.8pp.
    #:
    #: Nullable rather than defaulted: a league with no corner history must reach XGBoost as
    #: missing, not as a fabricated average.
    avg_corners: float | None = None


class LeagueBaselines:
    """Chronological expanding baselines per league, queried by (league, date).

    Built once over the whole pooled game log — the same "walk the log once" contract as
    app/models_ml/elo.py, and for the same reason: this is a running state, not a value each
    fixture can re-derive independently without rescanning everything."""

    def __init__(self, dates: dict[str, list[date]], values: dict[str, list[LeagueBaseline]]):
        self._dates = dates
        self._values = values

    def get(self, league: str | None, as_of: date) -> LeagueBaseline | None:
        """The baseline as it stood strictly before as_of, or None if nothing precedes it."""
        if league is None or league not in self._dates:
            return None
        dates = self._dates[league]
        idx = bisect.bisect_left(dates, as_of)
        if idx == 0:
            return None
        return self._values[league][idx - 1]


def _row_corners(row) -> float | None:
    """Total corners for one fixture, from the columns merge_corners_into_game_log attaches.

    Absent for a league whose corners were never collected, and for individual fixtures the
    provider published no statistics for -- both must stay None rather than becoming a zero.
    """
    for_, against = getattr(row, "CORNERS_FOR", None), getattr(row, "CORNERS_AGAINST", None)
    if for_ is None or against is None or pd.isna(for_) or pd.isna(against):
        return None
    return float(for_) + float(against)


def compute_league_baselines(games: pd.DataFrame) -> LeagueBaselines:
    """Walks the pooled game log once in date order, accumulating per-league running means.

    Requires a LEAGUE column (added when the per-league parquets are concatenated) and the
    home rows only — a game log carries two rows per fixture, so counting both would double
    every total and make home_win_rate meaningless."""
    if "LEAGUE" not in games.columns:
        return LeagueBaselines({}, {})

    home = games[games["HOME_AWAY"] == "home"].copy()
    home["GAME_DATE"] = pd.to_datetime(home["GAME_DATE"]).dt.date
    home = home.sort_values("GAME_DATE")

    # Global running totals, used as the fallback while a league is still too thin to trust.
    all_goals = all_home_wins = all_matches = 0
    all_corners = all_corner_matches = 0.0
    per_league: dict[str, list[int | float]] = {}
    dates: dict[str, list[date]] = {}
    values: dict[str, list[LeagueBaseline]] = {}

    for row in home.itertuples():
        league = row.LEAGUE
        goals = int(row.GF) + int(row.GA)
        home_win = 1 if row.WDL == "W" else 0

        # goals, home wins, matches, corners, matches WITH a real corner count
        stats = per_league.setdefault(league, [0, 0, 0, 0.0, 0])
        stats[0] += goals
        stats[1] += home_win
        stats[2] += 1
        all_goals += goals
        all_home_wins += home_win
        all_matches += 1

        # Corners are counted on their OWN denominator, because coverage is partial and uneven
        # -- Veikkausliiga sits at 44% where most leagues clear 90%. Dividing corner totals by
        # the match count would silently deflate exactly the leagues with the thinnest data.
        corners = _row_corners(row)
        if corners is not None:
            stats[3] += corners
            stats[4] += 1
            all_corners += corners
            all_corner_matches += 1

        if stats[2] >= MIN_MATCHES_FOR_OWN_BASELINE:
            avg_goals, home_rate = stats[0] / stats[2], stats[1] / stats[2]
        else:
            avg_goals, home_rate = all_goals / all_matches, all_home_wins / all_matches
        if stats[4] >= MIN_MATCHES_FOR_OWN_BASELINE:
            avg_corners = stats[3] / stats[4]
        elif all_corner_matches >= MIN_MATCHES_FOR_OWN_BASELINE:
            avg_corners = all_corners / all_corner_matches
        else:
            avg_corners = None
        baseline = LeagueBaseline(avg_goals, home_rate, avg_corners)

        dates.setdefault(league, []).append(row.GAME_DATE)
        values.setdefault(league, []).append(baseline)

    return LeagueBaselines(dates, values)


async def league_baseline_from_db(db, league_id, as_of) -> LeagueBaseline | None:
    """Live counterpart to compute_league_baselines, read from our own settled fixtures.

    Serving must see the same kind of number training saw, or the two drift apart and the
    feature does more harm than leaving it absent. Same expanding-window definition: only
    fixtures that finished strictly BEFORE this one, in this league.

    Returns None below MIN_MATCHES_FOR_OWN_BASELINE rather than a value built from a handful
    of games — XGBoost treats that as missing, which is honest, whereas a noisy estimate would
    be quietly wrong. Training falls back to the pooled average at that point; serving cannot,
    since it has no pooled log to hand, so absent is the safer of the two.
    """
    from sqlalchemy import Integer, func, select

    from app.fixtures.models import Fixture, FixtureLiveState, FixtureStatus

    row = (
        await db.execute(
            select(
                func.count(),
                func.avg(FixtureLiveState.home_score + FixtureLiveState.away_score),
                func.avg(
                    func.cast(FixtureLiveState.home_score > FixtureLiveState.away_score, Integer)
                ),
                # Its own COUNT and AVG: corner coverage is partial, so these must not be
                # divided by the goals denominator. NULLs are excluded by both aggregates.
                func.count(FixtureLiveState.home_corners),
                func.avg(FixtureLiveState.home_corners + FixtureLiveState.away_corners),
            )
            .select_from(Fixture)
            .join(FixtureLiveState, FixtureLiveState.fixture_id == Fixture.id)
            .where(
                Fixture.league_id == league_id,
                Fixture.status == FixtureStatus.COMPLETED,
                Fixture.kickoff_utc < as_of,
                FixtureLiveState.home_score.is_not(None),
                FixtureLiveState.away_score.is_not(None),
            )
        )
    ).first()

    if row is None:
        return None
    count, avg_goals, home_rate, corner_count, avg_corners = row
    if not count or count < MIN_MATCHES_FOR_OWN_BASELINE or avg_goals is None:
        return None
    corners = (
        float(avg_corners)
        if corner_count and corner_count >= MIN_MATCHES_FOR_OWN_BASELINE and avg_corners is not None
        else None
    )
    return LeagueBaseline(float(avg_goals), float(home_rate or 0.0), corners)
