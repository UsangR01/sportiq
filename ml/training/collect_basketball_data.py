"""Collect European basketball game logs from API-Basketball, for a TRANSFER MEASUREMENT.

WHY THIS EXISTS, AND WHAT IT IS NOT. This is not a serving pipeline and cannot become one on
the current plan. API-Basketball reaches the SAME API-Sports account the football key already
uses (confirmed live 2026-10-04: Free, 100 requests/day, valid to 2027-07-27), but Free serves
only three seasons -- asking for anything else returns, verbatim:

    "Free plans do not have access to this season, try from 2022 to 2024."

So 2022-2023 / 2023-2024 / 2024-2025 are reachable and the CURRENT season is not. Serving six
European leagues needs a paid plan. What Free buys is the one thing money cannot substitute for:
a held-out test of whether the NBA model's weights predict European basketball at all, BEFORE
paying for a subscription or building an adapter. That question is also still unanswered for the
WNBA, which has been served by NBA weights since August on an explicitly unmeasured bet.

THE OUTPUT SHAPE IS nba_api's leaguegamelog, DELIBERATELY. One row per team per game carrying
TEAM_ABBREVIATION / GAME_DATE / WL / PLUS_MINUS / SEASON / MATCHUP, because that is exactly what
app/models_ml/nba_features.py:assemble_from_game_log consumes. Reusing the real assembler rather
than reimplementing it is the whole point -- a transfer test against a reimplementation measures
the reimplementation.

MEASURED BEFORE ANY CODE WAS WRITTEN (2026-10-04), from the three free seasons:

    league          games   total pts   home win
    NBA (ours)       7220       227.1      0.552
    Spain ACB         326       166.3      0.613
    France LNB        329       161.6      0.599
    Germany BBL       336       171.7      0.571
    Greece BL         214       157.3      0.617
    Italy Lega A      268       164.6      0.649
    Turkey BSL        259       167.3      0.571

European basketball scores 27.4% fewer points than the NBA -- more than the 16.7% that
40-minute FIBA games versus 48-minute NBA games explains, so pace and efficiency differ too --
and its home advantage is 5.2pp stronger, spread over a 7.8pp range between Turkey and Italy.
Two of the NBA model's features (last10_point_diff_*, net_rating_diff) are ABSOLUTE point
differentials learned on the 227-point game, and home_court_indicator is a constant whose
learned meaning is NBA home advantage. That is the hypothesis this collection exists to test.

TWO PROVIDER TRAPS, both found by reading real payloads rather than documentation:

  - AOT ("after over time") IS A COMPLETED GAME, and it is 3.6-6.6% of a season. Mapping only FT
    would silently drop them -- the _map_status bug class CLAUDE.md records three times. Both
    are treated as final here.
  - THE SEASON LABEL HAS TWO FORMATS IN ONE PROVIDER: domestic leagues use the STRING
    "2026-2027", the Euroleague uses the INT 2026. And no season carries `current: true` on this
    plan, so api_football.py's resolve_current_season trick -- prefer the provider's own
    statement over a hardcoded convention -- has nothing to read. A serving adapter would need
    its own rule, which is precisely the class of bug that has bitten football three times.

A THIRD, recorded because it produced a wrong answer in the session that wrote this: the
`seasons` array from /leagues is UNSORTED and carries no current flag, so seasons[-1] is NOT the
latest. Read it with max(), never by position -- the same mistake already documented for
TheStatsAPI.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")  # see collect_nba_data.py for why this is explicit

from app.core.config import get_settings

BASE_URL = "https://v1.basketball.api-sports.io"
OUT_DIR = Path(__file__).resolve().parents[1] / "data"

# Exactly what the Free plan serves. Confirmed by probing both neighbours: 2021-2022 and
# 2025-2026 each return the plan error, 2022-2023 and 2024-2025 each return a full season.
DOMESTIC_SEASONS = ["2022-2023", "2023-2024", "2024-2025"]
# The Euroleague and the WNBA both label a season with a bare year rather than a span -- the
# Euroleague because the provider does, the WNBA because it genuinely runs inside one calendar
# year (May-September), the same convention split balldontlie.py's _current_season handles.
EUROLEAGUE_SEASONS = [2022, 2023, 2024]
WNBA_SEASONS = [2022, 2023, 2024]

# ids confirmed live via /leagues on 2026-10-04, every one with odds, standings and team/player
# statistics coverage true on its current season.
LEAGUE_CONFIGS: dict[str, dict] = {
    "acb": {"id": 117, "country": "Spain", "seasons": DOMESTIC_SEASONS},
    "lnb": {"id": 2, "country": "France", "seasons": DOMESTIC_SEASONS},
    "bbl": {"id": 40, "country": "Germany", "seasons": DOMESTIC_SEASONS},
    "lega_a": {"id": 52, "country": "Italy", "seasons": DOMESTIC_SEASONS},
    "greek_bl": {"id": 45, "country": "Greece", "seasons": DOMESTIC_SEASONS},
    "turkish_bsl": {"id": 104, "country": "Turkey", "seasons": DOMESTIC_SEASONS},
    # Not among the six asked for, but 3 extra calls and the strongest data candidate: its teams
    # also play their domestic leagues, so pooling the two gives a club one continuous form and
    # Elo history instead of two disjoint ones.
    "euroleague": {"id": 120, "country": "Europe", "seasons": EUROLEAGUE_SEASONS},
    # THE WNBA, from this provider rather than BallDontLie, and the reason is the measurement
    # rather than convenience. The WNBA has been served by NBA weights since August on a bet
    # CLAUDE.md records as explicitly unmeasured ("Nothing here has been measured against WNBA
    # outcomes"). Pulling it through the SAME collector as the European leagues means the same
    # instrument scores both, so the two results are directly comparable instead of being two
    # numbers produced by two code paths.
    #
    # It does NOT replace BallDontLie for serving -- that stays the live WNBA source, keeps its
    # `wnba:` external-id prefix, and is unaffected by this. API-Basketball calls it "NBA W".
    "wnba": {"id": 13, "country": "USA", "seasons": WNBA_SEASONS},
}

FINAL_STATUSES = {"FT", "AOT"}

# Free tier is 100/day; the per-minute ceiling is undocumented, so requests are paced
# proactively rather than discovered by 429. 21 calls at this spacing is about two minutes --
# the same "pace it rather than retry it" lesson collect_tennis_data.py learned expensively.
REQUEST_DELAY_SECONDS = 7.0


def _team_token(team_id: int) -> str:
    """The stand-in for nba_api's TEAM_ABBREVIATION.

    API-Basketball gives a team NAME and no abbreviation, and the name is not safe as a key --
    the same club is spelled differently across competitions. The provider's numeric id is, and
    it is one global namespace, so a club keeps one identity across its domestic league and the
    Euroleague, which is what makes pooling them meaningful.

    THE LEADING "T" IS LOAD-BEARING. _h2h_win_rate matches opponents with
    MATCHUP.str.endswith(abbr), so a bare id would let "341" match a MATCHUP ending in "2341"
    and silently attribute one team's head-to-head record to another. With the anchor, "T2341"
    does not end with "T341".
    """
    return f"T{team_id}"


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    response = client.get(path, params=params)
    response.raise_for_status()
    body = response.json()
    errors = body.get("errors")
    # API-Sports reports a plan or quota refusal with HTTP 200 plus an `errors` object, exactly
    # as api_football.py:_api_response documents. Raising stops it reading as an empty season.
    if errors:
        raise RuntimeError(f"API-Basketball refused {path} {params}: {errors}")
    return body


def collect_league(client: httpx.Client, slug: str, config: dict) -> pd.DataFrame:
    rows: list[dict] = []
    for season in config["seasons"]:
        body = _get(client, "/games", {"league": config["id"], "season": season})
        games = body.get("response") or []
        kept = 0
        for game in games:
            status = (game.get("status") or {}).get("short")
            if status not in FINAL_STATUSES:
                continue
            scores = game.get("scores") or {}
            home_total = (scores.get("home") or {}).get("total")
            away_total = (scores.get("away") or {}).get("total")
            if home_total is None or away_total is None:
                continue  # a final game with no score is unusable, and is never zero-filled
            home_team = game["teams"]["home"]
            away_team = game["teams"]["away"]
            home = _team_token(home_team["id"])
            away = _team_token(away_team["id"])
            game_date = pd.to_datetime(game["date"]).date()
            for team, opponent_token, points, opp_points, is_home in (
                (home_team, away, home_total, away_total, True),
                (away_team, home, away_total, home_total, False),
            ):
                token = _team_token(team["id"])
                rows.append(
                    {
                        "SEASON": str(season),
                        "LEAGUE": slug,
                        "GAME_ID": game["id"],
                        "GAME_DATE": game_date,
                        "TEAM_ID": team["id"],
                        "TEAM_ABBREVIATION": token,
                        "TEAM_NAME": team["name"],
                        # nba_api's own convention: "X vs. Y" for the home side and "X @ Y" for
                        # the away side. The opponent is last in both, which endswith needs.
                        "MATCHUP": f"{token} {'vs.' if is_home else '@'} {opponent_token}",
                        "IS_HOME": is_home,
                        "WL": "W" if points > opp_points else "L",
                        "PTS": points,
                        "OPP_PTS": opp_points,
                        "PLUS_MINUS": points - opp_points,
                        "WENT_TO_OT": status == "AOT",
                    }
                )
            kept += 1
        print(f"  {slug:<12} {season!s:<10} {len(games):>4} returned, {kept:>4} final")
        time.sleep(REQUEST_DELAY_SECONDS)
    return pd.DataFrame(rows)


def main() -> None:
    # --leagues exists because the budget is 100 requests/day: adding one competition should
    # cost its own 3 calls, not a re-fetch of all 24. Per-league parquets are written
    # independently, so a partial run tops up the set rather than replacing it.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leagues",
        help=f"comma-separated subset of {','.join(LEAGUE_CONFIGS)} (default: all)",
    )
    args = parser.parse_args()
    if args.leagues:
        wanted = [slug.strip() for slug in args.leagues.split(",") if slug.strip()]
        unknown = [slug for slug in wanted if slug not in LEAGUE_CONFIGS]
        if unknown:
            raise SystemExit(f"unknown league(s): {', '.join(unknown)}")
        selected = {slug: LEAGUE_CONFIGS[slug] for slug in wanted}
    else:
        selected = LEAGUE_CONFIGS

    key = get_settings().api_football_key
    if not key:
        raise SystemExit(
            "API_FOOTBALL_KEY is not set - see the .env resolution note in CLAUDE.md"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    with httpx.Client(
        base_url=BASE_URL, headers={"x-apisports-key": key}, timeout=60.0
    ) as client:
        for slug, config in selected.items():
            frame = collect_league(client, slug, config)
            if frame.empty:
                print(f"  {slug}: nothing collected")
                continue
            path = OUT_DIR / f"basketball_game_log_{slug}.parquet"
            frame.to_parquet(path, index=False)
            print(f"  -> {path.name}: {len(frame)} team-game rows")
            frames.append(frame)

    if frames:
        # Rebuilt from EVERY per-league parquet on disk, not just the ones this run fetched --
        # otherwise `--leagues wnba` would silently replace the pooled frame with one league and
        # the next measurement would score a population nobody chose.
        pooled_path = OUT_DIR / "basketball_game_log_pooled.parquet"
        parts = [
            p
            for p in sorted(OUT_DIR.glob("basketball_game_log_*.parquet"))
            if p != pooled_path
        ]
        pooled = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        pooled.to_parquet(pooled_path, index=False)
        print(
            f"\npooled: {len(pooled)} team-game rows, {pooled['GAME_ID'].nunique()} games"
        )
        print(
            json.dumps(
                pooled.groupby("LEAGUE")["GAME_ID"].nunique().to_dict(), indent=2
            )
        )


if __name__ == "__main__":
    main()
