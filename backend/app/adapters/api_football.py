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
}

# Leagues whose season runs on the calendar year (Jan-Dec) rather than the European Aug-May
# convention — confirmed live for Brasileirão: current season "2026" runs 2026-01-28 to
# 2026-12-02. Using the European convention here would compute the WRONG season year for
# most of the year (e.g. any month before July would look back a full year too far). MLS
# (2026 season: 2026-02-21 to 2026-11-08) and the Chinese Super League (2026 season:
# 2026-03-06 to 2026-11-08) are the same calendar-year shape, confirmed live the same way —
# Scottish Premiership stays out of this set, its 2026 season runs 2026-07-31 to 2027-04-10,
# the same Aug-May convention as the 5 original European leagues.
CALENDAR_YEAR_SEASON_LEAGUES = {"brasileirao", "mls", "csl"}

INJURY_LOOKAHEAD_DAYS = 3  # how far ahead fetch_injuries looks for fixtures to check dates for


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
    return now.year if now.month >= 7 else now.year - 1


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
    3/1/0-points-per-game average — the actual convention form_pts_5's name always implied
    but NBA's own win/loss-only implementation never used (NBA has no draws)."""
    if not form:
        return None
    points = {"W": 3, "D": 1, "L": 0}
    values = [points[c] for c in form if c in points]
    if not values:
        return None
    return sum(values) / len(values)


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
        season = _current_football_season(league, now)
        payloads: list[OddsPayload] = []

        async with self._client() as client:
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
                for fx in fixtures_response.json().get("response", [])
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
                for row in odds_response.json().get("response", []):
                    payloads.extend(_map_odds_response_to_payloads(row))

        return payloads

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        now = datetime.now(UTC)
        params = {
            "league": league_id,
            "season": _current_football_season(league, now),
            "from": (now - timedelta(days=days_back)).date().isoformat(),
            "to": (now + timedelta(days=days_ahead)).date().isoformat(),
        }
        async with self._client() as client:
            response = await client.get("/fixtures", params=params)
            response.raise_for_status()
        fixtures = response.json().get("response", [])
        return [_map_fixture_to_payload(fx, league) for fx in fixtures]

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        if league is None:
            raise ValueError("APIFootballAdapter.fetch_team_stats requires league (see base.py)")
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Football league id mapping for league={league!r}")

        params = {
            "league": league_id,
            "season": _current_football_season(league),
            "team": team_id,
        }
        async with self._client() as client:
            response = await client.get("/teams/statistics", params=params)
            response.raise_for_status()
        stats = response.json().get("response", {})
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
                season = _current_football_season(league_slug, now)
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
                    for fx in fixtures_response.json().get("response", [])
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
                    for injury in injuries_response.json().get("response", []):
                        updates.append(_map_injury_to_update(injury))

        return updates


H2H_LOOKBACK_MEETINGS = 10  # API-Football's own default page size for this endpoint


@dataclass(frozen=True)
class H2HStats:
    win_rate_home: float
    avg_goals_scored_home: float
    avg_goals_allowed_home: float


async def _fetch_h2h_meetings(
    home_external_id: str, away_external_id: str, last: int = H2H_LOOKBACK_MEETINGS
) -> list[dict]:
    api_key = get_settings().api_football_key
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": api_key}, timeout=15.0
    ) as client:
        response = await client.get(
            "/fixtures/headtohead",
            params={"h2h": f"{home_external_id}-{away_external_id}", "last": last},
        )
        response.raise_for_status()
    meetings = response.json().get("response", [])
    return [fx for fx in meetings if fx["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]


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


async def fetch_h2h_win_rate(home_external_id: str, away_external_id: str) -> float | None:
    """Football-specific helper, not part of the DataSourceAdapter ABC — mirrors
    app/adapters/balldontlie.py:fetch_h2h_win_rate's role (fixture-specific, needs both
    teams, doesn't fit fetch_team_stats(team_id)'s shape). Unlike NBA, API-Football has a
    dedicated head-to-head endpoint (/fixtures/headtohead) rather than requiring a manual
    search through one team's own fixture history — simpler and a real, direct provider
    feature, not a workaround."""
    completed = await _fetch_h2h_meetings(home_external_id, away_external_id)
    if not completed:
        return None

    def home_side_won(fx: dict) -> bool:
        scored = _goals_from_home_side_perspective(fx, home_external_id)
        if scored is None:
            return False
        return scored[0] > scored[1]

    return sum(1 for fx in completed if home_side_won(fx)) / len(completed)


async def fetch_h2h_stats(home_external_id: str, away_external_id: str) -> H2HStats | None:
    """Richer H2H than fetch_h2h_win_rate: the same /fixtures/headtohead response already
    carries real goals per meeting, so average goals scored/allowed vs this specific opponent
    (not just win rate) comes for free from a call this codebase was already making."""
    completed = await _fetch_h2h_meetings(home_external_id, away_external_id)
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
        for team_block in response.json().get("response", [])
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
    for team_block in response.json().get("response", []):
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
