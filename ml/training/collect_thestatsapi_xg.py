"""Collect real per-match expected goals from TheStatsAPI and resolve them onto our own IDs.

Why this exists: API-Football supplies no xG at any tier, so team_features.xg_for_5/
xg_against_5 have sat unpopulated since the schema was written (verified: 0 of 255 rows).
Measured on 693 real EPL fixtures before this was built, rolling xG correlates with actual
total goals at r=+0.129 (95% CI [+0.055,+0.202]) where rolling GOALS manages +0.062 with a CI
spanning zero — i.e. the signal the Over/Under market has been missing.

Two stages, deliberately separable:
  1. FETCH — one call per fixture (confirmed: include=/expand= are silently ignored, there is
     no bulk path). Checkpointed every 20 into a raw JSON cache, so a 429 storm or a killed
     process never loses completed work.
  2. RESOLVE — TheStatsAPI has its own ID space (comp_/mt_/tm_), so matches are joined onto
     API-Football FIXTURE_ID/TEAM_ID by DATE + FINAL SCORE, which both providers observe
     identically, with team-name similarity as a tiebreak for same-day same-score collisions
     (~20% of fixtures — two 1-0s on a matchday is ordinary). A pair that stays ambiguous is
     DROPPED rather than guessed: xG attached to the wrong fixture is far worse than absent
     xG, and the downstream merge is a left join that already tolerates gaps.

The join lives here rather than in football_features.py so merge_xg_into_game_log stays as
trivial as merge_corners_into_game_log.

Pacing is read from the response's own x-ratelimit-limit rather than hardcoded, because the
plan on this key has already changed once (12/min -> 120/min) and a stale constant silently
costs hours.

    python ml/training/collect_thestatsapi_xg.py --leagues epl brasileirao
"""

import argparse
import asyncio
import json
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
# pydantic-settings resolves env_file against the CWD, so a script run from the repo root
# silently loads a blank .env and every credential falls back to "" — see CLAUDE.md.
load_dotenv(BACKEND_DIR / ".env")

from app.core.database import async_session_factory, engine  # noqa: E402
from app.fixtures.models import Team  # noqa: E402
from app.sports.models import League  # noqa: E402
from sqlalchemy import select  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_CACHE = DATA_DIR / "thestatsapi_xg_raw.json"
BASE_URL = "https://api.thestatsapi.com/api/football"
KEYS_DOCX = Path(__file__).resolve().parent.parent.parent / "keys.docx"

# Pacing adapts to whatever the plan actually allows, read from x-ratelimit-limit on the
# first real response. It was hardcoded at 5.2s for a 12/min plan; when that plan was raised to
# 120/min the constant would have silently kept the run 10x slower than necessary — hours of
# wall-clock spent obeying a limit that no longer existed. The floor keeps a safety margin
# under whatever is advertised.
REQUEST_DELAY_SECONDS = 5.2
MIN_REQUEST_DELAY_SECONDS = 0.55


def _pace_from_headers(headers) -> None:
    """Relax REQUEST_DELAY_SECONDS to match the plan's advertised per-minute limit."""
    global REQUEST_DELAY_SECONDS
    try:
        per_minute = int(headers.get("x-ratelimit-limit", 0))
    except (TypeError, ValueError):
        return
    if per_minute <= 0:
        return
    # 85% of the advertised rate: bursts and retries still have room, and a 429 storm on this
    # API escalates rather than degrades gracefully.
    target = max(MIN_REQUEST_DELAY_SECONDS, 60.0 / (per_minute * 0.85))
    if abs(target - REQUEST_DELAY_SECONDS) > 0.05:
        print(
            f"  pacing: {per_minute}/min advertised -> {target:.2f}s between requests",
            flush=True,
        )
        REQUEST_DELAY_SECONDS = target


CHECKPOINT_EVERY = 20
NAME_MATCH_FLOOR = 0.55
AMBIGUITY_MARGIN = 0.15

# (league slug matching train_football.py's LEAGUES, competition id, season label, season int).
# EPL 21/22 is deliberately absent: measured 0/5 sampled matches carry xG, so it is a genuine
# upstream gap rather than something a re-run would fix.
#
# LA LIGA 24/25 IS IN THIS LIST AND STILL ONLY 18% COVERED, AND THAT IS NOT A BUG HERE. An audit
# on 2026-08-13 flagged it as the one target that had been attempted yet came back under half
# covered, which looks exactly like a rate-limited or half-finished run. It is neither: all 380
# matches are in the raw cache, and only 70 of them carry a real `xg.home` from the provider.
# Its neighbouring seasons are 380/380. So this is an upstream hole in one season of one league,
# and re-running costs 380 calls to change nothing.
#
# Worth stating because the check that settles it is one line against the cache, and two
# sloppier probes got it wrong first — grepping the raw JSON for a `"xg":` string reports 0/380
# for seasons that are fully covered, because the key is present-but-null on every record. Read
# `(match.get("xg") or {}).get("home") is not None`, the same condition resolve() uses.
TARGETS = [
    ("epl", "comp_3039", "22/23", 2022),
    ("epl", "comp_3039", "23/24", 2023),
    ("epl", "comp_3039", "24/25", 2024),
    ("epl", "comp_3039", "25/26", 2025),
    ("championship", "comp_8321", "22/23", 2022),
    ("championship", "comp_8321", "23/24", 2023),
    ("championship", "comp_8321", "24/25", 2024),
    ("championship", "comp_8321", "25/26", 2025),
    ("brasileirao", "comp_4795", "2022", 2022),
    ("brasileirao", "comp_4795", "2023", 2023),
    ("brasileirao", "comp_4795", "2024", 2024),
    ("brasileirao", "comp_4795", "2025", 2025),
    # The three leagues that had NO xG at all in the first retrain, which is why that result
    # was diluted: they are ~60% of the pooled test set, so every one of those fixtures scored
    # the new features as missing. Season labels differ by league and were read from each
    # competition's own /seasons response rather than assumed — MLS and the CSL run on calendar
    # years, the Scottish Premiership on the European Aug-May convention, the same split
    # CALENDAR_YEAR_SEASON_LEAGUES already encodes for live ingestion.
    # Start seasons below are MEASURED, not assumed. Sampling two matches per season showed xG
    # begins at 2023 for MLS and the CSL (2021/2022 return 0/2), and the Scottish Premiership
    # has none at all before 25/26 — the same hard upstream cutoff EPL has at 21/22. Collecting
    # the earlier seasons would have burned ~1,500 calls on fixtures that carry no xG whatever.
    ("mls", "comp_9799", "2023", 2023),
    ("mls", "comp_9799", "2024", 2024),
    ("mls", "comp_9799", "2025", 2025),
    ("csl", "comp_7712", "2023", 2023),
    ("csl", "comp_7712", "2024", 2024),
    ("csl", "comp_7712", "2025", 2025),
    # Scottish Prem only ever reaches the TEST season, and even there the sample came back 1/2,
    # so expect roughly half the fixtures to have no xG. Collected anyway because 2025 is the
    # season the held-out measurement runs on, which is exactly where coverage matters most.
    ("scottish_prem", "comp_6387", "25/26", 2025),
    # The four remaining MVP-scope European leagues. All four measured 5/6 or 2/2 sampled
    # matches carrying xG in EVERY season 22/23-25/26 — the best coverage of any league here,
    # better than EPL's (which has nothing before 22/23).
    #
    # Two competition-id traps were hit resolving these, both worth knowing before adding a
    # fifth: a substring match on "bundesliga" returns "2. Bundesliga" (the SECOND division,
    # 308 fixtures/season and no xG), and "laliga" likewise matches "LaLiga 2". Country does
    # not disambiguate either — both siblings are in the same country. The ids below are the
    # top flights, confirmed by ranked matching (exact name wins, then shortest).
    #
    # Bundesliga and Ligue 1 both sample 5/6 rather than 6/6, and the miss is the SAME fixture
    # every season: the one played after the regular season ends (Bundesliga's relegation
    # playoff, which is why it lists 308 fixtures rather than the 306 an 18-team league plays).
    # A real, bounded gap of ~2 fixtures/season, not a coverage problem.
    #
    # Ligue 1's start season is 2022 and NOT later, despite a first probe returning 0/2 for
    # 23/24, 24/25 and 25/26: that was a sampling artifact from taking two consecutive matches
    # off the head of page 1. Sampling six spread across each season returned 5/6 every time.
    # Nearly 1,000 real fixtures would have been discarded on the narrower sample — when a
    # league's coverage appears to VANISH in later seasons, suspect the sample before the data.
    ("bundesliga", "comp_4643", "22/23", 2022),
    ("bundesliga", "comp_4643", "23/24", 2023),
    ("bundesliga", "comp_4643", "24/25", 2024),
    ("bundesliga", "comp_4643", "25/26", 2025),
    ("seriea", "comp_5840", "22/23", 2022),
    ("seriea", "comp_5840", "23/24", 2023),
    ("seriea", "comp_5840", "24/25", 2024),
    ("seriea", "comp_5840", "25/26", 2025),
    ("laliga", "comp_8814", "22/23", 2022),
    ("laliga", "comp_8814", "23/24", 2023),
    ("laliga", "comp_8814", "24/25", 2024),
    ("laliga", "comp_8814", "25/26", 2025),
    ("ligue1", "comp_0256", "22/23", 2022),
    ("ligue1", "comp_0256", "23/24", 2023),
    ("ligue1", "comp_0256", "24/25", 2024),
    ("ligue1", "comp_0256", "25/26", 2025),
    # --- Tier-1 expansion candidates (top_30_football_leagues_for_prediction.md) ------------
    # NOT yet in train_football.py's LEAGUES: these have no API-Football game log yet, so
    # resolve() skips them and only the raw cache is populated. Re-run with --resolve-only once
    # their game logs exist.
    #
    # Competition ids were matched by NAME per country against the full 150-competition list
    # (paginated 20 at a time). A "shortest name without digits" heuristic was tried first and
    # picked Sweden's SUPERETTAN over Allsvenskan and Finland's YKKOSLIIGA over Veikkausliiga --
    # both second tiers -- while excluding J1 League for containing a digit. Second-division
    # data silently labelled as top-flight would have been worse than collecting nothing.
    #
    # Start seasons are MEASURED, not assumed: two matches sampled per season (144 calls) show
    # xG begins at 2023 for the calendar-year leagues, 22/23 for the split-year ones, and
    # ONLY 25/26 for the Austrian Bundesliga. That pruned 16 of 45 seasons before fetching.
    ("allsvenskan", "comp_1002", "2025", 2025),
    ("allsvenskan", "comp_1002", "2024", 2024),
    ("allsvenskan", "comp_1002", "2023", 2023),
    ("eliteserien", "comp_1992", "2025", 2025),
    ("eliteserien", "comp_1992", "2024", 2024),
    ("eliteserien", "comp_1992", "2023", 2023),
    ("veikkausliiga", "comp_2674", "2025", 2025),
    ("veikkausliiga", "comp_2674", "2024", 2024),
    ("veikkausliiga", "comp_2674", "2023", 2023),
    ("j1_league", "comp_6240", "2025", 2025),
    ("j1_league", "comp_6240", "2024", 2024),
    ("j1_league", "comp_6240", "2023", 2023),
    ("ekstraklasa", "comp_9711", "25/26", 2025),
    ("ekstraklasa", "comp_9711", "24/25", 2024),
    ("ekstraklasa", "comp_9711", "23/24", 2023),
    ("ekstraklasa", "comp_9711", "22/23", 2022),
    ("denmark_superliga", "comp_7938", "25/26", 2025),
    ("denmark_superliga", "comp_7938", "24/25", 2024),
    ("denmark_superliga", "comp_7938", "23/24", 2023),
    ("denmark_superliga", "comp_7938", "22/23", 2022),
    ("liga_i", "comp_9639", "25/26", 2025),
    ("liga_i", "comp_9639", "24/25", 2024),
    ("liga_i", "comp_9639", "23/24", 2023),
    ("liga_i", "comp_9639", "22/23", 2022),
    ("czech_first", "comp_9766", "25/26", 2025),
    ("czech_first", "comp_9766", "24/25", 2024),
    ("czech_first", "comp_9766", "23/24", 2023),
    ("czech_first", "comp_9766", "22/23", 2022),
    # Austria: 24/25 and earlier sampled 0/2 in every season - a genuine upstream gap.
    ("austria_bundesliga", "comp_4893", "25/26", 2025),
]


def _api_key() -> str:
    """Read the key out of keys.docx at point of use. Never logged, never written to disk."""
    with zipfile.ZipFile(KEYS_DOCX) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    lines = [
        re.sub(r"<[^>]+>", "", line).strip() for line in re.sub(r"</w:p>", "\n", xml).split("\n")
    ]
    lines = [line for line in lines if line]
    for i, line in enumerate(lines):
        flat = line.lower().replace(" ", "")
        if "thestatsapi" in flat or "thestatapi" in flat or "statsapi" in flat:
            for candidate in (line, lines[i + 1] if i + 1 < len(lines) else ""):
                match = re.search(r"([A-Za-z0-9_\-\.]{24,})", candidate.split(":")[-1])
                if match and "statsapi" not in match.group(1).lower():
                    return match.group(1)
    raise RuntimeError("no TheStatsAPI key found in keys.docx")


def _normalise(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    ascii_name = ascii_name.lower().replace("&", "and").replace("-", " ")
    return " ".join("".join(c for c in ascii_name if c.isalnum() or c == " ").split())


def _similarity(a: str, b: str) -> float:
    a, b = _normalise(a), _normalise(b)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # one provider abbreviating ("Wolves" vs "Wolverhampton Wanderers") is still the same club
    return max(ratio, 0.9) if (a in b or b in a) else ratio


def _all_stat(block: dict, key: str) -> dict:
    return ((block or {}).get(key) or {}).get("all") or {}


def fetch(client: httpx.Client, path: str, **params):
    for _ in range(8):
        try:
            response = client.get(path, params=params)
        except httpx.HTTPError as exc:
            print(f"    transport error {exc!r}, retrying", flush=True)
            time.sleep(10)
            continue
        if response.status_code == 429:
            # A monthly cap and a per-minute burst limit both arrive as 429 with the same
            # Retry-After, and retrying is futile for the first. Distinguish them: a monthly
            # cap will not clear no matter how long this loop waits, and pretending otherwise
            # burns twenty minutes before the caller reports a misleading "season not found".
            body = response.text or ""
            if "USAGE_LIMIT_EXCEEDED" in body or "Monthly usage limit" in body:
                print(
                    f"    QUOTA EXHAUSTED (monthly) on {path} — stopping, retries cannot help",
                    flush=True,
                )
                return 429, None
            time.sleep(float(response.headers.get("Retry-After", 20)))
            continue
        _pace_from_headers(response.headers)
        time.sleep(REQUEST_DELAY_SECONDS)
        if response.status_code != 200:
            return response.status_code, None
        return 200, response.json()
    return 429, None


def collect_raw(leagues: list[str]) -> dict:
    """Stage 1 — one call per fixture, checkpointed. Resumes from the cache on re-run."""
    rows = json.loads(RAW_CACHE.read_text(encoding="utf-8")) if RAW_CACHE.exists() else {}
    print(f"raw cache holds {len(rows)} matches", flush=True)
    client = httpx.Client(
        base_url=BASE_URL, headers={"Authorization": f"Bearer {_api_key()}"}, timeout=60.0
    )

    def save():
        RAW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RAW_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(RAW_CACHE)

    for league, competition, label, season in TARGETS:
        if league not in leagues:
            continue
        status, body = fetch(client, f"/competitions/{competition}/seasons")
        season_id = next(
            (
                s["id"]
                for s in (body or {}).get("data", [])
                if label in str(s.get("name", "")) or label == str(s.get("year", ""))
            ),
            None,
        )
        if not season_id:
            # Say WHICH it is. These were reported identically before, and a rate-limited run
            # was nearly recorded as "Ligue 1 has no xG" — a permanent-sounding conclusion
            # drawn from a temporary failure.
            reason = (
                "request failed (quota/rate limit) — NOT a missing season"
                if status != 200
                else "season genuinely absent from this competition"
            )
            print(f"{league} {label}: no season id — {reason}", flush=True)
            continue

        matches, page = [], 1
        while True:
            status, body = fetch(
                client, "/matches", season_id=season_id, status="finished", per_page=100, page=page
            )
            if status != 200:
                break
            matches.extend((body or {}).get("data", []))
            if page >= ((body or {}).get("meta") or {}).get("total_pages", 1):
                break
            page += 1

        todo = [m for m in matches if m["id"] not in rows]
        print(
            f"\n{league} {label} (season {season}): {len(matches)} found, {len(todo)} to fetch",
            flush=True,
        )
        for i, match in enumerate(todo):
            status, body = fetch(client, f"/matches/{match['id']}/stats")
            overview = ((body or {}).get("data") or {}).get("overview") or {}
            rows[match["id"]] = {
                "league": league,
                "season": season,
                "date": match["utc_date"],
                "home_name": (match.get("home_team") or {}).get("name"),
                "away_name": (match.get("away_team") or {}).get("name"),
                "score": match.get("score") or {},
                "xg": _all_stat(overview, "expected_goals"),
                "shots": _all_stat(overview, "total_shots"),
                "shots_on_target": _all_stat(overview, "shots_on_target"),
                "big_chances": _all_stat(overview, "big_chances"),
                "corners": _all_stat(overview, "corner_kicks"),
                "possession": _all_stat(overview, "ball_possession"),
            }
            if len(rows) % CHECKPOINT_EVERY == 0:
                save()
                print(f"    {len(rows)} cached ({league} {label} {i + 1}/{len(todo)})", flush=True)
        save()
    return rows


async def _team_names(league_slug: str) -> dict[str, str]:
    """external team id -> name, for resolve()'s ambiguity tiebreak.

    The DB is only one source and is empty for any league whose fixtures have never been
    ingested — which is the normal state for a league added ahead of its season opening. That
    is not a rare case: measured across the pooled leagues, 15-23% of fixtures share a
    (date, home goals, away goals) key with another fixture, and with no names to rank on,
    every one of those is dropped by the too_weak guard. So the collected parquet
    (collect_football_data.py:_fetch_teams, same call that already fetched team codes) is
    merged in, with the DB winning where both have a name — the DB reflects whatever ingestion
    actually created, which is the identity the rest of the system uses.
    """
    names: dict[str, str] = {}
    parquet = DATA_DIR / f"football_team_names_{league_slug}.parquet"
    if parquet.exists():
        frame = pd.read_parquet(parquet)
        names.update(
            {str(t): str(n) for t, n in zip(frame["team_id"], frame["name"], strict=False)}
        )

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Team.external_id, Team.name)
                .join(League, League.id == Team.league_id)
                .where(League.slug == league_slug)
            )
        ).all()
    names.update({str(external_id): name for external_id, name in rows})
    if not names:
        print(
            f"{league_slug}: WARNING no team names from DB or parquet — ambiguous fixtures "
            f"will be dropped rather than resolved by name"
        )
    return names


def resolve(league: str, raw: dict, names: dict[str, str]) -> pd.DataFrame | None:
    """Stage 2 — join onto API-Football FIXTURE_ID/TEAM_ID by date + score, names as tiebreak."""
    game_log_path = DATA_DIR / f"football_game_log_{league}.parquet"
    if not game_log_path.exists():
        print(f"{league}: no game log to resolve against")
        return None

    candidates = [r for r in raw.values() if r["league"] == league]
    game_log = pd.read_parquet(game_log_path)
    home_rows = game_log[game_log.HOME_AWAY == "home"].copy()
    home_rows["GAME_DATE"] = home_rows["GAME_DATE"].astype(str).str[:10]

    by_date_and_score = defaultdict(list)
    for row in home_rows.itertuples():
        by_date_and_score[(row.GAME_DATE, int(row.GF), int(row.GA))].append(row)

    resolved, dropped, via_name = [], 0, 0
    for match in candidates:
        score, expected = match.get("score") or {}, match.get("xg") or {}
        # The raw cache has ALWAYS carried shots/shots-on-target/big-chances alongside xG --
        # collect_raw stored them from day one -- but this resolver used to keep only XG_FOR
        # and, worse, SKIPPED any match without xG, discarding shot data that was already paid
        # for. Requiring the score is still right (it is the join key); requiring xG was just
        # the original single-purpose scope surviving its own generalisation.
        shots = match.get("shots") or {}
        sot = match.get("shots_on_target") or {}
        big = match.get("big_chances") or {}
        has_any_stat = any(
            block.get("home") is not None for block in (expected, shots, sot, big)
        )
        if score.get("home") is None or not has_any_stat:
            continue
        date = match["date"][:10]
        found = []
        for offset in (0, -1, 1):  # kickoff can straddle midnight UTC between providers
            shifted = (pd.Timestamp(date) + pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
            found = by_date_and_score.get((shifted, int(score["home"]), int(score["away"])), [])
            if found:
                break
        if not found:
            dropped += 1
            continue
        if len(found) > 1:
            ranked = sorted(
                (
                    (
                        _similarity(match["home_name"], names.get(str(c.TEAM_ID), ""))
                        + _similarity(match["away_name"], names.get(str(c.OPPONENT_ID), "")),
                        c,
                    )
                    for c in found
                ),
                key=lambda pair: -pair[0],
            )
            too_weak = ranked[0][0] < 2 * NAME_MATCH_FLOOR
            too_close = len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < AMBIGUITY_MARGIN
            if too_weak or too_close:
                dropped += 1
                continue
            best, via_name = ranked[0][1], via_name + 1
        else:
            best = found[0]
        def stat(block: dict, side: str) -> float | None:
            value = block.get(side)
            return float(value) if value is not None else None

        resolved.append(
            {
                "FIXTURE_ID": best.FIXTURE_ID,
                "TEAM_ID": best.TEAM_ID,
                "XG_FOR": stat(expected, "home"),
                "SHOTS_FOR": stat(shots, "home"),
                "SOT_FOR": stat(sot, "home"),
                "BIG_CHANCES_FOR": stat(big, "home"),
            }
        )
        resolved.append(
            {
                "FIXTURE_ID": best.FIXTURE_ID,
                "TEAM_ID": best.OPPONENT_ID,
                "XG_FOR": stat(expected, "away"),
                "SHOTS_FOR": stat(shots, "away"),
                "SOT_FOR": stat(sot, "away"),
                "BIG_CHANCES_FOR": stat(big, "away"),
            }
        )

    if not resolved:
        print(f"{league}: nothing resolved")
        return None
    frame = pd.DataFrame(resolved).drop_duplicates(subset=["FIXTURE_ID", "TEAM_ID"])
    usable = sum(
        1
        for m in candidates
        if (m.get("score") or {}).get("home") is not None
        and (m.get("xg") or {}).get("home") is not None
    )
    print(
        f"{league}: {usable} with real xG -> {frame.FIXTURE_ID.nunique()} fixtures resolved "
        f"({100 * frame.FIXTURE_ID.nunique() / usable:.1f}%), {via_name} by name tiebreak, "
        f"{dropped} dropped as ambiguous"
    )
    return frame


async def main_async(leagues: list[str], resolve_only: bool) -> None:
    raw = (
        json.loads(RAW_CACHE.read_text(encoding="utf-8"))
        if resolve_only and RAW_CACHE.exists()
        else collect_raw(leagues)
    )
    for league in leagues:
        frame = resolve(league, raw, await _team_names(league))
        if frame is not None and not frame.empty:
            out = DATA_DIR / f"football_xg_{league}.parquet"
            frame.to_parquet(out, index=False)
            print(f"  -> {out} ({len(frame)} team-rows)")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", nargs="+", default=["epl", "brasileirao"])
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="skip fetching and re-resolve the existing raw cache",
    )
    args = parser.parse_args()
    # One asyncio.run wrapping the ENTIRE script: two separate calls in one process corrupt
    # the shared engine on Windows (see CLAUDE.md / train_nba.py).
    asyncio.run(main_async(args.leagues, args.resolve_only))


if __name__ == "__main__":
    main()
