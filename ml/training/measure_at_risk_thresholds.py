"""Re-derive the at-risk thresholds against real settled fixtures (design spec §4.1).

THE QUESTION THE SPEC INSISTS ON: for each rule, how often did a pick that tripped it go on to
WIN anyway? A rule that fires on picks recovering more than half the time is noise sold as a
premium feature, and it should be loosened before launch rather than after complaints.

WHY THIS NEEDED NEW DATA. Nothing stores in-play history -- `fixture_live_state` is updated in
place every five minutes, never versioned -- so "what was the score at minute 62" is
unanswerable from our own database. API-Football's `/fixtures/events` carries goal TIMINGS,
which is enough to reconstruct the scoreline minute by minute for a match that has finished.

TWO TRAPS IN THAT FEED, both found by reading real responses rather than the docs:

  * `type == "Goal"` INCLUDES `detail == "Missed Penalty"`. Brentford v Tottenham finished 3-0
    and returns four Goal events. Counting them naively inflates the score.
  * An OWN GOAL is attributed to the team that BENEFITS FROM IT, not the one that scored it.
    I assumed the opposite and flipped it, which mis-attributed exactly one goal in every
    match containing one -- 3-0 replayed as 2-1. Settled by reading a real fixture rather than
    a convention: AIK Stockholm v Degerfors finished 2-0 with a Normal Goal AND an Own Goal
    both listed under AIK, which is only consistent with `team` meaning the credited side.

POPULATION. Every fixture contributes a hypothetical pick for every market, rather than only the
picks the model actually made. That is deliberate: the recovery rate of a scoreline rule is a
property of FOOTBALL, not of our model, and we hold only a few hundred settled real picks
against tens of thousands of fixtures. The caveat is that our real picks skew toward favourites,
so a rule's real-world recovery may differ from the population figure -- reported, not hidden.

RESULT, 2026-08-23, on 4,001 replayed fixtures (1.0% dropped for failing the integrity check):

    pick                   fires   wrong  caught  median
    double_chance:1X       35.6%   15.2%   98.8%     75'
    double_chance:X2       49.7%   11.7%   98.7%     75'
    goals over 1.5         43.0%   42.4%  100.0%     55'
    goals over 2.5         71.4%   32.1%  100.0%     55'
    goals over 3.5         88.6%   20.4%  100.0%     55'
    goals under 1.5        87.8%   16.0%   98.0%     23'
    goals under 2.5        62.2%   22.6%   93.5%     45'
    goals under 3.5        34.8%   26.9%   86.2%     54'
    h2h:away               51.1%    2.3%   71.9%     70'
    h2h:draw               84.6%   12.0%   99.1%     75'
    h2h:home               37.5%    4.4%   64.6%     70'

VERDICT: NO THRESHOLD CHANGED. Nothing crosses the spec's 50%-recovery bar, so by the criterion
fixed before the run, every rule stands. Adjusting them anyway on the strength of numbers seen
afterwards is exactly the post-hoc fitting a pre-registered bar exists to prevent.

TWO THINGS THE SPEC'S BAR DOES NOT CATCH, recorded rather than acted on:

  * `goals over 1.5` is wrong 42.4% of the time -- comfortably inside the bar but the closest
    to it, and nearly half its warnings land on picks that go on to win.
  * Three rules fire on 85-89% of fixtures (over 3.5, under 1.5, draw). Accurate and nearly
    constant: there is no signal in a warning you almost always get.

Both are contained by what actually reaches a card. `goals_total` is barred from the headline
pick (NO_DEMONSTRATED_SIGNAL_MARKETS) and no draw pick has ever reached one, so the markets
users can really save -- double chance and h2h -- are the well-behaved rows: firing on 36-51%,
wrong 2-15%, catching 65-99% of losses, at a median of 70-75'. Against the 85' alert cutoff that
leaves a 10-15 minute window to act, which is narrow but real.

    python ml/training/measure_at_risk_thresholds.py --collect --limit 4000
    python ml/training/measure_at_risk_thresholds.py --measure
"""

import argparse
import asyncio
import glob
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")

import pandas as pd  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.predictions.live_risk import PickState, evaluate  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EVENTS_PATH = DATA_DIR / "football_goal_events.parquet"

#: API-Football's Ultra plan allows far more, but a burst is what produced the
#: 429-then-spurious-401 cascade recorded for other providers here, and this has no deadline.
REQUEST_DELAY_SECONDS = 0.15

#: Goal events that did not change the score.
_NON_SCORING_DETAILS = frozenset({"Missed Penalty"})

#: The lines the product actually sells, so the measurement matches what ships.
GOALS_LINES = (1.5, 2.5, 3.5)


async def collect(limit: int) -> None:
    """Fetch goal timings for settled fixtures, caching so a re-run costs nothing."""
    fixtures = _fixtures_to_collect(limit)
    if not fixtures:
        print("nothing new to collect")
        return

    headers = {"x-apisports-key": get_settings().api_football_key}
    rows: list[dict] = []
    async with httpx.AsyncClient(
        base_url="https://v3.football.api-sports.io", headers=headers, timeout=40
    ) as client:
        for index, fixture_id in enumerate(fixtures, start=1):
            try:
                response = await client.get("/fixtures/events", params={"fixture": fixture_id})
                events = response.json().get("response", [])
            except httpx.HTTPError as exc:
                print(f"  {fixture_id}: {type(exc).__name__} - skipped")
                continue
            for event in events:
                if event.get("type") != "Goal":
                    continue
                detail = event.get("detail") or ""
                if detail in _NON_SCORING_DETAILS:
                    continue
                elapsed = (event.get("time") or {}).get("elapsed")
                if elapsed is None:
                    continue
                rows.append(
                    {
                        "FIXTURE_ID": int(fixture_id),
                        "MINUTE": int(elapsed) + int((event.get("time") or {}).get("extra") or 0),
                        # The team the goal COUNTS FOR, including own goals.
                        "SCORING_TEAM_ID": int((event.get("team") or {}).get("id") or 0),
                        # Recorded but not acted on -- kept so the cache preserves what the
                        # provider said rather than only our reading of it.
                        "OWN_GOAL": detail == "Own Goal",
                    }
                )
            # A 0-0 match legitimately produces no rows, so record that it was fetched or every
            # future run would re-request it forever.
            rows.append({"FIXTURE_ID": int(fixture_id), "MINUTE": -1,
                         "SCORING_TEAM_ID": 0, "OWN_GOAL": False})
            if index % 200 == 0:
                print(f"  {index}/{len(fixtures)} fixtures")
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

    frame = pd.DataFrame(rows)
    if EVENTS_PATH.is_file():
        frame = pd.concat([pd.read_parquet(EVENTS_PATH), frame], ignore_index=True)
    frame = frame.drop_duplicates()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(EVENTS_PATH, index=False)
    print(f"cached {frame['FIXTURE_ID'].nunique()} fixtures to {EVENTS_PATH.name}")


def _game_log() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(glob.glob(str(DATA_DIR / "football_game_log_*.parquet")))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fixtures_to_collect(limit: int) -> list[int]:
    log = _game_log()
    if log.empty:
        return []
    # Sampled across the whole pool rather than taking the newest, so one league or season
    # cannot dominate the recovery rates.
    ids = sorted(log["FIXTURE_ID"].dropna().astype(int).unique())
    already: set[int] = set()
    if EVENTS_PATH.is_file():
        already = set(pd.read_parquet(EVENTS_PATH)["FIXTURE_ID"].astype(int).unique())
    remaining = [i for i in ids if i not in already]
    if len(remaining) <= limit:
        return remaining
    step = len(remaining) / limit
    return [remaining[int(i * step)] for i in range(limit)]


def _fixture_frame() -> pd.DataFrame:
    """One row per fixture: home/away team ids and the final score, from the game log."""
    log = _game_log()
    home = log[log["HOME_AWAY"] == "home"][["FIXTURE_ID", "TEAM_ID", "GF", "GA"]]
    home = home.rename(columns={"TEAM_ID": "HOME_ID", "GF": "HOME_GOALS", "GA": "AWAY_GOALS"})
    away = log[log["HOME_AWAY"] == "away"][["FIXTURE_ID", "TEAM_ID"]]
    away = away.rename(columns={"TEAM_ID": "AWAY_ID"})
    return home.merge(away, on="FIXTURE_ID").drop_duplicates("FIXTURE_ID")


def _timeline(events: pd.DataFrame, home_id: int, away_id: int) -> list[tuple[int, int, int]]:
    """(minute, home_score, away_score) after each goal, in order."""
    scored = events[events["MINUTE"] >= 0].sort_values("MINUTE")
    home = away = 0
    out: list[tuple[int, int, int]] = []
    for _, row in scored.iterrows():
        # No flip for an own goal: the provider already names the team the goal COUNTS FOR.
        # See the module docstring -- flipping it here cost one goal per affected match and
        # silently dropped 7.5% of the sample, biased toward high-scoring games.
        if int(row["SCORING_TEAM_ID"]) == home_id:
            home += 1
        else:
            away += 1
        out.append((int(row["MINUTE"]), home, away))
    return out


#: Every pick a fixture could carry, and how to tell whether it won at full time.
def _candidates(final_home: int, final_away: int):
    total = final_home + final_away
    picks = [
        ("h2h", "home", None, final_home > final_away),
        ("h2h", "away", None, final_away > final_home),
        ("h2h", "draw", None, final_home == final_away),
        ("double_chance", "1X", None, final_home >= final_away),
        ("double_chance", "X2", None, final_away >= final_home),
    ]
    for line in GOALS_LINES:
        picks.append(("goals_total", "under", line, total < line))
        picks.append(("goals_total", "over", line, total > line))
    return picks


def measure() -> None:
    if not EVENTS_PATH.is_file():
        print("no cached events - run with --collect first")
        return
    events = pd.read_parquet(EVENTS_PATH)
    fixtures = _fixture_frame()
    covered = set(events["FIXTURE_ID"].astype(int).unique())
    fixtures = fixtures[fixtures["FIXTURE_ID"].astype(int).isin(covered)]
    print(f"replaying {len(fixtures)} settled fixtures\n")

    by_fixture = {int(k): v for k, v in events.groupby("FIXTURE_ID")}
    stats: dict[tuple, dict] = {}
    skipped: list[int] = []

    for _, fixture in fixtures.iterrows():
        fid = int(fixture["FIXTURE_ID"])
        final_home, final_away = int(fixture["HOME_GOALS"]), int(fixture["AWAY_GOALS"])
        timeline = _timeline(by_fixture[fid], int(fixture["HOME_ID"]), int(fixture["AWAY_ID"]))
        rebuilt = (timeline[-1][1], timeline[-1][2]) if timeline else (0, 0)
        if rebuilt != (final_home, final_away):
            # THE INTEGRITY CHECK, and it is not optional. If replaying the events does not
            # reproduce the score the game log recorded, this fixture's timings are incomplete
            # or misattributed -- and a half-reconstructed match would quietly bias every rule
            # toward "the pick was fine". Dropped, and counted so the loss is visible.
            skipped.append(fid)
            continue

        for market, selection, line, won in _candidates(final_home, final_away):
            key = (market, selection, line)
            entry = stats.setdefault(
                key, {"tripped": 0, "recovered": 0, "minutes": [], "n": 0, "lost": 0, "warned": 0}
            )
            entry["n"] += 1
            if not won:
                entry["lost"] += 1
            trip_minute = _first_trip_minute(market, selection, line, timeline)
            if trip_minute is None:
                continue
            entry["tripped"] += 1
            entry["minutes"].append(trip_minute)
            if won:
                entry["recovered"] += 1
            else:
                entry["warned"] += 1

    if skipped:
        print(
            f"dropped {len(skipped)} fixtures whose replayed goals did not reproduce the "
            f"recorded final score ({len(skipped) / max(len(fixtures), 1):.1%})\n"
        )
    _report(stats)


def _first_trip_minute(market, selection, line, timeline) -> int | None:
    """The earliest minute the rule would have fired, replaying minute by minute.

    Evaluated on every minute of the match rather than only at goals, because several rules are
    triggered by the CLOCK passing a threshold while the score stands still -- one goal down at
    69' is fine and at 70' is not.
    """
    home = away = 0
    index = 0
    for minute in range(1, 96):
        while index < len(timeline) and timeline[index][0] <= minute:
            home, away = timeline[index][1], timeline[index][2]
            index += 1
        state = evaluate(
            sport_slug="football",
            market=market,
            selection=selection,
            line=line,
            home_score=home,
            away_score=away,
            match_minute=minute,
        )
        if state is PickState.AT_RISK:
            return minute
    return None


def _report(stats: dict) -> None:
    """Four numbers per rule, because recovery alone cannot say which way to move a threshold.

      fires   how often the rule trips at all. A rule that fires on most fixtures is noise even
              when it is usually right -- there is no signal in a warning you always get.
      wrong   the spec's own bar: how often a pick it warned about went on to WIN anyway.
              Above 50% the alert is crying wolf and the rule should be loosened.
      caught  of the picks that actually LOST, how many did it warn about. A rule can be almost
              never wrong simply by almost never firing, and this is what exposes that.
      median  when it fires. An accurate warning at 88' is not a product.
    """
    print(
        f"{'pick':24} {'n':>6} {'fires':>7} {'wrong':>7} {'caught':>7} {'median':>7}   verdict"
    )
    print("-" * 84)
    concerning, toothless = [], []
    for (market, selection, line), entry in sorted(stats.items()):
        if not entry["tripped"]:
            continue
        label = f"{market}:{selection}" + (f" {line:g}" if line is not None else "")
        fires = entry["tripped"] / entry["n"]
        wrong = entry["recovered"] / entry["tripped"]
        caught = entry["warned"] / entry["lost"] if entry["lost"] else 0.0
        median = int(pd.Series(entry["minutes"]).median())

        verdict = "ok"
        if wrong > 0.5:
            verdict = "CRIES WOLF"
            concerning.append((label, wrong, entry["tripped"]))
        elif caught < 0.5:
            verdict = "misses most losses"
            toothless.append((label, caught))
        print(
            f"{label:24} {entry['n']:6} {fires:7.1%} {wrong:7.1%} {caught:7.1%} "
            f"{median:6}'   {verdict}"
        )

    print()
    if concerning:
        print("LOOSEN THESE -- they warn about picks that win more often than not:")
        for label, wrong, n in concerning:
            print(f"  {label:24} {wrong:.1%} of warnings were wrong (n={n})")
    else:
        print("No rule warns about picks that recover more than half the time.")
    if toothless:
        print("\nQuiet, not wrong -- these miss more than half of real losses:")
        for label, caught in toothless:
            print(f"  {label:24} catches {caught:.1%} of losses")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--limit", type=int, default=4000)
    args = parser.parse_args()
    if args.collect:
        asyncio.run(collect(args.limit))
    if args.measure or not args.collect:
        measure()
