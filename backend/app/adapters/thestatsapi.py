"""TheStatsAPI as a SECOND corner-statistics source, behind API-Football.

WHY THIS EXISTS. Corner counts drive the green tick / red cross on a settled corners pick, and
without one the card shows a neutral grey badge on a finished match. Measured 2026-08-14 across
every settled football fixture, API-Football's live coverage is 165/190 (86.8%) and the misses
are not spread evenly:

    veikkausliiga    0/7    0.0%      <- API-Football has NO corner statistics for this league
    brasileirao     17/32  53.1%
    j1_league       10/11  90.9%      <- an outlier, not a systemic gap
    every other league      100%

TheStatsAPI -- already provisioned, already paid for, already used for xG -- carries corners for
99.8% of both Veikkausliiga and Brasileirao in the 19,075 matches already cached. So this is a
second reader of a source we hold, not a new vendor.

LATENCY IS THE WHOLE CONSTRAINT, AND IT IS WHY THIS IS NOT A LIVE FALLBACK. Measured against the
real API: a Veikkausliiga match 3.4 hours past kickoff was already marked `finished` and its
/stats endpoint returned HTTP 404, while every sampled match five or more days old carried real
corners. Status is prompt; STATISTICS lag. The exact lag could not be pinned tighter than
">3.4h and <=~5 days" because no sampled league had a match in that window.

So the cadence below is a QUOTA decision, not a correctness one. The fallback fires as soon as
the data exists; asking every five minutes would simply spend a metered monthly allowance
learning "not yet" a few hundred times per fixture.

MATCHED ON DATE + SCORE, names only as a tiebreak -- the same join the offline collector uses,
and a final score is a far stronger key than two team names spelled by different providers.
"""

import logging
from datetime import date, timedelta

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.thestatsapi.com/api/football"

# Competition ids, taken verbatim from ml/training/collect_thestatsapi_xg.py's own TARGETS
# rather than re-derived. Two were resolved there by NAME per country against the full
# 150-competition list because the obvious search returns a second-tier league -- re-deriving
# them here would be a second chance to pick the wrong one.
COMPETITION_IDS = {
    "allsvenskan": "comp_1002",
    "austria_bundesliga": "comp_4893",
    "brasileirao": "comp_4795",
    "bundesliga": "comp_4643",
    "championship": "comp_8321",  # probed 2026-08-18: "Championship", England
    "csl": "comp_7712",
    "czech_first": "comp_9766",
    "denmark_superliga": "comp_7938",
    "ekstraklasa": "comp_9711",
    "eliteserien": "comp_1992",
    "epl": "comp_3039",
    "j1_league": "comp_6240",
    "laliga": "comp_8814",
    "liga_i": "comp_9639",
    "ligue1": "comp_0256",
    "mls": "comp_9799",
    "scottish_prem": "comp_6387",
    "seriea": "comp_5840",
    "veikkausliiga": "comp_2674",
}

# Kickoff can straddle midnight UTC between two providers, so the date window is widened by a
# day either side and the score does the real discriminating.
DATE_WINDOW_DAYS = 1

# The month from which a split season is considered to have STARTED rather than ended. Matches
# _current_football_season's own July boundary in api_football.py.
SEASON_SPLIT_MONTH = 7

# Season ids are stable for a season and cost a call each to resolve, so they are held for the
# life of the process. A worker restart re-resolves, which is cheap and self-correcting.
_SEASON_CACHE: dict[str, str | None] = {}


class TheStatsAPINotConfigured(RuntimeError):
    """Raised when no key is set. Distinct from an HTTP failure: one is a deployment gap the
    operator can fix, the other is the provider having a bad day."""


def _client() -> httpx.AsyncClient:
    key = get_settings().thestatsapi_key
    if not key:
        raise TheStatsAPINotConfigured(
            "THESTATSAPI_KEY is not set — the corner fallback cannot run. Note this key lived "
            "only in keys.docx for the offline collector; a deployed worker needs it in the "
            "environment (infra/render.yaml)."
        )
    return httpx.AsyncClient(
        base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    )


async def _season_id(client: httpx.AsyncClient, league_slug: str, kickoff: date) -> str | None:
    """Resolve the season a fixture belongs to FROM ITS KICKOFF DATE, not from a season label.

    THE LABELS CANNOT BE MATCHED ON, and the first version of this tried to. TheStatsAPI names
    calendar seasons "Veikkausliiga 2026" but split ones "J1 League 26/27" and "Premier League
    26/27", so a substring search for the year misses every autumn-spring league -- which is
    most of them. It happened to work for Veikkausliiga and hid the fault.

    API-Football's own labels cannot rescue it either, because they are inconsistent between
    leagues: the EPL's 2026-27 season is labelled 2026 (its START year) while the J1 League's
    2026-27 season is labelled 2027 (its END year). Verified live against both.

    start_year/end_year are structured and unambiguous, so the kickoff date decides. Where two
    seasons overlap a year -- the J1 League ran a calendar 2026 season AND began a 26/27 season
    in the same year, during its switch to the European calendar -- the month breaks the tie on
    the usual convention, and is_current settles anything still level.
    """
    competition = COMPETITION_IDS.get(league_slug)
    if competition is None:
        return None
    cache_key = f"{league_slug}:{kickoff.year}:{kickoff.month >= SEASON_SPLIT_MONTH}"
    if cache_key in _SEASON_CACHE:
        return _SEASON_CACHE[cache_key]

    response = await client.get(f"/competitions/{competition}/seasons")
    resolved = None
    if response.status_code == 200:
        rows = response.json().get("data", [])
        spanning = [
            row
            for row in rows
            if row.get("start_year") is not None
            and int(row["start_year"])
            <= kickoff.year
            <= int(row.get("end_year") or row["start_year"])
        ]
        if len(spanning) > 1:
            # A season starting in the second half of the year owns fixtures played in that
            # half; one ending this year owns the first half.
            preferred = [
                row
                for row in spanning
                if (
                    int(row["start_year"]) == kickoff.year
                    if kickoff.month >= SEASON_SPLIT_MONTH
                    else int(row.get("end_year") or row["start_year"]) == kickoff.year
                )
            ]
            spanning = preferred or spanning
        if len(spanning) > 1:
            spanning = [row for row in spanning if row.get("is_current")] or spanning
        # Still ambiguous means two seasons genuinely claim this date; refusing is safer than
        # picking one, because the wrong season yields a confident match against another
        # fixture entirely.
        resolved = spanning[0].get("id") if len(spanning) == 1 else None
    _SEASON_CACHE[cache_key] = resolved
    return resolved


def _corners_from_stats(payload: dict) -> tuple[int, int] | None:
    overview = (payload.get("data") or payload).get("overview") or {}
    for key in ("corner_kicks", "corners"):
        block = overview.get(key)
        if not block:
            continue
        values = block.get("all") if isinstance(block, dict) and "all" in block else block
        if isinstance(values, dict) and values.get("home") is not None:
            return int(values["home"]), int(values["away"])
    return None


async def fetch_corners(
    league_slug: str,
    kickoff: date,
    home_score: int,
    away_score: int,
) -> tuple[int, int] | None:
    """Corner counts for one settled fixture, or None when they genuinely aren't published yet.

    None is returned for every unhappy path -- unmapped league, unresolvable season, no match on
    date+score, statistics not yet published (the HTTP 404 measured 3.4h after kickoff). The
    caller leaves the fixture ungraded, which renders as a neutral badge rather than a guess.
    """
    if league_slug not in COMPETITION_IDS:
        return None

    async with _client() as client:
        season_id = await _season_id(client, league_slug, kickoff)
        if season_id is None:
            return None

        window = timedelta(days=DATE_WINDOW_DAYS)
        response = await client.get(
            "/matches",
            params={
                "season_id": season_id,
                "status": "finished",
                "date_from": (kickoff - window).isoformat(),
                "date_to": (kickoff + window).isoformat(),
                "per_page": 100,
            },
        )
        if response.status_code != 200:
            return None

        # THE SCORE IS THE KEY. Two providers spell teams differently ("Wolves" vs
        # "Wolverhampton Wanderers"), but an exact scoreline inside a three-day window is
        # decisive, and an ambiguous one is refused rather than guessed.
        matches = [
            match
            for match in response.json().get("data", [])
            if (match.get("score") or {}).get("home") == home_score
            and (match.get("score") or {}).get("away") == away_score
        ]
        if len(matches) != 1:
            return None

        stats = await client.get(f"/matches/{matches[0]['id']}/stats")
        if stats.status_code != 200:
            # 404 here is the normal "not published yet" case, not an error worth logging at
            # warning level -- it is expected for anything recent.
            return None
        return _corners_from_stats(stats.json())
