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

Rate limit observed on this key: x-ratelimit-limit 12/min, hence REQUEST_DELAY_SECONDS —
the same proactive pacing collect_football_data.py already applies to odds.

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

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory, engine  # noqa: E402
from app.fixtures.models import Team  # noqa: E402
from app.sports.models import League  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_CACHE = DATA_DIR / "thestatsapi_xg_raw.json"
BASE_URL = "https://api.thestatsapi.com/api/football"
KEYS_DOCX = Path(__file__).resolve().parent.parent.parent / "keys.docx"

REQUEST_DELAY_SECONDS = 5.2
CHECKPOINT_EVERY = 20
NAME_MATCH_FLOOR = 0.55
AMBIGUITY_MARGIN = 0.15

# (league slug matching train_football.py's LEAGUES, competition id, season label, season int).
# EPL 21/22 is deliberately absent: measured 0/5 sampled matches carry xG, so it is a genuine
# upstream gap rather than something a re-run would fix.
TARGETS = [
    ("epl", "comp_3039", "22/23", 2022),
    ("epl", "comp_3039", "23/24", 2023),
    ("epl", "comp_3039", "24/25", 2024),
    ("epl", "comp_3039", "25/26", 2025),
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
]


def _api_key() -> str:
    """Read the key out of keys.docx at point of use. Never logged, never written to disk."""
    with zipfile.ZipFile(KEYS_DOCX) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    lines = [
        re.sub(r"<[^>]+>", "", line).strip()
        for line in re.sub(r"</w:p>", "\n", xml).split("\n")
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
    ascii_name = (
        unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    )
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
            time.sleep(float(response.headers.get("Retry-After", 20)))
            continue
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
            print(f"{league} {label}: season not found", flush=True)
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
        print(f"\n{league} {label} (season {season}): {len(matches)} found, {len(todo)} to fetch", flush=True)
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
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Team.external_id, Team.name)
                .join(League, League.id == Team.league_id)
                .where(League.slug == league_slug)
            )
        ).all()
    return {str(external_id): name for external_id, name in rows}


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
        if score.get("home") is None or expected.get("home") is None:
            continue
        date = match["date"][:10]
        found = []
        for offset in (0, -1, 1):  # kickoff can straddle midnight UTC between providers
            shifted = (pd.Timestamp(date) + pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
            found = by_date_and_score.get(
                (shifted, int(score["home"]), int(score["away"])), []
            )
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
        resolved.append(
            {"FIXTURE_ID": best.FIXTURE_ID, "TEAM_ID": best.TEAM_ID, "XG_FOR": float(expected["home"])}
        )
        resolved.append(
            {
                "FIXTURE_ID": best.FIXTURE_ID,
                "TEAM_ID": best.OPPONENT_ID,
                "XG_FOR": float(expected["away"]),
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
