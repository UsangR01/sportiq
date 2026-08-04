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
    per_league: dict[str, list[int | float]] = {}
    dates: dict[str, list[date]] = {}
    values: dict[str, list[LeagueBaseline]] = {}

    for row in home.itertuples():
        league = row.LEAGUE
        goals = int(row.GF) + int(row.GA)
        home_win = 1 if row.WDL == "W" else 0

        stats = per_league.setdefault(league, [0, 0, 0])  # goals, home wins, matches
        stats[0] += goals
        stats[1] += home_win
        stats[2] += 1
        all_goals += goals
        all_home_wins += home_win
        all_matches += 1

        if stats[2] >= MIN_MATCHES_FOR_OWN_BASELINE:
            baseline = LeagueBaseline(stats[0] / stats[2], stats[1] / stats[2])
        else:
            baseline = LeagueBaseline(all_goals / all_matches, all_home_wins / all_matches)

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
    count, avg_goals, home_rate = row
    if not count or count < MIN_MATCHES_FOR_OWN_BASELINE or avg_goals is None:
        return None
    return LeagueBaseline(float(avg_goals), float(home_rate or 0.0))
