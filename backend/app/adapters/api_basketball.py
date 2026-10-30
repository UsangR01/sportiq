"""European basketball, from API-Basketball (api-sports.io).

WHY A SECOND BASKETBALL ADAPTER. BallDontLie serves the NBA and the WNBA and has no European
coverage at all. These seven competitions come from API-Sports, which answers to the SAME key
the football adapter already uses -- so this is a new adapter, not a new vendor.

WHY THESE LEAGUES ARE SERVED BY THE NBA MODEL. One model serves a whole sport here, and that bet
was MEASURED before any of this was written rather than assumed. ml/training/
measure_basketball_transfer.py scored the active NBA artefact against 1,926 held-out European
games with criteria pre-registered in the file:

    accuracy 0.6547 vs always-home 0.6121 (+4.26pp)   Brier 0.2211 vs 0.2375   ECE 0.0303

All three criteria passed. It works because the features that carry the signal are scale-free --
win rates, rest days, head-to-head -- and the two point-differential features are differences
between two teams in the SAME league, so a league-wide scoring level largely cancels. That
matters because European basketball scores 27.4% fewer points than the NBA (more than 40-minute
FIBA games versus 48-minute NBA games explains) with 5.2pp more home advantage.

So these leagues sit under Sport(slug="nba") alongside the WNBA, and the whole product -- the
model, the sport tab, the base rates -- applies unchanged.

=============================================================================================
FOUR THINGS MEASURED AGAINST THE REAL API, each of which would have been a silent bug
=============================================================================================

1. THE ODDS MARKET IS "Home/Away", NOT "3Way Result", and the difference is money. Basketball
   books offer both: "3Way Result" prices REGULATION TIME and carries a Draw at 11-16, while
   "Home/Away" is the moneyline including overtime -- which is what our two-outcome model
   predicts. Measured on one real fixture, the same bookmakers price home differently:

       Marathon Bet   3Way home 4.55   Home/Away home 4.33
       1xBet          3Way home 4.55   Home/Away home 4.26
       Betano         3Way home 4.15   Home/Away home 3.65

   Reading 3Way would hand us a draw price for a market the model does not predict AND
   systematically overstate home value by ~5%, inflating EV on every basketball card. It is also
   the thinner market: 8 of 8 sampled bookmakers offer Home/Away, only 3 offer 3Way.

2. "AOT" (AFTER OVER TIME) IS A FINISHED GAME, and it is 3.6-6.6% of a season. Mapping only
   "FT" would leave roughly one game in twenty permanently unsettled -- the _map_status bug
   class CLAUDE.md records three separate times. Status codes were ENUMERATED from three full
   days across every league rather than taken from documentation: FT, NS, AOT, CANC, POST, Q4.

3. THE SEASON LABEL HAS TWO FORMATS IN ONE PROVIDER. Domestic leagues use the STRING
   "2026-2027"; the Euroleague uses the INTEGER 2026. Worse, NO season carries `current: true`
   on this API, so api_football.py's resolve_current_season trick -- prefer the provider's own
   statement to a hardcoded convention -- has nothing to read here. The convention below is
   therefore a GUESS in exactly the way that has been wrong three times for football, which is
   why _season_for is one function with one test rather than an inline expression.

4. THE /leagues SEASONS ARRAY IS UNSORTED AND HAS NO CURRENT FLAG, so seasons[-1] is not the
   newest. Reading it by position reported Greece's latest season as 2016-2017 and Spain's as
   2020-2021, both wrong, before this was caught. Nothing here reads it by position.

EXTERNAL IDS ARE PREFIXED "ab:", AND THE PREFIX IS PER-PROVIDER RATHER THAN PER-LEAGUE.
Both halves matter. The prefix exists because these leagues share Sport(slug="nba") with the
NBA and WNBA while Team/Fixture uniqueness is (sport_id, external_id) -- an unprefixed
API-Basketball id could collide with a BallDontLie one and silently merge two different clubs,
the same hazard that made WNBA ids "wnba:"-prefixed. But it is deliberately NOT per-league:
API-Basketball uses ONE global team namespace, so Valencia is 2341 in both the ACB and the
Euroleague, and a per-league prefix would split one club into two rows with two disjoint form
and Elo histories. One club, one row, one continuous history.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from app.adapters.base import (
    DataSourceAdapter,
    FixturePayload,
    H2HMeeting,
    H2HPanel,
    H2HPanelStat,
    InjuryUpdate,
    OddsPayload,
    StandingRow,
    TeamStats,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://v1.basketball.api-sports.io"

#: Prefix on every team and fixture id this adapter produces. See the module docstring.
ID_PREFIX = "ab:"

# League ids confirmed live via /leagues on 2026-10-04. Every one carries odds, standings and
# team/player statistics coverage on its current season, and 11-19 seasons of history.
BASKETBALL_LEAGUE_IDS: dict[str, int] = {
    "acb": 117,  # Spain, Liga ACB
    "lnb": 2,  # France, LNB Betclic Elite
    "bbl": 40,  # Germany, Basketball Bundesliga
    "lega_a": 52,  # Italy, Lega Basket Serie A
    "greek_bl": 45,  # Greece, Basket League
    "turkish_bsl": 104,  # Turkey, Basketbol Super Ligi
    "euroleague": 120,  # pan-European; its clubs also play their domestic leagues
}


# STANDINGS ONLY -- deliberately NOT merged into BASKETBALL_LEAGUE_IDS, which drives adapter
# ROUTING (factory.py, the H2H dispatch, nba_features). Adding the NBA there would send its
# fixtures, odds and team stats to API-Basketball instead of BallDontLie, which owns them.
#
# The NBA and the WNBA appear here because BallDontLie CANNOT serve a table at any tier we
# hold: /standings returns 401 for both, measured, as do /stats and /season_averages. So the
# table comes from the provider that has it while everything else keeps its existing source.
STANDINGS_LEAGUE_IDS: dict[str, int] = {**BASKETBALL_LEAGUE_IDS, "nba": 12, "wnba": 13}

# Leagues whose season API-Basketball labels with a bare YEAR rather than a span. Checked per
# league against the provider's own season windows, never inferred -- that assumption is exactly
# what Brasileirao and the J1 League each broke for football.
# The WNBA is here for a DIFFERENT reason from the EuroLeague and it matters: it runs
# May-September inside ONE calendar year, so the span label is not merely a formatting
# mismatch, it names the wrong season. Measured: season "2026-2027" returns 0 rows, season
# "2026" returns 30 -- and a wrong season does not raise, it returns HTTP 200 with results=0.
YEAR_SEASON_LEAGUES = {"euroleague", "wnba"}

# Month from which a new autumn-spring season is considered to have started. European basketball
# tips off mid-September (measured: BBL 18 Sep, Euroleague 24 Sep, ACB/Lega A 26 Sep, Greece
# 3 Oct) and runs to May, so September is inside the NEW season and August is not.
SEASON_START_MONTH = 9

_FINISHED_STATUSES = {"FT", "AOT"}
_SCHEDULED_STATUSES = {"NS"}
# One shared bucket for every not-being-played reason, matching FixtureStatus.POSTPONED's own
# documented scope for football rather than modelling each separately.
_POSTPONED_STATUSES = {"POST", "CANC", "SUSP", "ABD", "AWD", "WO", "INTR"}
# Q4 was the only in-play code seen in a three-day sample; the rest are included because a code
# that only appears mid-game is precisely the one a sample taken at the wrong hour will miss.
_LIVE_STATUSES = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT"}

#: The two-way moneyline. NOT "3Way Result" -- see the module docstring.
MONEYLINE_MARKET = "Home/Away"

ODDS_LOOKAHEAD_DAYS = 3


class APIBasketballQuotaExceeded(httpx.HTTPError):
    """The plan's daily allowance is spent, or the season is outside it.

    Subclasses httpx.HTTPError for the same reason APIFootballQuotaExceeded does: every ingest
    worker already isolates one league from the rest by catching httpx.HTTPError, so a spent
    allowance is logged and skipped like any other provider failure instead of taking down the
    whole run -- which is what a bare RuntimeError did to tennis once.
    """


def _api_response(response: httpx.Response) -> dict:
    """Parse the body, raising if API-Sports reported an error inside an HTTP 200.

    This provider signals a refusal -- spent quota, or a season the plan does not cover -- with
    status 200 plus a populated `errors` object. Returning that as data is how "Free plans do
    not have access to this season" would otherwise read as "this league has no games".
    """
    body = response.json()
    errors = body.get("errors")
    # An empty list is the success shape; a populated dict is the refusal shape.
    if errors:
        text = str(errors)
        if "plan" in text.lower() or "limit" in text.lower():
            raise APIBasketballQuotaExceeded(f"API-Basketball refused the request: {errors}")
        raise httpx.HTTPError(f"API-Basketball error: {errors}")
    return body


def _season_for(league: str, now: datetime | None = None) -> str | int:
    """The season label this provider expects, in the format it expects for THIS league.

    Two formats in one API (see the module docstring), and no `current: true` flag anywhere to
    check the answer against -- so unlike football, which can ask the provider, this is a rule.
    It is kept in one place with its own test because a wrong season here does not raise: it
    returns HTTP 200 with results=0, indistinguishable from a league with no games scheduled.
    That exact failure hid the J1 League's entire fixture list for days.
    """
    now = now or datetime.now(UTC)
    start_year = now.year if now.month >= SEASON_START_MONTH else now.year - 1
    if league in YEAR_SEASON_LEAGUES:
        return start_year
    return f"{start_year}-{start_year + 1}"


def _map_status(short: str | None) -> str:
    """Provider status -> FixtureStatus value.

    UNKNOWN CODES FALL BACK TO "scheduled", NOT "live". The football adapter's original
    "anything else is live" default is what put a LIVE badge and a market prediction on four
    genuinely postponed fixtures. A fixture wrongly called scheduled is corrected by the next
    poll; one wrongly called live shows a game that is not happening.
    """
    if short in _FINISHED_STATUSES:
        return "completed"
    if short in _LIVE_STATUSES:
        return "live"
    if short in _POSTPONED_STATUSES:
        return "postponed"
    if short not in _SCHEDULED_STATUSES:
        logger.warning("API-Basketball returned an unmapped game status %r", short)
    return "scheduled"


def _score(side: dict | None) -> int | None:
    return (side or {}).get("total")


def _kickoff(game: dict) -> datetime:
    return datetime.fromisoformat(str(game["date"]).replace("Z", "+00:00")).astimezone(UTC)


class APIBasketballAdapter(DataSourceAdapter):
    """Fixtures, team stats and odds for the European basketball competitions above."""

    def __init__(self) -> None:
        self._settings = get_settings()

    @contextlib.asynccontextmanager
    async def _client(self):
        key = self._settings.api_football_key
        if not key:
            raise ValueError("API_FOOTBALL_KEY is not configured (API-Sports key, shared)")
        async with httpx.AsyncClient(
            base_url=BASE_URL, headers={"x-apisports-key": key}, timeout=30.0
        ) as client:
            yield client

    async def _season_games(self, client: httpx.AsyncClient, league: str) -> list[dict]:
        """Every game in the league's current season, in ONE request.

        Deliberately the whole season rather than a per-date query. /games accepts league+season
        and returns ~300 games in a single call, so a 5-minute live-score beat across seven
        leagues costs 7 requests rather than 7 x (window in days). Against the Pro plan's
        7,500/day that is ~2,000/day for live scores, which is what sized the subscription.
        """
        league_id = BASKETBALL_LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Basketball league id mapping for league={league!r}")
        response = await client.get(
            "/games", params={"league": league_id, "season": _season_for(league)}
        )
        response.raise_for_status()
        return _api_response(response).get("response") or []

    def _to_payload(self, game: dict, league: str) -> FixturePayload:
        status = _map_status((game.get("status") or {}).get("short"))
        scores = game.get("scores") or {}
        home, away = game["teams"]["home"], game["teams"]["away"]
        return FixturePayload(
            external_id=f"{ID_PREFIX}{game['id']}",
            league_external_id=str(BASKETBALL_LEAGUE_IDS[league]),
            home_team_external_id=f"{ID_PREFIX}{home['id']}",
            away_team_external_id=f"{ID_PREFIX}{away['id']}",
            kickoff_utc=_kickoff(game),
            season=str((game.get("league") or {}).get("season") or _season_for(league)),
            home_team_name=home.get("name"),
            away_team_name=away.get("name"),
            # No abbreviation field exists on this provider, so the full name doubles as the
            # short name -- the same convention api_football.py already uses, and the reason
            # short_name must never be populated from a provider "code" field (that collided
            # across four football leagues).
            home_team_short_name=home.get("name"),
            away_team_short_name=away.get("name"),
            status=status,
            # A not-yet-started game reports nulls here rather than 0-0, unlike BallDontLie's
            # WNBA feed -- but the guard is kept anyway so a future change cannot render a
            # live-looking 0-0 before tip-off.
            home_score=_score(scores.get("home")) if status != "scheduled" else None,
            away_score=_score(scores.get("away")) if status != "scheduled" else None,
            # No elapsed-clock field; the provider exposes a quarter, which is a different unit.
            match_minute=None,
        )

    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        now = datetime.now(UTC)
        window_start = (now - timedelta(days=days_back)).date()
        window_end = (now + timedelta(days=days_ahead)).date()
        async with self._client() as client:
            games = await self._season_games(client, league)
        payloads = []
        for game in games:
            try:
                kickoff = _kickoff(game)
            except (TypeError, ValueError):
                logger.warning("API-Basketball game %s has an unparseable date", game.get("id"))
                continue
            if window_start <= kickoff.date() <= window_end:
                payloads.append(self._to_payload(game, league))
        return payloads

    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        if league is None:
            raise ValueError("APIBasketballAdapter.fetch_team_stats requires league")
        async with self._client() as client:
            games = await self._season_games(client, league)
        return _compute_team_stats(team_id, games, n_matches)

    async def fetch_odds(
        self,
        sport: str,
        league: str,
        days_ahead: int,
        dates: list[date] | None = None,
    ) -> list[OddsPayload]:
        """Moneyline only, from the two-way "Home/Away" market.

        /odds takes league+season and returns whichever games the books have actually priced --
        measured as the next few days, not the whole season -- so this is one request per league
        per run rather than one per fixture or one per date. /odds does NOT accept a `date`
        parameter at all ("The Date field do not exist."), which is why the window is applied
        client-side.
        """
        league_id = BASKETBALL_LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"No API-Basketball league id mapping for league={league!r}")
        async with self._client() as client:
            response = await client.get(
                "/odds", params={"league": league_id, "season": _season_for(league)}
            )
            response.raise_for_status()
            entries = _api_response(response).get("response") or []

        cutoff = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
        payloads: list[OddsPayload] = []
        for entry in entries:
            game = entry.get("game") or {}
            game_id = game.get("id")
            if game_id is None:
                continue
            # The window is applied HERE because /odds has no `date` parameter at all ("The
            # Date field do not exist."), so unlike the other adapters this one cannot push the
            # filter to the provider. `dates` is the caller's explicit list of match days --
            # capture_closing_odds narrows to a single day with it -- and it wins over the
            # broader days_ahead cutoff when supplied.
            try:
                kickoff_date = _kickoff(game).date()
            except (KeyError, TypeError, ValueError):
                kickoff_date = None  # an unparseable date is not a reason to drop a real price
            if kickoff_date is not None:
                if dates is not None:
                    if kickoff_date not in set(dates):
                        continue
                elif kickoff_date > cutoff:
                    continue
            for bookmaker in entry.get("bookmakers") or []:
                for bet in bookmaker.get("bets") or []:
                    if bet.get("name") != MONEYLINE_MARKET:
                        continue
                    prices = {v.get("value"): v.get("odd") for v in bet.get("values") or []}
                    home, away = _decimal(prices.get("Home")), _decimal(prices.get("Away"))
                    if home is None and away is None:
                        continue
                    payloads.append(
                        OddsPayload(
                            # The SAME id space as Fixture.external_id, because this provider is
                            # also the fixtures source -- so ingest_odds resolves it on the
                            # direct external_id path and never needs the fuzzy team-name join
                            # that has produced both missed and inverted prices elsewhere.
                            fixture_external_id=f"{ID_PREFIX}{game_id}",
                            bookmaker=str(bookmaker.get("name") or "unknown"),
                            market="h2h",
                            home_odds=home,
                            # Basketball has no draw. The provider's "3Way Result" market does
                            # carry one, for regulation time, and is deliberately not read.
                            draw_odds=None,
                            away_odds=away,
                            updated_at=datetime.now(UTC),
                        )
                    )
        return payloads

    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]:
        """No basketball injury feed is configured. Returns empty rather than raising, matching
        every other sport without one -- the four key-player features are already None for every
        league this adapter serves, exactly as they are for the WNBA."""
        return []


def _decimal(value) -> float | None:
    """Prices arrive as strings. A non-numeric or non-positive one is dropped rather than
    coerced -- a 0 price would read as a free bet to the EV ranking."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 1.0 else None


def _compute_team_stats(team_external_id: str, games: list[dict], n_matches: int) -> TeamStats:
    """Derived from the season game list, mirroring balldontlie.py:_compute_team_stats.

    Computed from /games rather than the provider's own /statistics endpoint on purpose: the
    season list is already fetched once per league per run, so every team's stats come out of
    that one response instead of one request per team. For seven leagues that is 7 requests a
    day rather than 113.
    """
    raw_id = team_external_id.removeprefix(ID_PREFIX)

    def is_home(game: dict) -> bool:
        return str(game["teams"]["home"]["id"]) == raw_id

    def scores(game: dict) -> tuple[int | None, int | None]:
        home, away = _score((game.get("scores") or {}).get("home")), _score(
            (game.get("scores") or {}).get("away")
        )
        return (home, away) if is_home(game) else (away, home)

    completed = [
        g
        for g in games
        if (g.get("status") or {}).get("short") in _FINISHED_STATUSES
        and raw_id in {str(g["teams"]["home"]["id"]), str(g["teams"]["away"]["id"])}
        and all(s is not None for s in scores(g))
    ]
    completed.sort(key=lambda g: str(g["date"]), reverse=True)

    def won(game: dict) -> bool:
        for_, against = scores(game)
        return for_ > against

    recent = completed[:n_matches]
    # Basketball has no draws, so every result is W or L and no "D" can ever appear.
    recent_form = "".join("W" if won(g) else "L" for g in completed[:5]) or None
    # A plain 0.0-1.0 win-rate fraction, NOT football's 3/1/0 points-per-game convention that
    # the field name suggests -- identical to what balldontlie.py puts here, which is what
    # nba_features reads as last10_win_rate.
    form_pts_5 = (sum(1 for g in recent if won(g)) / len(recent)) if recent else None
    attack_str = (sum(scores(g)[0] for g in recent) / len(recent)) if recent else None
    defence_str = (sum(scores(g)[1] for g in recent) / len(recent)) if recent else None

    home_games = [g for g in completed if is_home(g)]
    away_games = [g for g in completed if not is_home(g)]
    home_win_rate = (sum(1 for g in home_games if won(g)) / len(home_games)) if home_games else None
    away_win_rate = (sum(1 for g in away_games if won(g)) / len(away_games)) if away_games else None

    season_point_diff = (
        (sum(scores(g)[0] - scores(g)[1] for g in completed) / len(completed))
        if completed
        else None
    )

    days_since_last_match = None
    if completed:
        days_since_last_match = (datetime.now(UTC) - _kickoff(completed[0])).days

    win_streak = losing_streak = 0.0
    for game in completed:
        if won(game):
            if losing_streak:
                break
            win_streak += 1
        else:
            if win_streak:
                break
            losing_streak += 1

    return TeamStats(
        team_external_id=team_external_id,
        elo_rating=None,
        attack_str=attack_str,
        defence_str=defence_str,
        form_pts_5=form_pts_5,
        xg_for_5=None,
        xg_against_5=None,
        days_since_last_match=days_since_last_match,
        home_win_rate=home_win_rate,
        away_win_rate=away_win_rate,
        season_point_diff=season_point_diff,
        win_streak=win_streak,
        losing_streak=losing_streak,
        recent_form=recent_form,
    )


async def fetch_h2h_win_rate(
    home_external_id: str, away_external_id: str, league: str
) -> float | None:
    """Home team's win rate in previous meetings, or None if they have never met.

    A standalone function rather than a DataSourceAdapter method, matching balldontlie.py and
    api_football.py: head-to-head needs BOTH teams and so does not fit fetch_team_stats's
    per-team shape.

    /games?h2h=ID1-ID2 is the real endpoint. There is NO /games/h2h route on this API ("This
    endpoint do not exist."), which is the shape football uses -- checked rather than assumed.
    """
    settings = get_settings()
    if not settings.api_football_key:
        return None
    home_raw = home_external_id.removeprefix(ID_PREFIX)
    away_raw = away_external_id.removeprefix(ID_PREFIX)
    try:
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"x-apisports-key": settings.api_football_key},
            timeout=30.0,
        ) as client:
            response = await client.get("/games", params={"h2h": f"{home_raw}-{away_raw}"})
            response.raise_for_status()
            games = _api_response(response).get("response") or []
    except httpx.HTTPError:
        logger.warning("API-Basketball h2h lookup failed for %s", league, exc_info=True)
        return None

    decided = []
    for game in games:
        if (game.get("status") or {}).get("short") not in _FINISHED_STATUSES:
            continue
        home_score = _score((game.get("scores") or {}).get("home"))
        away_score = _score((game.get("scores") or {}).get("away"))
        if home_score is None or away_score is None:
            continue
        # Which SIDE our home team was in that historical meeting decides who won it -- the
        # fixture's current home team is not necessarily the home team of a past meeting.
        was_home = str(game["teams"]["home"]["id"]) == home_raw
        our_score, their_score = (home_score, away_score) if was_home else (away_score, home_score)
        decided.append(our_score > their_score)
    if not decided:
        return None
    return sum(decided) / len(decided)


async def _h2h_meetings(home_raw: str, away_raw: str) -> list[dict]:
    """Decided meetings between two teams, newest first. Shared by the win rate and the panel."""
    settings = get_settings()
    if not settings.api_football_key:
        return []
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"x-apisports-key": settings.api_football_key},
        timeout=30.0,
    ) as client:
        response = await client.get("/games", params={"h2h": f"{home_raw}-{away_raw}"})
        response.raise_for_status()
        games = _api_response(response).get("response") or []
    decided = [
        g
        for g in games
        if (g.get("status") or {}).get("short") in _FINISHED_STATUSES
        and _score((g.get("scores") or {}).get("home")) is not None
        and _score((g.get("scores") or {}).get("away")) is not None
    ]
    decided.sort(key=lambda g: str(g["date"]), reverse=True)
    return decided


H2H_PANEL_MEETINGS = 10

# Box scores are fetched PER MEETING -- one /games/statistics/teams call each -- so the stat
# window is deliberately narrower than the record window above. The record is free (it comes
# out of the single /games?h2h= call), the stats are not, and this panel is fetched per
# FIXTURE-DETAIL VIEW rather than per ingested fixture, so its cost scales with how many
# people open cards. Mirrors api_football's own H2H_DETAIL_MEETINGS = 5 for the same reason.
H2H_STAT_MEETINGS = 5


async def fetch_standings(league: str) -> list[StandingRow]:
    """The league table, computed by the provider rather than from our own fixtures.

    BASKETBALL IS NOT FOOTBALL-SHAPED, and two differences are measured rather than assumed:

    1. **`group.points` is 0 for the NBA and the WNBA.** European competitions award 2 points a
       win and rank on them; the North American leagues rank on WIN-LOSS RECORD and the provider
       returns a zero there. Writing that into `points` and letting the client print "PTS" would
       show a column of zeros, so the router picks the display columns per sport -- wins for
       basketball, points for football.
    2. **The NBA returns TWO tables** (60 rows then 30) because it is grouped by conference and
       by division. Every table is flattened here with its group label kept, so the fixture
       screen can show the subject team's own section instead of a 60-row list with silent
       duplicates.

    `drawn` is structurally 0: basketball cannot tie. `goal_difference` carries the POINT
    differential -- the field is football-named because the row type is shared, and the column
    LABEL is what the user reads.
    """
    league_id = STANDINGS_LEAGUE_IDS.get(league)
    if league_id is None:
        raise ValueError(f"No API-Basketball league id mapping for league={league!r}")
    settings = get_settings()
    if not settings.api_football_key:
        return []
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"x-apisports-key": settings.api_football_key},
        timeout=30.0,
    ) as client:
        response = await client.get(
            "/standings", params={"league": league_id, "season": _season_for(league)}
        )
        response.raise_for_status()
    rows: list[StandingRow] = []
    seen: set[str] = set()
    for table in _api_response(response).get("response", []):
        for entry in table:
            team = entry.get("team") or {}
            external_id = f"{ID_PREFIX}{team.get('id')}"
            # API-Basketball suffixes every WNBA club with " W" ("Minnesota Lynx W"). That is
            # the provider disambiguating itself from the men's league, not part of the club's
            # name, and showing it would read as a typo on the table. Scoped to the WNBA rather
            # than stripped globally, so a club legitimately ending in " W" survives elsewhere.
            team_name = (team.get("name") or "").strip()
            if league == "wnba" and team_name.endswith(" W"):
                team_name = team_name[:-2]
            group = (entry.get("group") or {}).get("name")
            # ONE ROW PER TEAM, first table wins. The NBA returns 60 rows for 30 teams --
            # every club once by CONFERENCE and again by DIVISION -- and the provider returns
            # the conference table first, which is the canonical NBA standings view. Keying on
            # (team, group) instead would be "correct" and render each club twice.
            if external_id in seen:
                continue
            seen.add(external_id)
            games = entry.get("games") or {}
            points = entry.get("points") or {}
            scored, conceded = points.get("for"), points.get("against")
            rows.append(
                StandingRow(
                    rank=entry.get("position") or 0,
                    team_external_id=external_id,
                    team_name=team_name,
                    played=(games.get("played") or 0),
                    won=((games.get("win") or {}).get("total") or 0),
                    drawn=0,
                    lost=((games.get("lose") or {}).get("total") or 0),
                    goal_difference=(
                        (scored - conceded) if scored is not None and conceded is not None else 0
                    ),
                    points=((entry.get("group") or {}).get("points") or 0),
                    form=entry.get("form"),
                    group=group,
                )
            )
    return rows


# Resolved team-name -> provider-id maps, per league, held for the life of the process. The
# list changes once a season; a restart re-resolves, which is cheap and self-correcting.
_TEAM_NAME_CACHE: dict[str, dict[str, int]] = {}


def normalise_team_name(name: str) -> str:
    """Key for matching a club across the two basketball providers.

    The NBA and WNBA have their FIXTURES from BallDontLie and their table and box scores from
    here, and the two id spaces cannot meet -- so the club name is the only join. Measured
    before being relied on: 18 of 18 NBA clubs we hold match exactly.

    The one real discrepancy is this provider suffixing every WNBA club " W" ("Minnesota
    Lynx W"), stripped here rather than special-cased per club.
    """
    cleaned = (name or "").strip()
    if cleaned.endswith(" W"):
        cleaned = cleaned[:-2]
    return cleaned.casefold()


async def resolve_team_ids_by_name(league: str, names: list[str]) -> dict[str, int]:
    """Provider team ids for clubs we only know by name — the NBA and WNBA.

    Returns only what it actually finds. A caller that gets back fewer ids than it asked for
    must degrade rather than guess, because the wrong id returns a real, confident, WRONG
    head-to-head.
    """
    league_id = STANDINGS_LEAGUE_IDS.get(league)
    if league_id is None:
        return {}
    settings = get_settings()
    if not settings.api_football_key:
        return {}
    if league not in _TEAM_NAME_CACHE:
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"x-apisports-key": settings.api_football_key},
            timeout=30.0,
        ) as client:
            response = await client.get(
                "/teams", params={"league": league_id, "season": _season_for(league)}
            )
            response.raise_for_status()
            entries = _api_response(response).get("response") or []
        _TEAM_NAME_CACHE[league] = {
            normalise_team_name(entry.get("name")): entry["id"]
            for entry in entries
            if entry.get("id") is not None
        }
    lookup = _TEAM_NAME_CACHE[league]
    resolved = {}
    for name in names:
        found = lookup.get(normalise_team_name(name))
        if found is not None:
            resolved[name] = found
    return resolved


async def _box_score_stats(
    client: httpx.AsyncClient, meetings: list[dict], home_raw: str
) -> list[H2HPanelStat]:
    """Averaged box scores across the most recent meetings, from THIS fixture's home side.

    NEVER FABRICATES A ZERO. Each measure averages over its OWN list of real values, so a
    meeting that reports rebounds but not fouls contributes to the rebound average and is
    simply absent from the fouls one -- the same rule football's H2H panel already follows.
    A measure with no real values anywhere is dropped rather than shown as 0.0, which is why
    personal_fouls and the offensive/defensive rebound split (both null in real responses)
    will usually not appear at all.
    """
    home_values: dict[str, list[float]] = {}
    away_values: dict[str, list[float]] = {}

    def record(label: str, is_home: bool, value) -> None:
        if value is None:
            return
        (home_values if is_home else away_values).setdefault(label, []).append(float(value))

    for game in meetings[:H2H_STAT_MEETINGS]:
        try:
            response = await client.get("/games/statistics/teams", params={"id": game.get("id")})
            response.raise_for_status()
            rows = _api_response(response).get("response") or []
        except httpx.HTTPError:
            # One unavailable box score must not cost the whole panel: the record and the
            # points rows are already real.
            continue
        for row in rows:
            is_home = str((row.get("team") or {}).get("id")) == home_raw
            record("Rebounds", is_home, (row.get("rebounds") or {}).get("total"))
            record("Assists", is_home, row.get("assists"))
            record("Steals", is_home, row.get("steals"))
            record("Turnovers", is_home, row.get("turnovers"))
            record("Field goal %", is_home, (row.get("field_goals") or {}).get("percentage"))
            record("3-point %", is_home, (row.get("threepoint_goals") or {}).get("percentage"))

    def mean(values: list[float] | None) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    stats: list[H2HPanelStat] = []
    # Fixed order so the panel does not reshuffle between fixtures.
    for label in ("Rebounds", "Assists", "Field goal %", "3-point %", "Steals", "Turnovers"):
        home, away = mean(home_values.get(label)), mean(away_values.get(label))
        if home is None and away is None:
            continue
        stats.append(
            H2HPanelStat(label=label, home=home, away=away, suffix="%" if "%" in label else "")
        )
    return stats


async def fetch_h2h_panel(
    home_external_id: str, away_external_id: str, league: str = "acb"
) -> H2HPanel | None:
    """The fixture-detail head-to-head panel for the European competitions.

    WHY THIS EXISTS AT ALL: _fetch_head_to_head routed every sport_slug == "nba" fixture to
    BallDontLie, which is correct for the NBA and the WNBA and fatal for these -- their ids are
    "ab:"-prefixed and BallDontLie cannot parse them. The resulting error was NOT an
    httpx.HTTPError, so the panel's own try/except did not catch it and the whole fixture screen
    returned HTTP 500. Every European basketball fixture detail was broken; the WNBA was fine.

    TWO STAT ROWS, for the same reason BallDontLie's NBA panel has them: the final score is the
    only per-meeting number guaranteed across these competitions, which yields points scored and
    points conceded. Fabricating rebounds or shooting percentages to match football's five-row
    panel would mean inventing them.

    Values are relative to THIS fixture's home/away assignment, not each historical meeting's
    own -- a team's record must not flip depending on which side it happened to be on before.
    """
    home_raw = home_external_id.removeprefix(ID_PREFIX)
    away_raw = away_external_id.removeprefix(ID_PREFIX)
    try:
        meetings = await _h2h_meetings(home_raw, away_raw)
    except httpx.HTTPError:
        logger.warning("API-Basketball h2h panel failed for %s", league, exc_info=True)
        return None
    meetings = meetings[:H2H_PANEL_MEETINGS]
    if not meetings:
        return None

    home_wins = away_wins = 0
    scored: list[int] = []
    conceded: list[int] = []
    rows: list[H2HMeeting] = []
    for game in meetings:
        home_score = _score((game.get("scores") or {}).get("home"))
        away_score = _score((game.get("scores") or {}).get("away"))
        was_home = str(game["teams"]["home"]["id"]) == home_raw
        ours, theirs = (home_score, away_score) if was_home else (away_score, home_score)
        scored.append(ours)
        conceded.append(theirs)
        if ours > theirs:
            home_wins += 1
        else:
            away_wins += 1
        try:
            kickoff = _kickoff(game)
        except (KeyError, ValueError):
            kickoff = None
        if kickoff is None:
            # Without a date the row cannot be ordered or labelled, and a fabricated one would
            # sort wrongly against the others. The meeting still counts toward the record.
            continue
        rows.append(
            H2HMeeting(
                fixture_external_id=f"{ID_PREFIX}{game.get('id')}",
                kickoff_utc=kickoff,
                home_team=(game["teams"]["home"] or {}).get("name") or "",
                away_team=(game["teams"]["away"] or {}).get("name") or "",
                home_score=home_score,
                away_score=away_score,
                # Basketball cannot draw, so W/L only -- no "D" branch to get wrong.
                result="W" if ours > theirs else "L",
                competition=(game.get("league") or {}).get("name"),
            )
        )

    def avg(values: list[int]) -> float:
        return round(sum(values) / len(values), 1)

    # "Points conceded" used to sit beside "Points scored" and was removed when the real box
    # score arrived: in a TWO-TEAM head-to-head it is the exact mirror (118.6/115.0 becomes
    # 115.0/118.6), so it filled a row without adding a fact. Rebounds and shooting are facts.
    settings = get_settings()
    box_score_stats: list[H2HPanelStat] = []
    if settings.api_football_key:
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"x-apisports-key": settings.api_football_key},
                timeout=30.0,
            ) as client:
                box_score_stats = await _box_score_stats(client, meetings, home_raw)
        except httpx.HTTPError:
            # The record and the points row are already real and worth showing alone.
            logger.warning("Box-score stats unavailable for %s", league, exc_info=True)

    return H2HPanel(
        meetings_count=len(meetings),
        home_wins=home_wins,
        # Basketball has no draws, so this is structurally 0 rather than merely unobserved.
        draws=0,
        away_wins=away_wins,
        stats=[
            H2HPanelStat(label="Points", home=avg(scored), away=avg(conceded)),
            *box_score_stats,
        ],
        meetings=rows,
    )
