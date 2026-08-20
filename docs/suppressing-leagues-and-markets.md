# Runbook: hiding leagues and withholding markets

How to stop the app recommending something, and how to put it back. Four operations, each a
one- or two-line edit in a single file, no migration and no retrain.

Every operation below changes only **what the feed leads with**. None of them delete data, and
none of them touch `/history` or `/history/summary` — a hidden league keeps counting toward
every accuracy this project reports. That separation is deliberate: withholding a
recommendation is a product choice, editing the scoreboard is not.

**The files**

| File | Owns |
|---|---|
| `backend/app/fixtures/league_availability.py` | operator decisions — whole leagues, and any market for one league |
| `backend/app/fixtures/router.py` | `NO_DEMONSTRATED_SIGNAL_MARKETS` — a market withheld everywhere |
| `backend/app/fixtures/goals_availability.py` | goals, admitted per league **by measurement** |
| `backend/app/fixtures/corners_availability.py` | corners, withheld per league **by measurement** |

The bottom two are measurement records with their own numbers and lifting conditions. **Do not
hand-edit them to express a decision** — that is what `league_availability.py` is for, and
keeping them apart is what stops a person's call being mistaken later for a measured finding.

**Market names**, exactly as the code spells them: `h2h`, `double_chance`, `goals_total`,
`corners_total`.

**Finding a league slug:** `SELECT slug FROM leagues ORDER BY slug;` — or read
`LEAGUE_IDS` in `backend/app/adapters/api_football.py`.

---

## 1. Hide a league's cards for all future games

Its undecided fixtures disappear from the feed. Settled ones keep their result and their
tick/cross, so the track record stays honest.

Edit `backend/app/fixtures/league_availability.py`:

```python
SUPPRESSED_LEAGUES = frozenset(
    {
        "mls",
        "csl",          # <-- add the slug
    }
)
```

To lift it, delete the line.

**What the user sees:** upcoming fixtures for that league vanish from the Picks feed entirely —
no card, not a card with a blank pick. Past results stay visible.

**Still works:** an explicit `GET /fixtures?league_slug=<slug>` request, `GET /fixtures/{id}`
deep links, and both history endpoints.

**Use it for:** a league whose picks are performing badly, a league mid-preseason with no real
data, a competition you do not want to recommend right now.

**Record why, next to the set.** The MLS entry carries its measured record and a stated
re-admission condition; the next one should too.

---

## 2. Withhold one market for one league

The market stops winning that league's headline pick. Another market takes over, or the fixture
shows no pick if nothing else clears the guards.

Edit `backend/app/fixtures/league_availability.py`:

```python
SUPPRESSED_MARKETS_BY_LEAGUE: dict[str, frozenset[str]] = {
    "csl": frozenset({"corners_total"}),
    "brasileirao": frozenset({"double_chance", "goals_total"}),   # several is fine
}
```

To lift it, delete the entry.

**This is the only mechanism that covers `h2h` and `double_chance`** — those have no
measurement-driven gate at all. For goals and corners you may prefer the measured files (see
"When to use which", below).

**What the user sees:** cards for that league still appear, now led by a different market.

**Not deleted:** the market still appears in `all_market_picks` and in the fixture detail's
Other Markets, and an explicit `?market=<name>` request is still honoured. Withholding a market
from the default ranking is not the same as removing it.

---

## 3. Withhold one market across every league

Edit `NO_DEMONSTRATED_SIGNAL_MARKETS` in `backend/app/fixtures/router.py`:

```python
NO_DEMONSTRATED_SIGNAL_MARKETS = frozenset({"goals_total", "corners_total"})
```

`goals_total` is already there — barred after measuring r=+0.049 against actual totals, i.e.
0.2% of variance explained.

**Read the comment block above that line before editing it.** It records what each market
measured and why corners survived the same cut that removed goals. If you are adding a market
because of a bad run rather than a measurement, say so in the comment — the two are different
kinds of claim and this file has so far only held the second.

**Per-league leagues gates still apply on top.** A league that has *earned* goals in
`goals_availability.py` will still lose it if goals is barred globally here; the global bar wins.

---

## 4. Hide a league entirely, including historic games

Every fixture disappears — upcoming and settled.

Edit `backend/app/fixtures/league_availability.py`:

```python
SUPPRESSED_LEAGUES_INCLUDING_HISTORY: frozenset[str] = frozenset({"mls"})
```

**Prefer operation 1 unless you specifically need the history gone.** Hiding a league's settled
cards removes its *losses* from view, which makes the visible track record better than reality.
That is the exact bias that once erased a published, winning Hearts v Dundee Utd card when a
completeness floor was raised after the fact, and it is why this set ships empty.

**Good reasons:** a league ingested by mistake, a competition with corrupt fixture data, a
league being retired outright.
**Not a good reason:** "its recent results look bad" — that is operation 1.

`/history` still counts the league either way, so the reported accuracy does not move. What
changes is only whether a user can see the cards.

---

## When to use which, for goals and corners

Those two markets have both a measured gate and the operator override, and picking the wrong
one loses information.

| Situation | Use |
|---|---|
| A measurement says the market has no skill in this league | the measured file (`goals_availability.py` / `corners_availability.py`), recording the numbers |
| A person decided to withhold it — bad run, product call, caution | `SUPPRESSED_MARKETS_BY_LEAGUE` |
| The market is dead everywhere | `NO_DEMONSTRATED_SIGNAL_MARKETS` |

The measured files re-derive from a training run's per-fixture parquet
(`ml/evaluation/test_predictions_<version>.parquet`). **Re-derive them after every model
promotion** — per-league skill changes with the model, and on 2026-08-19 two leagues that had
passed the goals bar three retrains earlier no longer did.

---

## Applying a change

```bash
cd backend
.venv/Scripts/python -m pytest tests/test_league_availability.py tests/test_goals_availability.py tests/test_corners_availability.py -q
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m black --check app/ tests/
git commit -am "..." && git push origin main
```

Then **deploy** — these are backend constants read at request time, so nothing changes until
the API redeploys. No migration, no retrain, no re-ingest, and no predictions are regenerated.

**Verify with a screenshot of the feed, not only the API.** An API check confirms the fixture
is gone; it does not show you what took its place, and this project has twice traded one kind
of clutter for another while the API check said the fix worked.

A quick before/after count:

```bash
curl -s "https://sportpiq-api.onrender.com/fixtures?sport_slug=football&limit=200" \
  | python -c "import json,sys;from collections import Counter;d=json.load(sys.stdin);\
print(Counter(f['league_slug'] for f in d if f['status']!='completed'))"
```

---

## Guard rails already in the tests

`backend/tests/test_league_availability.py` pins the parts that are easy to get wrong:

- operation 1's filter **must** exempt `COMPLETED` fixtures; operation 4's **must not**
- an explicit `league_slug` request keeps serving a suppressed league
- `app/history/router.py` must never import the suppression list
- both sets and the market dict default to empty/unsuppressed, so nothing drifts in silently

If you change the mechanism rather than the membership, expect those to fail — that is them
working.
