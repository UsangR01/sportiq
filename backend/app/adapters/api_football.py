import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    InjuryUpdate,
    OddsPayload,
    TeamStats,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Confirmed via live research (see CLAUDE.md): the real, working base host and auth header —
# NOT RapidAPI, despite TDD §2.2 saying "API-Football (via RapidAPI)". Auth is a raw key value
# under x-apisports-key, no Bearer/host header needed on this host.
BASE_URL = "https://v3.football.api-sports.io"

# Real league IDs, confirmed live via GET /leagues?name=X&country=Y (or ?search=/?country= when
# the league's own `name` field doesn't literally contain the common English name — e.g.
# Scotland's top flight is just "Premiership" in API-Football's data, and MLS is "Major League
# Soccer", not "MLS") — the 5 original European leagues must match
# app/adapters/therundown.py's _RUNDOWN_SPORT_IDS keys exactly so odds ingestion resolves to the
# same League.slug rows. "brasileirao"/"scottish_prem"/"csl" are deliberate exceptions:
# confirmed live (see CLAUDE.md) that TheRundown's own /sports list has no Brazil or China
# league entry, and no Scotland entry either — odds ingestion for these leagues gracefully
# no-ops on the TheRundown side (see ingest_odds.py) rather than raising, since fixtures/stats/
# injuries/predictions don't depend on TheRundown coverage existing, and all three have real
# API-Football odds coverage instead (confirmed live via each league's current-season
# coverage.odds flag).
LEAGUE_IDS: dict[str, int] = {
    "epl": 39,
    "ligue1": 61,
    "bundesliga": 78,
    "laliga": 140,
    "seriea": 135,
    "brasileirao": 71,  # Brazil Serie A ("Brasileirão Betano") — confirmed live: id 71
    "scottish_prem": 179,  # Scottish Premiership — confirmed live: id 179, country=Scotland
    "mls": 253,  # Major League Soccer (USA) — confirmed live: id 253, NOT "MLS" by name search
    "csl": 169,  # Chinese Super League — confirmed live: id 169, country=China
    # The nine Tier-1 leagues the model is now trained on (see ml/training/train_football.py's
    # LEAGUES). They were collected and pooled into the model before being added here, which
    # meant the model had learned from them while the app ingested nothing for them — no
    # fixture, odds or prediction ever reached a user. Training config and this map are
    # separate wirings and had silently drifted apart.
    "allsvenskan": 113,
    "eliteserien": 103,
    "veikkausliiga": 244,
    "ekstraklasa": 106,
    "denmark_superliga": 119,
    "liga_i": 283,
    "j1_league": 98,
    "czech_first": 345,
    "austria_bundesliga": 218,
}

# Leagues whose season runs on the calendar year (Jan-Dec) rather than the European Aug-May
# convention — confirmed live for Brasileirão: current season "2026" runs 2026-01-28 to
# 2026-12-02. Using the European convention here would compute the WRONG season year for
# most of the year (e.g. any month before July would look back a full year too far). MLS
# (2026 season: 2026-02-21 to 2026-11-08) and the Chinese Super League (2026 season:
# 2026-03-06 to 2026-11-08) are the same calendar-year shape, confirmed live the same way —
# Scottish Premiership stays out of this set, its 2026 season runs 2026-07-31 to 2027-04-10,
# the same Aug-May convention as the 5 original European leagues.
#
# The four Nordic/Japanese additions were confirmed the same way, but from the real match dates
# already collected in ml/data/football_game_log_{league}.parquet rather than by spending API
# calls: Allsvenskan 2025 ran 2025-03-29 to 2025-11-29, Eliteserien 2025-03-29 to 2025-12-11,
# Veikkausliiga 2025-04-05 to 2025-11-09, J1 League 2025-02-14 to 2025-12-06 — all opening and
# closing inside one calendar year. The other five Tier-1 leagues stay out of this set, equally
# confirmed: Ekstraklasa 2025 ran 2025-07-18 to 2026-05-23, Danish Superliga 2025-07-18 to
# 2026-05-21, Liga I 2025-07-11 to 2026-06-01, Czech First 2025-07-18 to 2026-05-31, Austrian
# Bundesliga 2025-08-01 to 2026-05-25.
CALENDAR_YEAR_SEASON_LEAGUES = {
    "brasileirao",
    "mls",
    "csl",
    "allsvenskan",
    "eliteserien",
    "veikkausliiga",
    # NOTE: the J1 League is NOT here despite its 2021-2025 history being calendar-year. See
    # END_YEAR_SEASON_LEAGUES below — Japan switched conventions from 2026-27.
}

# A third convention, found live and not derivable from history: a league on an Aug-May window
# whose season API-Football labels by the year it ENDS.
#
# The J1 League is the only known case, and only because Japan is mid-transition from a
# calendar-year season to an autumn-spring one. Confirmed live via /leagues?id=98:
#
#     season 2025  2025-02-14 -> 2025-12-06   (calendar year, as the collected history shows)
#     season 2026  2026-02-06 -> 2026-06-06   (a short transitional season)
#     season 2027  2026-08-07 -> 2027-06-06   <- current=true on 2026-08-10
#
# So the season running RIGHT NOW is labelled 2027. Neither existing rule produces that: the
# calendar-year rule gives 2026, and the European start-year rule also gives 2026. The
# provider is not being inconsistent for its own sake -- it follows each league's own naming,
# and the EPL's 2026-27 season really is labelled 2026 while Japan's really is labelled 2027.
#
# HOW THIS WAS CAUGHT, because the failure mode is silent: ingesting the J1 League returned
# zero fixtures with HTTP 200, an empty `errors` object and results=0 -- indistinguishable
# from "this league genuinely has no matches this week". It was only found by comparing our
# computed season against the `current: true` flag for all 18 leagues at once, which showed
# 17 correct and this one wrong. Do that comparison rather than trusting an empty response.
#
# STRUCTURAL FOLLOW-UP worth taking: this is the second season-convention bug (Brasileirão was
# the first), and the provider already publishes the authoritative answer via that `current`
# flag. Resolving the season from /leagues with a cached lookup, falling back to these rules,
# would remove the whole class of bug rather than accumulating a fourth hardcoded set.
END_YEAR_SEASON_LEAGUES = {"j1_league"}

# How long a resolved current-season label is trusted. A season boundary moves a handful of
# times a year, so this is deliberately long: 18 leagues at two refreshes a day is ~36 calls
# against a 75,000/day allowance.
SEASON_CACHE_TTL_SECONDS = 12 * 60 * 60
_SEASON_CACHE: dict[str, tuple[int, float]] = {}


def _cached_season(league: str) -> int | None:
    entry = _SEASON_CACHE.get(league)
    if entry is None or entry[1] <= time.monotonic():
        return None
    return entry[0]


async def resolve_current_season(client: httpx.AsyncClient, league: str) -> int:
    """The season label API-Football itself considers current, falling back to the hardcoded
    conventions above.

    Those conventions have now been wrong three times, each silently: Brasileirão runs Jan-Dec
    rather than Aug-May, four Tier-1 leagues likewise, and the J1 League labels its season by
    the year it ENDS. The failure mode never raises -- asking for the wrong season returns
    HTTP 200 with an empty `errors` object and results=0, which is indistinguishable from "no
    matches scheduled". The J1 League ingested nothing at all until its computed season was
    compared against this very field.

    A convention is a guess about a league's calendar; `current: true` is the provider stating
    it. Preferring the statement removes the whole class rather than accumulating a fourth
    hardcoded set -- so a league that changes its calendar (as Japan just did) now corrects
    itself within the cache TTL instead of needing a code change.

    Best-effort by design: any failure falls back to _current_football_season, so this can only
    improve on the previous behaviour, never take ingestion down. The fallback is also why the
    convention sets are kept rather than deleted.
    """
    cached = _cached_season(league)
    if cached is not None:
        return cached

    fallback = _current_football_season(league)
    league_id = LEAGUE_IDS.get(league)
    if league_id is None:
        return fallback

    try:
        response = await client.get("/leagues", params={"id": league_id})
        response.raise_for_status()
        entries = _api_response(response).get("response") or []
        seasons = entries[0].get("seasons", []) if entries else []
        current = next((s for s in seasons if s.get("current")), None)
        season = int(current["year"]) if current and current.get("year") is not None else None
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        logger.warning(
            "Could not resolve the current season for league=%s; falling back to the "
            "hardcoded convention (%s)",
            league,
            fallback,
            exc_info=True,
        )
        return fallback

    if season is None:
        return fallback
    if season != fallback:
        # Worth surfacing: it means a hardcoded convention is now wrong for this league.
        logger.info(
            "League %s: provider reports season %s, hardcoded convention says %s — using %s",
            league,
            season,
            fallback,
            season,
        )
    _SEASON_CACHE[league] = (season, time.monotonic() + SEASON_CACHE_TTL_SECONDS)
    return season


INJURY_LOOKAHEAD_DAYS = 3  # how far ahead fetch_injuries looks for fixtures to check dates for


class APIFootballQuotaExceeded(httpx.HTTPError):
    """The daily/plan request limit is spent. Not an outage, and not an empty schedule.

    Subclasses httpx.HTTPError deliberately. Every ingest worker already isolates one league or
    one adapter from the rest by catching httpx.HTTPError, so inheriting from it means a spent
    allowance is logged and skipped exactly like any other provider failure. As a bare
    RuntimeError it escaped that isolation and took down the whole live-score run, stopping
    TENNIS updates too -- worse than the silent failure it was written to replace.
    """


def _api_response(response) -> dict:
    """Return the parsed body, raising if API-Football reported an error.

    API-Football signals quota exhaustion with HTTP **200** plus an `errors` object and an
    EMPTY `response` list. raise_for_status() therefore passes and .get("response", [])
    yields [], so running out of quota is indistinguishable from "nothing scheduled today"
    unless the errors field is actually read.

    That was live for real: with the daily limit spent, ingest_live_scores completed without a
    single warning and simply stopped updating football scores. This codebase has been bitten
    by the same shape twice before -- the injury worker failing unnoticed for weeks, and the
    odds outage that presented as a modelling problem -- which is why a loud exception is worth
    more here than a tidy empty list.

    `errors` comes back as a LIST when there is nothing to report and a DICT when there is;
    both arrive with HTTP 200.
    """
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors")
    if isinstance(errors, dict) and errors:
        message = "; ".join(f"{key}: {value}" for key, value in errors.items())
        if "limit" in message.lower() or "quota" in message.lower():
            raise APIFootballQuotaExceeded(message)
        raise httpx.HTTPError(f"API-Football error: {message}")
    return payload


def _current_football_season(league: str, now: datetime | None = None) -> int:
    """API-Football labels a season by the year it starts. For the 5 European leagues that's
    Aug-May (e.g. 2025 for "2025-26") — same start-year convention as BallDontLie's NBA
    seasons, just on a different calendar. Brasileirão instead runs Jan-Dec of a single
    calendar year (see CALENDAR_YEAR_SEASON_LEAGUES) — a real distinction found live while
    adding this league, not something either convention can be assumed from the other.
    """
    now = now or datetime.now(UTC)
    if league in CALENDAR_YEAR_SEASON_LEAGUES:
        return now.year
    season = now.year if now.month >= 7 else now.year - 1
    return season + 1 if league in END_YEAR_SEASON_LEAGUES else season


#  Confirmed live on a real matchday (see CLAUDE.md): API-Football genuinely returns "PST"
# for a real postponed fixture, not a hypothetical edge case — 4 real Brasileirão fixtures
# were postponed while backfilling for this feature. fixtures.status originally had no
# cancelled/postponed value (a pre-existing, documented TDD §2.1/§2.3 gap — see CLAUDE.md);
# these used to map to "scheduled" as the least-misleading available bucket, which turned out
# to be actively misleading in its own right — the mobile Picks feed kept showing a live
# market prediction/odds badge for a game that was never going to be played (the user's own
# report, from a real Brasileirão postponement: "originally scheduled games... postponed...
# but they are still on display"). Now maps to its own POSTPONED status instead (see
# app/fixtures/models.py:FixtureStatus) — still one shared bucket for all 8 of these
# non-live-non-scheduled states, not 8 individually modeled ones, same scope cut as before.
_NOT_ACTUALLY_LIVE_STATUSES = {"PST", "CANC", "ABD", "SUSP", "INT", "TBD", "AWD", "WO"}


def _map_status(short_status: str) -> str:
    """API-Football's fixture.status.short: "NS" (not started) before kickoff, "FT"/"AET"/
    "PEN" once finished, "PST"/"CANC"/etc. are real non-live, non-scheduled states (see
    _NOT_ACTUALLY_LIVE_STATUSES), anything else (1H/HT/2H/ET/BT/P) is genuinely in progress.
    """
    if short_status == "NS":
        return "scheduled"
    if short_status in _NOT_ACTUALLY_LIVE_STATUSES:
        return "postponed"
    if short_status in ("FT", "AET", "PEN"):
        return "completed"
    return "live"


def _map_fixture_to_payload(fixture: dict, league_slug: str) -> FixturePayload:
    """Pure, network/DB-free mapping — kept separate so it's directly unit-testable against a
    recorded sample response, mirroring app/adapters/balldontlie.py's pattern."""
    fx = fixture["fixture"]
    league = fixture["league"]
    teams = fixture["teams"]
    goals = fixture.get("goals", {})
    return FixturePayload(
        external_id=str(fx["id"]),
        league_external_id=league_slug,
        home_team_external_id=str(teams["home"]["id"]),
        away_team_external_id=str(teams["away"]["id"]),
        kickoff_utc=datetime.fromisoformat(fx["date"].replace("Z", "+00:00")),
        season=str(league["season"]),
        home_team_name=teams["home"].get("name"),
        away_team_name=teams["away"].get("name"),
        # API-Football has no short "abbreviation" field like BallDontLie's — only a full
        # team name and a separate logo URL. TheRundown's teams_normalized.abbreviation is
        # what fixture-matching (find_fixture_by_abbreviations_and_time) actually needs to
        # match against; API-Football's own name is used as short_name until proven otherwise
        # — same "best available field" tradeoff noted for other cross-provider joins.
        home_team_short_name=teams["home"].get("name"),
        away_team_short_name=teams["away"].get("name"),
        status=_map_status(fx["status"]["short"]),
        home_score=goals.get("home"),
        away_score=goals.get("away"),
        match_minute=fx["status"].get("elapsed"),
    )


def _parse_form_points(form: str | None) -> float | None:
    """Converts API-Football's real "WWDLW..." form string (most-recent last) into a
    3/1/0-points-per-game average over the LAST LAST_N_FORM MATCHES.

    THE WINDOW IS THE WHOLE POINT, and it was missing until 2026-08-18. This averaged the
    ENTIRE form string, which is the team's season to date, while training computes the same
    feature over a rolling LAST_N_FORM window (football_features._rolling_form). A train/serve
    mismatch on one of the model's core inputs, and one that grows with every match played --
    invisible in a league that has just kicked off, worst in a long season.

    Caught from a real report of MLS predictions. Philadelphia Union, 19 matches in, form
    string LLLLLLWDDLDLLDLWWWW:

        season-wide         5W 4D 10L = 19 points / 19 = 1.0   <- what the model was told
        last 10 LDLLDLWWWW        14 points / 10 = 1.4          <- what training means
        last 4              four straight wins, confirmed against their real fixtures

    So a side on a four-game winning run was described to the model as the worst in the
    league, while win_streak_home said 4.0 -- two features flatly contradicting each other.
    Across a mid-season league this compresses every team's form toward the same mid-table
    number, the feature stops separating anybody, and the model falls back on home advantage:
    MLS away-win probability collapsed to a mean of 0.048 and all 30 cards showed 1X at ~95%.

    LAST_N_FORM is imported rather than redeclared so the two definitions cannot drift; the
    import is function-level because football_features imports this module back.
    """
    if not form:
        return None
    from app.models_ml.football_features import LAST_N_FORM

    points = {"W": 3, "D": 1, "L": 0}
    values = [points[c] for c in form if c in points]
    if not values:
        return None
    # Most-recent LAST, verified live: Philadelphia's string ends WWWW and their real last four
    # fixtures are four wins. So the tail is the recent window.
    recent = values[-LAST_N_FORM:]
    return sum(recent) / len(recent)


def _parse_streaks(form: str | None) -> tuple[float | None, float | None]:
    """Same "WWDLW..." form string (most-recent last), read backward from the end to find the
    team's current consecutive streak. A draw at the most recent match breaks both streaks
    (0, 0) — a draw is neither a win nor a loss, so no streak of either kind survives it."""
    if not form:
        return None, None
    if form[-1] == "D":
        return 0.0, 0.0
    target = form[-1]
    streak = 0
    for c in reversed(form):
        if c != target:
            break
        streak += 1
    if target == "W":
        return float(streak), 0.0
    return 0.0, float(streak)


def _compute_team_stats(team_external_id: str, stats: dict) -> TeamStats:
    """Derives everything real /teams/statistics provides (form, goals-average, home/away
    win rate) — confirmed live (see CLAUDE.md): no elo_rating/xg fields exist in this
    response at any tier, so those stay None, same honest-gap pattern as NBA's pace
    differential."""
    fixtures = stats.get("fixtures", {})
    goals = stats.get("goals", {})

    played_home = fixtures.get("played", {}).get("home") or 0
    played_away = fixtures.get("played", {}).get("away") or 0
    wins_home = fixtures.get("wins", {}).get("home") or 0
    wins_away = fixtures.get("wins", {}).get("away") or 0

    home_win_rate = (wins_home / played_home) if played_home else None
    away_win_rate = (wins_away / played_away) if played_away else None

    # Season-wide averages, NOT a last-5 rolling window — /teams/statistics has no per-match
    # breakdown to compute a true rolling average from at serving time. A real, documented
    # train/serve divergence from app/models_ml/football_features.py's training-time
    # attack_str/defence_str (computed from an exact last-5-match window over the collected
    # game log) — same spirit as NBA's home_court_indicator always being a constant 1.0,
    # flagged here rather than silently accepted.
    #
    # Confirmed live: before a season's first match, API-Football returns "0.0" (a string,
    # not null) for these averages rather than omitting them — treated as genuinely missing
    # (None), not a real "zero goals" signal, matching this codebase's "never fabricate a
    # neutral value" convention (see app/models_ml/nba_features.py's own docstring).
    has_played = (played_home + played_away) > 0
    attack_str_raw = goals.get("for", {}).get("average", {}).get("total") if has_played else None
    defence_str_raw = (
        goals.get("against", {}).get("average", {}).get("total") if has_played else None
    )

    win_streak, losing_streak = _parse_streaks(stats.get("form"))

    return TeamStats(
        team_external_id=team_external_id,
        elo_rating=None,  # see Team.elo_rating for the real, persistent source of this now
        attack_str=float(attack_str_raw) if attack_str_raw is not None else None,
        defence_str=float(defence_str_raw) if defence_str_raw is not None else None,
        form_pts_5=_parse_form_points(stats.get("form")),
        xg_for_5=None,
        xg_against_5=None,
        days_since_last_match=None,  # not derivable from this aggregate-only endpoint
        home_win_rate=home_win_rate,
        away_win_rate=away_win_rate,
        season_point_diff=None,
        win_streak=win_streak,
        losing_streak=losing_streak,
    )


MATCH_WINNER_BET_NAME = "Match Winner"  # API-Football's own name for the h2h/moneyline market
DOUBLE_CHANCE_BET_NAME = "Double Chance"
GOALS_OVER_UNDER_BET_NAME = "Goals Over/Under"
CORNERS_OVER_UNDER_BET_NAME = "Corners Over Under"

# The lines this product supports (see app/models_ml/markets.py) — real bookmaker responses
# offer many more (0.5, 1.5, ..., 4.5+ for goals; 6.5 through 14.5+ for corners), but only
# these are ever mapped, matching the scope confirmed useful live (CLAUDE.md).
GOALS_LINES = (1.5, 2.5, 3.5, 4.5)
# 9.5 is the standard, most-traded corners line (confirmed live via Bet365); 10.5 was added
# alongside it on request. Both are commonly offered, so this is an ingestion question, not a
# modelling one - the Poisson CDF in app/models_ml/markets.py handles any line unchanged.
CORNERS_LINES = (9.5, 10.5)


def _map_odds_response_to_payloads(row: dict) -> list[OddsPayload]:
    """Pure, network/DB-free mapping — one or more OddsPayloads per bookmaker, mirroring
    app/adapters/therundown.py:_map_event_to_odds_payloads's shape. Maps "Match Winner"
    (home/draw/away), "Double Chance", "Goals Over/Under" (GOALS_LINES only), and "Corners
    Over Under" (CORNERS_LINES only) — confirmed live (see CLAUDE.md) that all four are real
    bet types this endpoint returns, at least for Brasileirão (the only league with real odds
    coverage on this plan). The dozen other bet types (Asian handicap, per-half markets, ...)
    still aren't mapped — nothing downstream consumes them.

    Odds are already real decimals here — unlike TheRundown, no American-to-decimal
    conversion is needed (confirmed live, see CLAUDE.md).

    fixture_external_id is API-Football's own fixture id — the SAME id space as
    Fixture.external_id for any league whose fixtures also come from API-Football (every
    football league, currently). That means odds ingestion can match directly on
    Fixture.external_id instead of the fuzzy team-abbreviation-plus-kickoff-time join
    TheRundown-sourced odds need (see app/workers/ingest_odds.py:_resolve_fixture) — a real
    simplification unique to same-provider fixtures+odds, not something to generalise
    to TheRundown's payloads."""
    fixture_id = str(row["fixture"]["id"])
    updated_at_raw = row.get("update")
    updated_at = (
        datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
        if updated_at_raw
        else datetime.now(UTC)
    )

    payloads: list[OddsPayload] = []
    for bookmaker in row.get("bookmakers", []):
        bookmaker_name = bookmaker.get("name", "unknown")
        bets_by_name = {b.get("name"): b for b in bookmaker.get("bets", [])}

        match_winner = bets_by_name.get(MATCH_WINNER_BET_NAME)
        if match_winner is not None:
            odds_by_value = {v["value"]: v.get("odd") for v in match_winner.get("values", [])}
            home_odds = odds_by_value.get("Home")
            draw_odds = odds_by_value.get("Draw")
            away_odds = odds_by_value.get("Away")
            if home_odds is not None or away_odds is not None:
                payloads.append(
                    OddsPayload(
                        fixture_external_id=fixture_id,
                        bookmaker=bookmaker_name,
                        market="h2h",
                        home_odds=float(home_odds) if home_odds is not None else None,
                        draw_odds=float(draw_odds) if draw_odds is not None else None,
                        away_odds=float(away_odds) if away_odds is not None else None,
                        updated_at=updated_at,
                    )
                )

        double_chance = bets_by_name.get(DOUBLE_CHANCE_BET_NAME)
        if double_chance is not None:
            odds_by_value = {v["value"]: v.get("odd") for v in double_chance.get("values", [])}
            home_or_draw = odds_by_value.get("Home/Draw")
            away_or_draw = odds_by_value.get("Draw/Away")
            if home_or_draw is not None or away_or_draw is not None:
                payloads.append(
                    OddsPayload(
                        fixture_external_id=fixture_id,
                        bookmaker=bookmaker_name,
                        market="double_chance",
                        home_odds=float(home_or_draw) if home_or_draw is not None else None,
                        draw_odds=None,
                        away_odds=float(away_or_draw) if away_or_draw is not None else None,
                        updated_at=updated_at,
                    )
                )

        payloads.extend(
            _map_totals_bet(
                bets_by_name.get(GOALS_OVER_UNDER_BET_NAME),
                GOALS_LINES,
                "total",
                fixture_id,
                bookmaker_name,
                updated_at,
            )
        )
        payloads.extend(
            _map_totals_bet(
                bets_by_name.get(CORNERS_OVER_UNDER_BET_NAME),
                CORNERS_LINES,
                "corners_total",
                fixture_id,
                bookmaker_name,
                updated_at,
            )
        )
    return payloads


def _map_totals_bet(
    bet: dict | None,
    lines: tuple[float, ...],
    market: str,
    fixture_id: str,
    bookmaker_name: str,
    updated_at: datetime,
) -> list[OddsPayload]:
    """Shared by the Goals Over/Under and Corners Over Under mappings — both bets return a
    flat values list like [{"value": "Over 2.5", "odd": "2.00"}, {"value": "Under 2.5", ...}]
    covering many lines per bookmaker; only `lines` (GOALS_LINES/CORNERS_LINES) are kept."""
    if bet is None:
        return []
    odds_by_value = {v["value"]: v.get("odd") for v in bet.get("values", [])}
    payloads: list[OddsPayload] = []
    for target_line in lines:
        over_odds = odds_by_value.get(f"Over {target_line}")
        under_odds = odds_by_value.get(f"Under {target_line}")
        if over_odds is None and under_odds is None:
            continue
        payloads.append(
            OddsPayload(
                fixture_external_id=fixture_id,
                bookmaker=bookmaker_name,
                market=market,
                home_odds=None,
                draw_odds=None,
                away_odds=None,
                updated_at=updated_at,
                line=target_line,
                over_odds=float(over_odds) if over_odds is not None else None,
                under_odds=float(under_odds) if under_odds is not None else None,
            )
        )
    return payloads


def _map_injury_to_update(injury: dict, source: str = "api_football") -> InjuryUpdate:
    """API-Football's /injuries rows are fixture-scoped ("Missing Fixture") rather than a
    RotoWire-style rolling current-status feed — every row it returns is, by construction,
    a confirmed absence, so every mapped row is InjuryStatus.OUT. No GTD/doubtful signal
    exists in this feed (see CLAUDE.md)."""
    player = injury["player"]
    team = injury["team"]
    return InjuryUpdate(
        player_external_id=str(player["id"]),
        team_external_id=str(team["id"]),
        player_name=player["name"],
        status="OUT",
        return_date=None,
        salary_rank=None,
        source=source,
    )


class APIFootballAdapter(DataSourceAdapter):
    """Football fixtures, team stats, injuries, and (per-league) odds — real Pro-tier
    implementation (confirmed live: 7500 req/day, active until 2026-08-29, see CLAUDE.md).

    fetch_odds is real but genuinely partial, not a universal TheRundown replacement:
    confirmed live via each league's /leagues coverage.odds flag that only Brasileirão has
    real odds coverage on this plan (13 real bookmakers incl. Bet365/Pinnacle/Betfair — richer
    than TheRundown's 3-of-~15-unmasked situation) — all 5 European leagues report
    coverage.odds=False, same as originally found pre-Pro-upgrade. The two providers are
    complementary per league, not redundant: TheRundown remains the only real odds source for
    the 5 European leagues (which it covers and API-Football doesn't); API-Football is now
    the only real odds source for Brasileirão (which TheRundown doesn't cover at all). See
    app/adapters/factory.py:get_odds_adapters and app/workers/ingest_odds.py for how both are
    queried and merged.
    """

    def __init__(self) -> None:
        self._api_key = get_settings().api_football_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers={"x-apisports-key": self._api_key}, timeout=15.0
        )

    async def fetch_odds(
        self,
        sport: str,
        league: str,
        days_ahead: int,
        dates: list[date] | None = None,
    ) -> list[OddsPayload]:
        """Real for leagues API-Football covers odds for (currently only Brasileirão —
        see class docstring); genuinely empty (not an error) for a recognised league this
        plan has zero odds coverage for (confirmed live: EPL returns results=0, not a 4xx) —
        same "no line from this book" graceful-miss philosophy as TheRundown's masked-
        sentinel handling, just at the whole-league level instead of per-bookmaker.

        Queried per real upcoming fixture date (mirrors fetch_injuries's own
        dates-with-real-fixtures optimization) rather than blindly for every date in
        days_ahead, to avoid empty-date calls once more leagues gain odds coverage."""
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        payloads: list[OddsPayload] = []

        async with self._client() as client:
            season = await resolve_current_season(client, league)
            fixtures_response = await client.get(
                "/fixtures",
                params={
                    "league": league_id,
                    "season": season,
                    "from": now.date().isoformat(),
                    "to": (now + timedelta(days=days_ahead)).date().isoformat(),
                },
            )
            fixtures_response.raise_for_status()
            dates: set[date] = {
                datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).date()
                for fx in _api_response(fixtures_response).get("response", [])
            }

            for fixture_date in dates:
                odds_response = await client.get(
                    "/odds",
                    params={
                        "league": league_id,
                        "season": season,
                        "date": fixture_date.isoformat(),
                    },
                )
                odds_response.raise_for_status()
                for row in _api_response(odds_response).get("response", []):
                    payloads.extend(_map_odds_response_to_payloads(row))

        return payloads

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        async with self._client() as client:
            params = {
                "league": league_id,
                "season": await resolve_current_season(client, league),
                "from": (now - timedelta(days=days_back)).date().isoformat(),
                "to": (now + timedelta(days=days_ahead)).date().isoformat(),
            }
            response = await client.get("/fixtures", params=params)
            response.raise_for_status()
        fixtures = _api_response(response).get("response", [])
        return [_map_fixture_to_payload(fx, league) for fx in fixtures]

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        if league is None:
            raise ValueError("APIFootballAdapter.fetch_team_stats requires league (see base.py)")
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        async with self._client() as client:
            params = {
                "league": league_id,
                "season": await resolve_current_season(client, league),
                "team": team_id,
            }
            response = await client.get("/teams/statistics", params=params)
            response.raise_for_status()
        stats = _api_response(response).get("response", {})
        return _compute_team_stats(team_id, stats)

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """Bulk per-league-per-date query (confirmed live: /injuries?league=X&season=Y&date=Z
        works without needing fixture IDs upfront) — matches the ABC's existing bulk-fetch
        shape rather than iterating fixture IDs one at a time. Looks at every date with a real
        upcoming fixture across every league in LEAGUE_IDS in the next INJURY_LOOKAHEAD_DAYS
        days (season computed per-league — see _current_football_season); genuinely
        returns nothing for dates too far out (confirmed live: real injury news doesn't exist
        that early — see CLAUDE.md), which is correct, not a bug."""
        now = datetime.now(UTC)
        updates: list[InjuryUpdate] = []

        async with self._client() as client:
            for league_slug, league_id in LEAGUE_IDS.items():
                season = await resolve_current_season(client, league_slug)
                fixtures_response = await client.get(
                    "/fixtures",
                    params={
                        "league": league_id,
                        "season": season,
                        "from": now.date().isoformat(),
                        "to": (now + timedelta(days=INJURY_LOOKAHEAD_DAYS)).date().isoformat(),
                    },
                )
                fixtures_response.raise_for_status()
                dates: set[date] = {
                    datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).date()
                    for fx in _api_response(fixtures_response).get("response", [])
                }

                for fixture_date in dates:
                    injuries_response = await client.get(
                        "/injuries",
                        params={
                            "league": league_id,
                            "season": season,
                            "date": fixture_date.isoformat(),
                        },
                    )
                    injuries_response.raise_for_status()
                    for injury in _api_response(injuries_response).get("response", []):
                        updates.append(_map_injury_to_update(injury))

        return updates


# Requested from the provider BEFORE any competition filter is applied, which is why it is 20
# rather than the 10 it was. Measured over 45 real upcoming fixtures: filtering a 10-meeting
# page leaves a mean of 5.42 qualifying meetings, a 40-meeting page 5.98, and the wider request
# recovers ZERO fixtures from empty. So 10 was very nearly enough and 20 captures the rest at
# the same one API call — the provider caps this page around 34 regardless.
H2H_LOOKBACK_MEETINGS = 20

# The first day of the history the model was TRAINED on. ml/training/collect_football_data.py
# collects seasons 2021-2025, so _h2h_stats -- the training-side counterpart of the functions
# below -- can never see a meeting older than this. Serving had no such bound and was reaching
# back to 2013.
H2H_TRAINING_WINDOW_START = date(2021, 7, 1)


@dataclass(frozen=True)
class H2HStats:
    win_rate_home: float
    avg_goals_scored_home: float
    avg_goals_allowed_home: float


async def _fetch_h2h_meetings(
    home_external_id: str,
    away_external_id: str,
    last: int = H2H_LOOKBACK_MEETINGS,
    league_external_id: int | None = None,
    since: date | None = None,
) -> list[dict]:
    """Completed meetings between two teams, optionally narrowed to ONE competition and a
    start date.

    BOTH FILTERS ARE OPTIONAL AND OFF BY DEFAULT, deliberately: fetch_h2h_detail (the
    display-only panel on fixture detail) passes neither and is unchanged by this, while the
    two MODEL-FEATURE functions below pass both. Filtering the panel is a separate product
    decision with the opposite pull -- it would empty a card rather than enrich it.

    WHY THE MODEL PATH NEEDS THEM. /fixtures/headtohead returns every competition the two
    clubs have ever met in, at any age. Training does not: _h2h_stats reads the collected game
    log, which is gathered PER LEAGUE for 2021-2025 only. So three features
    (h2h_win_rate_home, h2h_avg_goals_scored_home, h2h_avg_goals_allowed_home) were learned
    from same-league meetings and served from a mixture that included friendlies and cup ties.

    Measured over 153 real upcoming fixtures: 106 (69%) had at least one of those three
    features move, mean |delta win_rate| 0.103, max 0.800. The unfiltered meetings were 47
    friendlies, 25 Championship, 21 FA Cup, 20 J2, 18 League One, 11 League Cup.

    THE 13 FIXTURES THAT LOSE H2H ENTIRELY ARE ALL PROMOTED CLUBS -- Coventry, Hull City,
    Ipswich, Racing Santander, Deportivo, Malaga, Wieczysta Krakow. That is the argument FOR
    this rather than against it: a promoted club has no same-league history, so training sees
    NaN, and serving was handing the model a Championship-derived number in its place.
    """
    api_key = get_settings().api_football_key
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    ) as client:
        response = await client.get(
            "/fixtures/headtohead",
            params={"h2h": f"{home_external_id}-{away_external_id}", "last": last},
        )
        response.raise_for_status()
    meetings = _api_response(response).get("response", [])
    completed = [fx for fx in meetings if fx["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
    if league_external_id is not None:
        completed = [fx for fx in completed if fx["league"]["id"] == league_external_id]
    if since is not None:
        # ISO-8601 dates sort lexicographically, so no parsing is needed to compare them and a
        # malformed or absent date cannot raise here.
        cutoff = since.isoformat()
        completed = [fx for fx in completed if (fx["fixture"].get("date") or "") >= cutoff]
    return completed


def _goals_from_home_side_perspective(fx: dict, home_external_id: str) -> tuple[int, int] | None:
    """Returns (goals_scored_by_home_external_id, goals_conceded), regardless of which side of
    THIS particular past meeting home_external_id played on — a team's own H2H scoring record
    against an opponent shouldn't silently flip depending on historical home/away assignment."""
    home_goals = fx["goals"]["home"]
    away_goals = fx["goals"]["away"]
    if home_goals is None or away_goals is None:
        return None
    if str(fx["teams"]["home"]["id"]) == home_external_id:
        return home_goals, away_goals
    return away_goals, home_goals


async def fetch_h2h_win_rate(
    home_external_id: str,
    away_external_id: str,
    league_external_id: int | None = None,
) -> float | None:
    """Football-specific helper, not part of the DataSourceAdapter ABC — mirrors
    app/adapters/balldontlie.py:fetch_h2h_win_rate's role (fixture-specific, needs both
    teams, doesn't fit fetch_team_stats(team_id)'s shape). Unlike NBA, API-Football has a
    dedicated head-to-head endpoint (/fixtures/headtohead) rather than requiring a manual
    search through one team's own fixture history — simpler and a real, direct provider
    feature, not a workaround.

    Pass league_external_id to match what training saw — see _fetch_h2h_meetings."""
    completed = await _fetch_h2h_meetings(
        home_external_id,
        away_external_id,
        league_external_id=league_external_id,
        since=H2H_TRAINING_WINDOW_START if league_external_id is not None else None,
    )
    if not completed:
        return None

    def home_side_won(fx: dict) -> bool:
        scored = _goals_from_home_side_perspective(fx, home_external_id)
        if scored is None:
            return False
        return scored[0] > scored[1]

    return sum(1 for fx in completed if home_side_won(fx)) / len(completed)


async def fetch_h2h_stats(
    home_external_id: str,
    away_external_id: str,
    league_external_id: int | None = None,
) -> H2HStats | None:
    """Richer H2H than fetch_h2h_win_rate: the same /fixtures/headtohead response already
    carries real goals per meeting, so average goals scored/allowed vs this specific opponent
    (not just win rate) comes for free from a call this codebase was already making.

    Pass league_external_id to match what training saw — see _fetch_h2h_meetings. It stays
    OPTIONAL so an omitted league (a fixture whose slug is not in LEAGUE_IDS) degrades to the
    old behaviour rather than silently dropping the three features altogether."""
    completed = await _fetch_h2h_meetings(
        home_external_id,
        away_external_id,
        league_external_id=league_external_id,
        since=H2H_TRAINING_WINDOW_START if league_external_id is not None else None,
    )
    if not completed:
        return None

    wins = 0
    scored_total = 0
    allowed_total = 0
    counted = 0
    for fx in completed:
        goals = _goals_from_home_side_perspective(fx, home_external_id)
        if goals is None:
            continue
        scored, allowed = goals
        scored_total += scored
        allowed_total += allowed
        counted += 1
        if scored > allowed:
            wins += 1

    if counted == 0:
        return None
    return H2HStats(
        win_rate_home=wins / counted,
        avg_goals_scored_home=scored_total / counted,
        avg_goals_allowed_home=allowed_total / counted,
    )


H2H_DETAIL_MEETINGS = 5  # the fixture-detail H2H panel's own window — separate from
# H2H_LOOKBACK_MEETINGS (the model-feature functions above, untouched) per direct user request
# ("Average X in the last 5 meetings") — deliberately NOT shared with fetch_h2h_stats/
# fetch_h2h_win_rate so this display-only change can never silently alter model training.


@dataclass(frozen=True)
class MatchStats:
    """Real per-team stats for ONE match, via /fixtures/statistics — the same endpoint
    fetch_corner_stats already calls for the corners settlement grading, this time keeping
    every field the H2H averages panel needs. Any field can be None on its own (not every
    historical fixture's /fixtures/statistics call returns every stat type — a real, accepted
    gap, never fabricated)."""

    corners: int | None
    shots: int | None
    shots_on_goal: int | None
    possession_pct: float | None


_MATCH_STAT_TYPE_KEYS = {
    "Corner Kicks": "corners",
    "Total Shots": "shots",
    "Shots on Goal": "shots_on_goal",
}


def _parse_match_stats_block(team_block: dict) -> MatchStats:
    """Pure parsing for one team's /fixtures/statistics block — confirmed live (see CLAUDE.md)
    that this real response includes far more than corners: Total Shots, Shots on Goal, Ball
    Possession (a string like "27%", not a number) among ~19 real stat types per team."""
    values: dict[str, float | None] = {
        "corners": None,
        "shots": None,
        "shots_on_goal": None,
        "possession_pct": None,
    }
    for stat in team_block.get("statistics", []):
        raw = stat.get("value")
        if raw is None:
            continue
        stat_type = stat.get("type")
        if stat_type in _MATCH_STAT_TYPE_KEYS:
            values[_MATCH_STAT_TYPE_KEYS[stat_type]] = int(raw)
        elif stat_type == "Ball Possession":
            values["possession_pct"] = float(str(raw).rstrip("%"))
    return MatchStats(**values)


async def fetch_match_stats(fixture_external_id: str) -> dict[str, MatchStats]:
    """Real per-team match stats (corners, total shots, shots on goal, ball possession %) for
    ONE already-completed fixture — {team_external_id: MatchStats}. fetch_corner_stats below is
    now a thin wrapper over this, kept as its own function since its one caller (settlement-time
    corners-pick grading) only ever needed corners, not the fuller shape. A team missing from
    the response (real, not every historical fixture's /fixtures/statistics call returns a
    value) simply has no entry, never a fabricated value."""
    api_key = get_settings().api_football_key
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    ) as client:
        response = await client.get("/fixtures/statistics", params={"fixture": fixture_external_id})
        response.raise_for_status()

    return {
        str(team_block["team"]["id"]): _parse_match_stats_block(team_block)
        for team_block in _api_response(response).get("response", [])
    }


@dataclass(frozen=True)
class H2HDetail:
    """Richer than H2HStats (which only exists for the model's own feature vector) — this is
    for a real display panel (GET /fixtures/{id}'s Head-to-Head section): the last
    H2H_DETAIL_MEETINGS real meetings' average goals/corners/shots/shots-on-goal/possession per
    side, replacing an earlier version of this panel that just listed individual match scores
    (per direct user follow-up: "important stats that will give users confidence on the
    prediction" instead of raw scores). home_wins/draws/away_wins and every avg_*_home/away
    field are relative to the CURRENT fixture's home/away assignment, not each historical
    meeting's own (same reasoning as _goals_from_home_side_perspective) — a team's H2H record
    shouldn't flip depending on which side it happened to be on in a past meeting. Every avg_*
    field is None (never a fabricated value) when none of the counted meetings had a real value
    for that specific stat."""

    meetings_count: int
    home_wins: int
    draws: int
    away_wins: int
    avg_goals_home: float | None
    avg_goals_away: float | None
    avg_corners_home: float | None
    avg_corners_away: float | None
    avg_shots_home: float | None
    avg_shots_away: float | None
    avg_shots_on_goal_home: float | None
    avg_shots_on_goal_away: float | None
    avg_possession_home: float | None
    avg_possession_away: float | None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _parse_h2h_detail(
    meetings: list[dict],
    home_external_id: str,
    match_stats_by_fixture: dict[str, dict[str, MatchStats]],
) -> H2HDetail | None:
    """Pure parsing, separated from the live _fetch_h2h_meetings/fetch_match_stats calls so
    this is directly testable against recorded sample responses — mirrors every other
    _map_*/_parse_* helper in this file. `meetings` is assumed already-completed-only (i.e.
    _fetch_h2h_meetings's own return shape) — no re-filtering here. match_stats_by_fixture is
    keyed by the meeting's own fixture id (as a string) to whichever MatchStats
    fetch_match_stats managed to fetch for it — an empty dict for a fixture whose stats call
    failed or returned nothing degrades every avg_* for that meeting to "not counted", never a
    fabricated value."""
    home_wins = draws = away_wins = 0
    goals_home: list[float] = []
    goals_away: list[float] = []
    corners_home: list[float] = []
    corners_away: list[float] = []
    shots_home: list[float] = []
    shots_away: list[float] = []
    shots_on_goal_home: list[float] = []
    shots_on_goal_away: list[float] = []
    possession_home: list[float] = []
    possession_away: list[float] = []

    for fx in meetings:
        goals = _goals_from_home_side_perspective(fx, home_external_id)
        if goals is None:
            continue
        scored, allowed = goals
        goals_home.append(scored)
        goals_away.append(allowed)
        if scored > allowed:
            home_wins += 1
        elif scored == allowed:
            draws += 1
        else:
            away_wins += 1

        # match_stats is keyed by the PROVIDER'S OWN team id directly, not home/away, so no
        # perspective flip is needed here the way goals needed — just look up whichever
        # external id is home_external_id in THIS meeting vs. the other side.
        meeting_home_id = str(fx["teams"]["home"]["id"])
        meeting_away_id = str(fx["teams"]["away"]["id"])
        away_external_id_here = (
            meeting_away_id if meeting_home_id == home_external_id else meeting_home_id
        )
        stats_by_team = match_stats_by_fixture.get(str(fx["fixture"]["id"]), {})
        home_stats = stats_by_team.get(home_external_id)
        away_stats = stats_by_team.get(away_external_id_here)
        if home_stats:
            if home_stats.corners is not None:
                corners_home.append(home_stats.corners)
            if home_stats.shots is not None:
                shots_home.append(home_stats.shots)
            if home_stats.shots_on_goal is not None:
                shots_on_goal_home.append(home_stats.shots_on_goal)
            if home_stats.possession_pct is not None:
                possession_home.append(home_stats.possession_pct)
        if away_stats:
            if away_stats.corners is not None:
                corners_away.append(away_stats.corners)
            if away_stats.shots is not None:
                shots_away.append(away_stats.shots)
            if away_stats.shots_on_goal is not None:
                shots_on_goal_away.append(away_stats.shots_on_goal)
            if away_stats.possession_pct is not None:
                possession_away.append(away_stats.possession_pct)

    counted = home_wins + draws + away_wins
    if counted == 0:
        return None
    return H2HDetail(
        meetings_count=counted,
        home_wins=home_wins,
        draws=draws,
        away_wins=away_wins,
        avg_goals_home=_average(goals_home),
        avg_goals_away=_average(goals_away),
        avg_corners_home=_average(corners_home),
        avg_corners_away=_average(corners_away),
        avg_shots_home=_average(shots_home),
        avg_shots_away=_average(shots_away),
        avg_shots_on_goal_home=_average(shots_on_goal_home),
        avg_shots_on_goal_away=_average(shots_on_goal_away),
        avg_possession_home=_average(possession_home),
        avg_possession_away=_average(possession_away),
    )


async def fetch_h2h_detail(home_external_id: str, away_external_id: str) -> H2HDetail | None:
    """Real head-to-head data for a fixture detail screen's own H2H panel — same
    /fixtures/headtohead call this codebase already makes for the model's H2H features
    (fetch_h2h_stats), over the last H2H_DETAIL_MEETINGS (5) meetings. Also fetches
    fetch_match_stats once PER meeting (up to 5 extra real API calls, on top of the 1 for
    headtohead itself) to get corners/shots/shots-on-goal/possession — a real, meaningful cost
    increase over the goals-only version of this panel, but still a per-viewed-fixture cost
    (see app/fixtures/router.py:get_fixture), not a per-ingested-fixture one, and each call
    degrades independently (an HTTPError for one meeting's stats doesn't lose the others, or
    the win/draw/loss record and goals averages, which need no extra call at all)."""
    meetings = await _fetch_h2h_meetings(
        home_external_id, away_external_id, last=H2H_DETAIL_MEETINGS
    )
    if not meetings:
        return None

    match_stats_by_fixture: dict[str, dict[str, MatchStats]] = {}
    for fx in meetings:
        fixture_id = str(fx["fixture"]["id"])
        try:
            match_stats_by_fixture[fixture_id] = await fetch_match_stats(fixture_id)
        except httpx.HTTPError:
            match_stats_by_fixture[fixture_id] = {}

    return _parse_h2h_detail(meetings, home_external_id, match_stats_by_fixture)


async def fetch_lineup_presence(fixture_external_id: str) -> dict[str, set[str]]:
    """Real box-score/lineup presence for ONE already-completed fixture — {team_external_id:
    {lowercased player names who actually played}}. One-off, real-time equivalent of
    ml/training/collect_football_data.py:collect_lineups for a single fixture, used ONLY by
    app/workers/backfill_predictions.py's retrodiction path when a completed fixture is newer
    than the cached training parquet's collection snapshot (so its lineup was never collected
    in bulk). Never call this pre-game — see
    app/models_ml/historical_key_players.py's module docstring for why lineup presence is only
    ever safe to use once a fixture's outcome is already known."""
    api_key = get_settings().api_football_key
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    ) as client:
        response = await client.get("/fixtures/players", params={"fixture": fixture_external_id})
        response.raise_for_status()

    by_team: dict[str, set[str]] = {}
    for team_block in _api_response(response).get("response", []):
        team_id = str(team_block["team"]["id"])
        names = set()
        for player_row in team_block.get("players", []):
            stats = player_row.get("statistics", [{}])[0]
            minutes = stats.get("games", {}).get("minutes")
            if minutes:
                names.add(player_row["player"]["name"].lower())
        by_team[team_id] = names
    return by_team


async def fetch_corner_stats(fixture_external_id: str) -> dict[str, int]:
    """Real per-team corner-kick count for ONE already-completed fixture — {team_external_id:
    corners}. Called exactly once per fixture, at settlement time
    (app/workers/ingest_fixtures.py:_maybe_settle_outcome), so the Over/Under corners market can
    show a real win/loss verdict instead of staying permanently unverifiable. Now a thin wrapper
    over fetch_match_stats (kept as its own function since this is the only caller that only
    ever needed corners, not the fuller shots/possession shape) — same real behavior: a team
    missing a real corners value simply has no entry here, never a fabricated 0."""
    stats_by_team = await fetch_match_stats(fixture_external_id)
    return {
        team_id: stats.corners
        for team_id, stats in stats_by_team.items()
        if stats.corners is not None
    }
