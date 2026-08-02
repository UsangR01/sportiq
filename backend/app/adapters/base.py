from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class OddsPayload:
    # This is the ODDS provider's own event ID (TheRundown's, per TDD §6.2 — always TheRundown
    # regardless of sport) — a *different* ID space from a fixture's stats-provider
    # external_id (BallDontLie for NBA, API-Football for football). The two only get linked
    # by matching team abbreviations + kickoff time; see
    # app/fixtures/service.py:find_fixture_by_abbreviations_and_time.
    fixture_external_id: str
    bookmaker: str
    market: str  # "h2h" | "spread" | "total" | "double_chance" | "corners_total"
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    updated_at: datetime
    # Needed for fixture matching — not part of the original shape.
    home_team_short_name: str | None = None
    away_team_short_name: str | None = None
    kickoff_utc: datetime | None = None
    # TOTAL/CORNERS_TOTAL only (see app/odds/models.py:Odds) — null for h2h/double_chance.
    # DOUBLE_CHANCE reuses home_odds (Home-or-Draw price) / away_odds (Away-or-Draw price).
    line: float | None = None
    over_odds: float | None = None
    under_odds: float | None = None


@dataclass(frozen=True)
class FixturePayload:
    external_id: str
    league_external_id: str
    home_team_external_id: str
    away_team_external_id: str
    kickoff_utc: datetime
    season: str
    # Not part of the original shape — a first-seen team needs a display name to create its
    # Team row, and a provider that returns historical/completed fixtures (e.g. verifying
    # against a past date range) needs to say so, rather than every ingested fixture silently
    # defaulting to "scheduled" forever. Optional so adapters without this data can omit it.
    home_team_name: str | None = None
    away_team_name: str | None = None
    home_team_short_name: str | None = None
    away_team_short_name: str | None = None
    # "scheduled" | "live" | "completed" | "postponed" — matches FixtureStatus
    status: str = "scheduled"
    # Not part of the original shape — needed to show a score inline (Home feed, fixture
    # detail) instead of requiring a separate live-scores source. Real for both adapters that
    # implement fetch_fixtures for real (API-Football, BallDontLie) since their /fixtures-
    # equivalent endpoints already carry goals/scores for any in-progress or completed game;
    # None for a fixture that hasn't started.
    home_score: int | None = None
    away_score: int | None = None
    # Elapsed match minute — real for API-Football (fixture.status.elapsed), None where the
    # provider doesn't expose it (BallDontLie's /games has no equivalent field).
    match_minute: int | None = None
    # None for a normally-played-out result; "retired"/"walkover" for one that ended without
    # being played out. Tennis-only in practice (see balldontlie_tennis.py:_match_result_type,
    # which has to infer this structurally from the score because the provider reports real
    # retirements as plain match_status="finished"). Consumed by the mobile feed to show a
    # neutral "RET" badge instead of a win/loss verdict, since bookmakers generally void bets
    # on a retirement. Football/NBA adapters leave it None.
    result_type: str | None = None
    # Tennis-only: matches are grouped by TOURNAMENT rather than by league/tour, since "ATP
    # Tour" alone tells a user nothing about which event to look up in a betting app. Real,
    # already-embedded fields on every BallDontLie match response — no extra API call.
    # `tournament_location` is a CITY (e.g. "Montreal"), not a country: the provider exposes no
    # country field at all, which is why the mobile flag needs its own city->country map.
    tournament_name: str | None = None
    tournament_surface: str | None = None
    tournament_location: str | None = None
    # True when kickoff_utc was INFERRED rather than reported by the provider, so the client
    # can say "time TBC" instead of stating a precise time we don't actually have. Real and
    # common for tennis: measured across a full ATP tournament, 570 of 600 matches had no
    # scheduled_time at all, and of the 30 that did, 17 were midnight (date only). Falling
    # back to the tournament's start date gave every match in a 12-day draw the same
    # timestamp, which both showed wrong times AND made matches appear on days they were not
    # played. Fabricating a kickoff contradicts this codebase's own rule of never inventing a
    # neutral value; flagging it keeps the fixture usable without asserting false precision.
    kickoff_is_estimated: bool = False


@dataclass(frozen=True)
class TeamStats:
    team_external_id: str
    elo_rating: float | None = None
    attack_str: float | None = None
    defence_str: float | None = None
    form_pts_5: float | None = None
    xg_for_5: float | None = None
    xg_against_5: float | None = None
    days_since_last_match: int | None = None
    home_win_rate: float | None = None
    away_win_rate: float | None = None
    # Season-long (not last-N) average point differential — used as a "net rating" proxy by
    # app/models_ml/nba_features.py. Not part of the original shape; added because the model
    # needs both a short-term-form signal (form_pts_5/attack_str/defence_str, last-N) and a
    # longer season-quality signal, and TeamFeatures had no season-long aggregate at all.
    season_point_diff: float | None = None
    # Current consecutive-match streak, derived from the same recent-form data as form_pts_5
    # (API-Football's "form" string; NBA has no equivalent source yet, stays None). Exactly one
    # of the two is ever positive for a given team (a team is never both on a win streak and a
    # losing streak) — kept as two separate fields rather than one signed value so a team with
    # no streak at all (most recent match drawn) has an unambiguous (0, 0) rather than an
    # overloaded 0 that could mean either "no streak" or "streak of length 0".
    win_streak: float | None = None
    losing_streak: float | None = None
    # Tennis only — real, provider-computed ATP/WTA ranking points (BallDontLie's /rankings).
    # Used as the primary relative-strength signal instead of a hand-rolled Elo approximation
    # (unlike football, which added one later) — simpler and more honest than approximating a
    # rating system when a real one already exists. None for every other sport.
    rank_points: float | None = None


@dataclass(frozen=True)
class InjuryUpdate:
    player_external_id: str
    team_external_id: str
    player_name: str
    status: str  # "OUT" | "GTD" | "PROBABLE" | "ACTIVE"
    return_date: str | None
    salary_rank: int | None
    source: str  # "rotowire" | "balldontlie"


class DataSourceAdapter(ABC):
    """Every external provider is accessed only through this interface (TDD §2.2 KEY note).
    Ingest workers and routes must never call a provider's SDK/HTTP client directly."""

    @abstractmethod
    async def fetch_odds(
        self,
        sport: str,
        league: str,
        days_ahead: int,
        dates: list[date] | None = None,
    ) -> list[OddsPayload]:
        """Deliberately mirrors fetch_fixtures's shape, not a fixture_ids list as originally
        drafted: real odds providers (TheRundown) are queried by sport+date-range, not by IDs
        the caller already knows — those IDs live in a different provider's ID space
        entirely. The caller (ingest_odds.py) matches results to internal Fixture rows
        itself, the same way ingest_fixtures.py resolves teams via get_or_create_team."""
        ...

    @abstractmethod
    async def fetch_fixtures(
        self, sport: str, league: str, days_ahead: int, days_back: int = 0
    ) -> list[FixturePayload]:
        """days_back is optional (default 0, the original forward-only behavior) — used by
        ingest_fixtures.py to backfill recently-completed fixtures for browsing/score display,
        and by ingest_live_scores.py to re-poll a narrow window around "now" for in-progress
        games. Queries from (now - days_back) to (now + days_ahead)."""
        ...

    @abstractmethod
    async def fetch_team_stats(
        self, team_id: str, n_matches: int, league: str | None = None
    ) -> TeamStats:
        """league is optional and ignored by adapters that don't need it (e.g. BallDontLie
        derives NBA's single league/season internally). API-Football needs it: a team's
        league can't be inferred from team_id alone, and /teams/statistics requires both
        league and season — see app/adapters/api_football.py."""
        ...

    @abstractmethod
    async def fetch_injuries(self, sport: str) -> list[InjuryUpdate]: ...
