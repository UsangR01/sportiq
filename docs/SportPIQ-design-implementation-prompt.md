# SportPIQ v5 — implementation prompt for Claude Code

Build the SportPIQ mobile app UI exactly as specified below. This is a **sports-prediction app**: a model publishes probability-backed picks, and a Premium tier surfaces where the model disagrees with bookmaker odds. Nigeria is the primary market (₦ pricing).

Target: **React Native (Expo)** or **React + TypeScript** web/PWA — pick one and stay consistent. Design canvas is **390 × 844** (iPhone 14/15 logical size). Every value below is a real spec, not a suggestion; match it.

---

## 1. Foundations

### 1.1 Theme tokens

Two themes, switched by a single `darkMode` boolean. Expose as a `theme` object; never hardcode a colour at a call site.

| token | light | dark |
|---|---|---|
| `bg` | `#f5f6f8` | `#0f1216` |
| `surface` | `#ffffff` | `#171b21` |
| `surfaceAlt` | `#eef1f5` | `#1e232b` |
| `border` | `#e2e5ea` | `#262c35` |
| `mutedBg` (track fills) | `#eef0f3` | `#1c2027` |
| `text` | `#14171c` | `#f2f4f7` |
| `textSub` | `#6b7280` | `#9aa3af` |
| `textFaint` | `#9aa1ab` | `#6b7280` |
| `accent` | `#2f5dfb` | `#5b82ff` |
| `accentSoft` | `#eaf0ff` | `#1c2540` |
| `premiumBg` | `#152046` | `#232a55` |
| `success` | `#1f9d55` | `#3ecb7e` |
| `successSoft` | `rgba(31,157,85,0.1)` | `rgba(62,203,126,0.14)` |
| `fail` | `#e2402f` | `#ff6b5b` |
| `failSoft` | `rgba(226,64,47,0.1)` | `rgba(255,107,91,0.14)` |
| `warn` | `#b8720a` | `#e0a458` |
| `star` (favourite, both themes) | `#f5b715` | `#f5b715` |

`shadow`
- light: `0 1px 2px rgba(20,23,28,0.06), 0 4px 12px rgba(20,23,28,0.04)`
- dark: `0 1px 2px rgba(0,0,0,0.4), 0 4px 16px rgba(0,0,0,0.3)`

Dropdown/popover shadow: `0 12px 32px rgba(0,0,0,0.2)`; calendar popover `0 14px 36px rgba(0,0,0,0.22)`; bottom sheet `0 -12px 36px rgba(10,13,20,0.26)`; scrim `rgba(10,13,20,0.5)`.

Links use `accent`; hover `#234ad6` (light).

### 1.2 Type

System stack: `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif`. No web fonts.

| role | size / weight / tracking |
|---|---|
| Screen wordmark "SportPIQ" | 26 / 800 / -0.025em (24 / 800 / -0.03em on Top calls) |
| Section eyebrow (all caps) | 10 / 800 / 0.1em, `textFaint` |
| Small eyebrow in cards | 9.5 / 800 / 0.08em, `textFaint` |
| Summary card value | 20 / 800 / -0.035em |
| Big stat (model %) | 22 / 800 / -0.03em |
| Team name | 14.5 / 600 / -0.02em (winner 800 when settled) |
| Score | 14.5 / 800 |
| Pick label | 13 / 700 / -0.01em |
| Body | 13–13.5 / 400, `textSub` |
| Caption | 11–12 / 600, `textFaint` |
| Tab label | 11 / 600 |

All numerals: **tabular-nums** (`fontVariantNumeric: 'tabular-nums'`). Long strings (team, league, match) truncate with ellipsis on one line.

### 1.3 Geometry

Screen padding **18px**. Radii: cards & sheets `18` (list card) / `14` (small card, control) / `12` (button, expanded panel) / `11–10` (chip) / `9` (icon button, segment thumb) / `22 22 0 0` (bottom sheet top). Icon buttons `36×36` r12; date arrows `40×40` r14; toggles `46×28` with a `24` white knob (offset 2 → 20). Card vertical gap `10`; league group gap `22`; header control gap `10`.

Screen top padding **60px** (58 on overlays) to clear the status bar; bottom tab bar padding-bottom **22px** for the home indicator.

---

## 2. App shell

Four tabs in a bottom bar on `surface` with a 1px `border` top edge, `padding: 8px 8px 22px`, tabs distributed evenly. Each tab: 22px glyph above an 11/600 label, `accent` when selected, `textFaint` otherwise.

| tab | label | glyph |
|---|---|---|
| picks | Picks | concentric rings (22px circle, 2px border, inner 10px circle) |
| live | Live | three ascending 3px bars (9/15/21px) |
| premium | Top calls | 14px square rotated 45°, r3, filled |
| profile | Profile | head-and-shoulders outline (9px circle + 16×8 r8-8-0-0 shape) |

Two full-screen overlays sit above the tabs: **Hub** (z 49) and **Paywall** (z 53). Bottom sheets sit at z 44 (scrim) / 45 (panel).

**Saved picks do NOT get a fifth tab.** The bar stays at four. Saving is initiated from the
expanded match panel (§3.2) and the saved list is reached from the Hub's `SAVED` block (§7.1),
which becomes tappable rather than a bare count. See §6.5 for that screen.

> **This supersedes a shipped Saved tab.** A fifth tab currently exists in the app
> (Picks · Live · Saved · Profile). Adopting this design removes it and re-homes the list under
> the Hub. Flagged rather than silently dropped: the screen and its API layer already work, so
> this is a re-parent, not new work — but the `premium` tab replaces it in the bar, so the two
> cannot both be reached from the tab bar.

---

## 3. Picks screen

### 3.1 Header (fixed, non-scrolling)

Row 1: hamburger icon button (two 15×1.8px bars, opens Hub) · wordmark · right cluster of `PRO` pill (only when subscribed: `accentSoft` bg, `accent` text, 10/800/0.08em), theme toggle (`☾`/`☀`), and a **Filters** button (`surfaceAlt`, 12.5/700) carrying a 5px `accent` dot when any filter is off-default.

Row 2 — **date stepper**: `‹` 40×40 · centred pill showing `Today` or `Thu, Jul 31` (14/700) with a chevron · `›` 40×40. All on `surface` with 1px `border`, r14.

Tapping the centre opens a **calendar popover** (absolute, full width, r16, 14px padding): month name with `‹ ›` 28×28 buttons, Mo–Su header row (11/700 `textFaint`), a 7-column grid of square r10 day cells (selected = `accent` bg, white 800; days with fixtures = `text` 700 plus a 3px `accent` dot at the bottom; empty days = `textFaint` 500), and a full-width `TODAY` button in `accent`.

Row 3 — **summary strip**, one `surface` card r14, `13px 4px` padding, three equal columns divided by 1px `border` (first divider transparent). Each column: a fixed 24px-tall eyebrow then a 20/800 value.
1. `CALLS TODAY` — count of visible picks
2. `LEAGUES` — count of visible league groups
3. `COUNTRY` — **interactive**: the value reads `All` in `text` or the selected country in `accent`, with a small chevron. Tapping opens a popover anchored to the strip's right edge (min-width 210, r14) listing `All countries` plus every country with fixtures that day — each row a country-code badge (`surfaceAlt`, r6, 10/800), the country name (14/600), and its match count (12/700 `textFaint`). Selected row uses `accentSoft` bg and `accent` text. Rows separated by 1px `border` (first transparent). Selecting filters the feed to that country **across all sports**; the list is derived from the day's data, not hardcoded.

Row 4 — **segmented control**: `surfaceAlt` r12 with 3px padding; three equal r9 segments `All` / `Upcoming` / `Finished`; the active one lifts to `surface` with the card shadow and `text` colour, the rest are `textSub`.

### 3.2 Feed (scrolls)

Optional sign-up banner (hidden for Premium): `accentSoft` r12, 13px copy left, bold `Sign up` link right.

Then league groups, `22px` apart. Each group has a **header row** outside the card: country-code badge (`surfaceAlt` r6, 10/800, min-width 20, centred) · league name (14/700) over country (12 `textFaint`) · a **favourite star button** 30×30 on the right — `★` in `#f5b715` when starred, `☆` in `textFaint` otherwise.

**Favourites behave at league level, not match level:**
- starring a league persists it in `favourites`
- starred leagues are **always pinned to the top** of the day's list whenever they have fixtures; the original order is preserved within the starred band and within the rest
- the Hub's Favourites tile filters the feed to starred leagues only

Below the header, one `surface` card r18 with 1px `border` and card shadow contains all that league's matches; **matches are rows inside the shared card**, separated by a 1px `border` top edge (first row's separator transparent), not individual cards.

Each match row:
- **Tappable summary block** (`15px 16px`): status eyebrow (10/800/0.07em — `accent` for a kick-off time, `textFaint` for `FULL-TIME`, `warn` for `POSTPONED`), plus a 15px result disc when settled (`success` with `✓` or `fail` with `✕`, glyph in `surface` colour). Beneath, two lines: team name left (winner at 800 when settled), score right at 14.5/800. A chevron on the right rotates 45° → 225° when expanded.
- **Pick line** (`0 16px 15px`): pick label (13/700) above a 4px r2 `mutedBg` track filled to the probability — fill is `success` if the pick won, `fail` if it lost, `accent` while pending. Right column: probability at 15/800 in the same colour, odds beneath at 11/600 `textFaint` (formatted per the user's odds format).
  - **`was {n}%` caption**, 10/600 `textFaint`, sits under the odds **only when the call has
    materially moved** (server sends `previous_probability`; it is null otherwise). Measured on
    real data this appears on roughly 2–3% of cards, which is what makes it worth reading — a
    badge on every card is wallpaper. Never render a placeholder when it is null.
  - **Limited-data treatment.** When `feature_completeness < 0.35` the probability renders in
    `textSub` rather than its status colour, and a 10/600 `warn` caption reads `limited data`.
    Worded as a limitation of the DATA, not a hedge on the number: the probability is genuinely
    what the model says, it simply had little to go on. The server already refuses to publish a
    pick below its own floor, so this band is narrow by construction.
- **Postponed variant**: replace the pick line with a `surfaceAlt` r10 strip reading `RESCHEDULED — NO PICK` (11/700/0.06em `textFaint`, centred).
- **Expanded panel**: `surfaceAlt` r12, 14px padding, eyebrow `WHY THE MODEL CALLED IT`, then three factor rows — 112px label (12/600 `textSub`), a 6px r3 `mutedBg` track with an `accent` fill, and a right-aligned 12/700 value. Ends with a full-width action button: **Save this pick** in `accent`/white, or **Remove from saved picks** in `mutedBg`/`textSub` when already saved.
  - **Provenance line**, directly under the factor rows and above the button: `CALLED` eyebrow
    with the pick's `as_of` timestamp right-aligned, 11/600 `textFaint` — time alone if it was
    generated today, otherwise a short date, so a call that has not been refreshed for days says
    so rather than showing a bare time that reads as recent.
  - **Why this is here at all:** the pick is recomputed on every request and never stored, so a
    card can legitimately read differently between visits. That was reported twice as the app
    changing its mind overnight. The churn is mostly the market arriving — freezing the feed
    would mean knowingly showing a stale number — so the design answer is to stop presenting a
    moving estimate as timeless.
  - **Saving freezes the call.** Tapping Save records the pick AS SHOWN, server-side; the saved
    list renders that receipt and must never re-rank or recompute it (§6.5).
  - **Sub-caption under the button** when not yet saved, 11/600 `textFaint`:
    `Keeps this pick exactly as it is now, even if it changes later.`

Empty state: 15/700 `No picks match your filters`, then a context-aware reason — favourites-only, country-specific, or threshold-based — and a `Reset filters` outline button.

### 3.3 Filter sheet

Bottom sheet, `surface`, r22 top, `14px 18px 26px`: 34×4 grab handle, title `Filters` (17/800) with a `Reset` text button, then
1. **SPORT** — wrapping chips `All` / `Football` / `NBA Basketball` / `Tennis` (34px, r11, selected = `accent`/white, else `surface` + border)
2. **TOUR / LEAGUE** — appears only for sports with sub-competitions, indented behind a 2px `mutedBg` left rule: Tennis → `ATP` / `WTA`; NBA Basketball → `NBA` / `WNBA`. Tapping a selected chip clears it.
3. **Minimum probability** — 0–100, 5px r3 track, `accent` fill, a 22px white thumb with a 4px `accent` ring; value shown at 17/800
4. **Minimum odds** — 1.01–3.00 step 0.01, same treatment, label formatted per odds format
5. Primary button: `Show {n} matches`

---

## 4. Live screen

Wordmark + a 7px `fail` dot, and `{n} matches` right-aligned in 12/600 `textFaint`.

Each live match is its own `surface` card r18, 16px padding, card shadow, 13px internal gaps:
- `fail` dot + minute (10/800/0.08em `fail`) · league right (10/700/0.07em uppercase `textFaint`)
- two team/score lines (14.5/600 and 14.5/800)
- a 3px r2 `mutedBg` progress track filled `fail` to match elapsed time
- pick label (12.5/700) with a status tag right: `ON TRACK` on `successSoft`/`success` or `AT RISK` on `failSoft`/`fail` (9.5/800/0.08em, r6, `4px 7px`)

---

## 5. Top calls screen (Premium)

The idea: show the model's probability beside the **de-vigged market probability** implied by the odds, and make the gap between them the hero. Never multiply legs, never quote a stake return, never show a booking code — this screen is analysis, not a bet slip.

### 5.1 Header (fixed, closed by a 1px `border` rule)

- Wordmark 24/800/-0.03em, with a plain `PREMIUM` eyebrow right-aligned (10/800/0.14em, `accent`, **no badge fill**)
- **Calibration block on plain background — not a tinted card**: eyebrow `CALIBRATION` with `Updated 12 min ago` right in 10.5 `textFaint`; the sentence *"When we say **70%**, it happens **68%** of the time."* at 13.5 with both figures at 800; then 11.5 `textFaint` *"Across our last 412 published calls"*
- Four calibration bands side by side: a 2px `mutedBg` rail filled in `text` to the actual rate, then `{actual}` (11/700) `/ {stated}` (10 `textFaint`). Caption: `Actual hit rate against what we said, by band`

### 5.2 Stance tabs

Underline tabs, 20px apart: **Agreed** (gap ≤ 4 pts) · **Watch** (5–12) · **We disagree** (> 12), each with a live count beside the label. Selected = 14/800 `text` with a 2px `accent` underline; others 14/600 `textFaint`, transparent underline. Beneath, a 12 `textSub` sentence describing the selected band:
- Agreed — "Calls where our model and the odds land in roughly the same place."
- Watch — "A modest gap — worth following, not worth leaning on."
- We disagree — "The widest gaps between our model and the market price."

Default view: **We disagree**.

### 5.3 Gap legend

One row: an 18×8 r3 swatch of the **model gradient** labelled `Our model`, an 18×8 swatch of the **odds gradient** labelled `Odds imply`, and `gap in the middle` right-aligned. Below, 11 `textFaint`: *"Each bar splits by share of belief — the coloured middle is the overhang, leaning to whichever side is higher."*

Gradients (left→right):
- model — light `hsl(248 82% 48%) → hsl(202 88% 46%)`, dark `hsl(248 85% 62%) → hsl(202 90% 56%)` (indigo → cyan)
- odds — light `hsl(44 90% 54%) → hsl(26 82% 50%)`, dark `hsl(46 92% 62%) → hsl(26 85% 58%)` (amber → orange)

### 5.4 Call rows

Hairline-separated rows (1px `border` top, `16px 18px`) — **not cards, not inside a card wrapper**.

- **Left column**: eyebrow `{kick-off} · {league}` (11/700/0.06em `textFaint`), plus a `WIDEST GAP` outline tag (9.5/800/0.1em, 1px border, r5) on the leading row when its gap exceeds 4 points; match name 15/600/-0.02em; pick 13 `textSub`
- **Right column** (fixed 86px, right-aligned): model probability 22/800/-0.03em, and `odds say {n}%` beneath at 11.5 `textFaint`

- **The gap bar** — a single row of **20 cells**, each `flex:1`, 18px tall, r5, 3px apart, `16px 0 12px`:
  - the row splits by share of belief: `leftN = round(20 × model / (model + market))`, clamped to 2…18; `rightN = 20 − leftN`
  - the left cells step through the **model gradient**, the right cells through the **odds gradient** — interpolate each ramp in HSL across its own cell count so both sides read as continuous
  - the **overhang** — `gapN = clamp(round(|gap| / 4), 1, 5)` cells — is carved out of the **favoured side's own field** at the boundary, painted flat `success` when the model is higher and flat `fail` when the market is higher. That makes the coloured middle sit slightly left when we're higher and slightly right when the odds are higher.

- **Caption row**: `{n} points higher/lower` at 700 in `success`/`fail`, followed by plain `than the odds` in 12 `textSub`; a plain-text **Why** affordance right-aligned in `accent` 12/700 that toggles to **Close**

- **Expanded panel**: 1px `border` container, r12, 16px padding. Eyebrow `WHAT IS DRIVING THIS`, then a plain-language sentence — *"We put this at 88% while the odds imply 74%. The biggest single reason is recent form — strong recent run."* Then hairline-separated label/value rows (13 `textSub` left, 13/700 right) — **words, not bars**: Recent form · Goals per game · Head-to-head · Rest and rotation · How sure we are. **The wording must agree with the direction of the gap** — favourable phrasing when the model is higher, unfavourable when lower, neutral inside ±3. Close with the caveat at 11.5 `textFaint`: *"A gap can mean we have spotted something the odds have missed. It can also mean the market knows something our model does not."*

### 5.5 Gating & empty state

Free users see **one** call in the selected band; Premium sees five. Below the list, free users get `{n} more calls in this view are held for members.`, the `WHAT PREMIUM ADDS` list (accent `✓` + 13.5 copy), and an `Unlock with Premium` button. If a band is empty: 15/700 `Nothing in this view` and *"No upcoming call sits in this band right now. Try another view."*

**Colour discipline on this screen:** `success` and `fail` appear only on the overhang cells and the coloured caption fragment. No tinted banner backgrounds anywhere.

---

## 6. Profile screen

Wordmark, then an account row: 56px circular avatar (`accentSoft` bg, `accent` initial, 22/800) · `Guest` (17/700) over the membership label (13 `textSub`) · a `Sign up` chip (`accentSoft`, `accent`, r10).

Then a single `surface` r14 card of three rows divided by 1px `border`:
1. **Dark mode** with a toggle
2. **Kick-off alerts** with a 12 `textFaint` sub-line and a toggle. **The sub-line must state the real interval**: the implemented reminder fires at **T-60 minutes**, so it reads "60 min before saved picks" unless the worker's schedule is changed to match the design. Do not ship copy that promises a timing the backend does not implement.
3. **Preferences & more** with a chevron — opens the Hub

Nothing else on this screen: no hit-rate card and no stat tiles.

### 6.5 Saved picks (reached from the Hub, not a tab)

Same shell as any screen: wordmark, 18px padding, `bg` background. Header row is the wordmark
with `{n} saved` right-aligned, 12/600 `textFaint`.

**This screen renders a RECEIPT, and that is its entire purpose.** It shows the pick as it was
when the user saved it — market, selection, line, probability and odds — captured server-side at
save time. It must **not** re-rank, re-price, or recompute anything. A saved card that quietly
updates itself is the exact defect this screen exists to answer.

Two groups, `22px` apart, each with a section eyebrow: **UPCOMING** then **FINISHED**. Finished
picks stay visible with their result. Do not hide them: hiding settled cards deletes the losses
and makes the visible track record better than it was, which is the same bias that once erased a
published, winning pick when a guard was tightened after the fact.

Each row is a `surface` card r14, 1px `border`, 14px padding, 10px gap:
- eyebrow row — league name (10/800/0.07em `textFaint`) left, kick-off right (11/600
  `textFaint`, or `Time TBC` when the kick-off is only an estimate)
- two team lines, 14.5/600
- **receipt block** — `YOU SAVED` eyebrow (9.5/800/0.08em `textFaint`) over the pick at 13/700,
  reading `{pick} · {prob}% · {odds}`, then `on {date}` at 10/600 `textFaint`
- a `Remove` text button right-aligned, `textSub`, in a 1px `border` r10 pill
- when settled, the 15px result disc (`success ✓` / `fail ✕`) sits in the eyebrow row

**Rows with no recorded pick** — saved before the receipt existed, or saved on a fixture whose
pick did not clear the guards — read `Saved · no pick recorded` at 12/600 `textFaint`. Never
invent a pick to fill the slot.

**Guest state**: `Sign in to save picks` at 15/700 over 13 `textSub` copy — *"Saving keeps a pick
exactly as you saw it, even if the odds move afterwards — and lets us remind you before
kick-off."* — plus a `Sign in` button in `accent`. Saving is account-only by design: a guest
session is device-bound state with a 24h TTL, while a saved pick is durable and drives a push.

**Empty state**: `Nothing saved yet` at 15/700 over *"Open a match, expand it, and tap Save to
keep its pick exactly as it is now."*

---

## 7. Hub (full-screen overlay)

Top bar: `SPORTPIQ · {membership}` (11/800/0.14em `textFaint`) and a `×` close button.

1. **Identity block** — `Guest` at 28/800/-0.035em with `Sign in to sync picks →` beneath; right side shows a `SAVED` eyebrow over the saved-pick count at 26/800. **The saved block is tappable** and opens the saved-picks screen (§6.5) — it is the only route to it, since there is no Saved tab. Give it a pressed state so it reads as a control rather than a statistic.
2. **Premium feature card** — `premiumBg` r16, 18px padding, two decorative concentric circle outlines (`rgba(255,255,255,0.16)`) bleeding off the top-right corner. Inside: a translucent `PREMIUM` pill plus state text (`Active on this device` / `Locked`); the headline *"The calls where the model / disagrees with the market"* (19/800/-0.025em, white); a stat row (`CALLS`, `WIDEST GAP`) and a white CTA pill reading `Open Top calls` or `See plans`
3. **Tile grid**, 2 columns, 10px gap — `surface` r14, min-height 92, a big value top-left and label/sub bottom-left: **All matches** (count, `accent`) · **Favourites** (starred-league count, `#f5b715`) · **How it works** (`01`) · **Get in touch** (`02`)
4. **Preferences card** — `ODDS FORMAT` segmented chips `EU` / `UK` / `US` with a live example (`e.g. 1.50` / `1/2` / `−200`). **No language selector.**
5. Footer: `Terms of service` · `Privacy policy` · version, all 11.5 `textFaint`

---

## 8. Paywall (full-screen)

`PREMIUM` accent pill top-left, `×` top-right. Headline *"See where the model / beats the market"* at 26/800/-0.035em. The perks list with accent `✓` marks:

1. No ads, anywhere
2. Full model reasoning on every pick
3. Top calls, where we agree and disagree with the odds
4. What the odds imply, with the margin stripped out
5. Power charts and form trends
6. Filter by odds and probability

Three plan rows (`surface`, r14, 2px border — `accent` when selected, transparent otherwise): a 20px radio, plan name (15/700) with `₦{was}` struck through beside `₦{now}`, and the price right at 19/800. The Monthly row carries a `MOST POPULAR` pill pinned to its top edge (`top: -9px; left: 15px`, `accent`, white, 9/800/0.1em).

Pricing (₦): Weekly 2,800 → 1,400 · Monthly 6,000 → 3,000 · Yearly 16,000 → 8,000.

Footer: legal note at 11 `textFaint` ("Auto-renewing subscription. Cancel any time at least 24 hours before the period ends. 18+."), a primary `Start {plan} — ₦{price}` button (50px, r13), and a `Restore purchase` text button.

---

## 9. Behaviour & data

### 9.1 State

```ts
darkMode, tab                                  // 'picks' | 'live' | 'premium' | 'profile'
selectedDate                                   // day being viewed
sport, subSport, segment                       // 'All' | 'Upcoming' | 'Finished'
minProb (60), minOdds (1.01)
country                                        // null = all
favourites: string[]                           // league names
onlyFavourites
expanded                                       // match id open in the feed
savedPicks: SavedPick[]                        // receipts from the server, NOT ids to re-render
premium, plan
oddsFormat                                     // 'EU' | 'UK' | 'US'
stance ('We disagree'), topExpanded
calOpen, countryOpen, sheetOpen, hubOpen, paywallOpen
```

### 9.2 Filter pipeline (order matters)

1. matches for the selected date
2. drop non-matching sports (postponed matches always survive threshold filters)
3. `prob ≥ minProb` and `odds ≥ minOdds` — **but a pick with NO price must not be dropped by the
   odds floor.** An odds filter is unanswerable where no odds exist, and the previous
   "no odds → fails the floor" rule made every upcoming tennis fixture vanish the moment the
   slider moved off its minimum. A real price, once present, is filtered normally. This is
   already how the server behaves; the client must not re-impose the stricter rule.
4. segment (All / Upcoming / Finished)
5. drop empty league groups
6. keep only starred leagues if `onlyFavourites`
7. **sort starred leagues to the front, stable within each band**
8. build the country list from the result of step 7 (before country filtering, so switching countries is never a dead end)
9. apply the country filter

### 9.3 Odds formatting

One helper converts a decimal price for display everywhere (feed, sheet label, expanded rows):
- `EU` — two decimals (`1.50`)
- `UK` — nearest clean fraction with denominators 1–20 (`1/2`)
- `US` — `+{(d−1)×100}` when `d ≥ 2`, else `−{100/(d−1)}`

### 9.4 Model vs market

Each upcoming call carries a model probability and a market probability derived from de-vigged odds; `gap = model − market`. The stance bands and everything on Top calls derive from `gap`. Sort each band widest-gap first.

### 9.5 Data shape

```ts
type Match = {
  teamA: string; teamB: string;
  status: string;          // '8:00 PM' | 'FULL-TIME' | 'POSTPONED'
  score?: string;          // '2 – 1'
  pick?: string;           // 'UNDER 3.5'
  prob?: number;           // model probability, %
  odds?: number;           // decimal
  result?: 'win' | 'loss' | 'pending';
  postponed?: boolean;
  // Provenance — the pick is recomputed per request and never stored, so the card has to be
  // able to say WHEN it was called and what it moved from. See §3.2.
  asOf?: string;           // ISO; when the underlying prediction was generated
  previousProb?: number;   // last materially different value, else absent — drives `was {n}%`
  completeness?: number;   // 0–1 share of the model's inputs that were real; < 0.35 dims
};

// The saved RECEIPT. Captured server-side at save time and never recomputed — this is a record
// of what the user acted on, not a live view of the same fixture.
type SavedPick = {
  fixtureId: string;
  league: string; teamA: string; teamB: string;
  kickoff: string; kickoffIsEstimated: boolean;
  status: string;
  savedAt: string;
  market: string | null;      // null = saved with no pick recorded; never fabricate one
  selection: string | null;
  line: number | null;
  probability: number | null;
  odds: number | null;
};

type LeagueGroup = {
  league: string;          // 'Scottish Premiership'
  country: string;         // 'Scotland'
  flag: string;            // country code shown in the badge: 'SCO'
  matches: Match[];
};
```

Group by league, and give each group its country and code so the country filter and badges work.

**On badges — the reasoning here is right and the conclusion is now out of date.** Emoji flags
genuinely do not render reliably: measured in this app, regional-indicator sequences fell back to
raw two-letter text ("BR", "CN") and Scotland's 7-codepoint tag sequence degraded to a blank
flag. That is why the app already ships **bundled flag PNGs** rather than emoji — real images,
uniform 24×24, centre-cropped so differing aspect ratios still read as one size, with a globe
fallback for anything unmapped. It also ships **competition badges** for the UEFA competitions,
whose "country" is Europe and for which a flag says almost nothing.

So: keep letter codes wherever a compact inline badge is wanted (the country picker rows, where
a code beside a name is clearer than a flag), but the **league-group header keeps the existing
PNG flag / competition badge**, which is strictly better than a code and already built. Do not
regress it to `SCO`.

Seed fixtures for development: Série A (Brazil, BR), Scottish Premiership (Scotland, SCO), Chinese Super League (China, CN), Major League Soccer (USA, US), plus a Premier League / La Liga / Serie A set for Live. Include at least one postponed match, one won pick, one lost pick and several pending across different days so every state is reachable.

---

## 9.6 What this design needs that the backend does NOT yet provide

Ordered by how much work sits behind them. Nothing here blocks starting the UI; all of it blocks
shipping the screens that depend on it, so it is listed rather than discovered late.

**1. Per-pick driver explanations — the biggest gap.** Both expanded panels (§3.2 factor rows,
§5.4 "WHAT IS DRIVING THIS") need to say WHY a call was made, with a magnitude per factor. There
is no feature-attribution anywhere in the system today: the models are XGBoost, the inputs are
stored, but nothing computes or serves per-fixture contributions. Options, cheapest first:
(a) surface the raw inputs already stored — recent form, rest days, H2H — as descriptive rows
without claiming they caused anything; (b) global feature importances, which are honest but
identical on every card; (c) real per-prediction SHAP values, which is the only version that
supports the copy as written. **Do not fabricate factor weights to fill the bars.** If (a) is
chosen, the eyebrow should read `WHAT WENT INTO THIS` rather than `WHY THE MODEL CALLED IT`.

**2. De-vigged market probability is computed but never exposed.** The whole Top calls screen
rests on `gap = model − market`. The devig maths already exists server-side (it feeds the football
1X2 model as an input feature), but no endpoint returns a market probability. This is a small,
well-understood addition — the arithmetic is written and tested.

> **A structural caveat that shapes what Top calls can promise.** The football 1X2 model
> CONSUMES devigged market probability as an input feature — adopted deliberately, on measured
> evidence, as the best 1X2 result this project has recorded. A model trained partly on the
> market's own opinion will systematically agree with it, so the "We disagree" band is
> compressed BY CONSTRUCTION for football 1X2, and a wide gap there is more likely to be thin
> odds coverage than genuine insight. Markets the model does not see prices for (corners, and
> anything in a league with no odds) are where a real disagreement can still appear. The screen
> is still worth building; the copy should not imply an edge the architecture argues against.

**3. Calibration figures must carry their own uncertainty.** §5.1 states *"When we say 70%, it
happens 68% of the time"* over *"our last 412 published calls"*. The data exists, but the honest
numbers today are uncomfortable and thin: settled per-fixture samples are around n=60–90 per
sport with 95% intervals spanning roughly ±12 points, and the shipped confidence tiers measured
inverted at one point (HIGH claiming 74% and delivering 61%). Show `n` alongside, and do not
publish a band whose sample is below the reporting floor. A calibration claim the data cannot
support is worse than no calibration block.

**4. `ON TRACK` / `AT RISK` on Live has no implementation.** It needs the live score compared
against the specific pick — derivable for h2h, double chance and goals totals from the running
score, but **not for corners**, where live corner counts are not ingested. A corners pick should
show no tag rather than a guessed one.

**5. Premium is a commercial layer with nothing behind it.** No payment provider, no entitlement
store, no receipt validation, and ₦ pricing implies a Nigerian payment rail. The paywall can be
built as UI against a local `premium` flag, but "Premium sees five calls" is not enforceable
until entitlements are real — treat the gating as presentational for now and say so.

**6. Smaller, but real.**
- **Market gates are invisible to the design.** Some leagues deliberately offer no goals or
  corners pick, one competition is suppressed entirely, and those cards legitimately show
  nothing. The empty state currently blames the user's filters; where a gate is the cause, the
  copy should not.
- **Withdrawn fixtures** are hidden server-side (a provider publishing then retracting a draw).
  The client needs no logic, but do not "fix" their absence.
- **Odds format** already exists as a stored user preference; the Hub control should read and
  write it rather than keeping a second copy in local state.

---

## 10. Acceptance checklist

- [ ] Both themes ship; every colour comes from the token map
- [ ] All four tabs reachable; wordmark shows on every screen instead of a per-screen title
- [ ] Country picker lives in the third summary slot, is built from live data, cuts across sports, and reads `All` when unset
- [ ] Favourite stars sit on league headers, persist, and pin starred leagues to the top of the day
- [ ] Matches render as rows inside one league card, separated by hairlines
- [ ] Expanding a match shows factor bars and a save/remove action
- [ ] Top calls has no slip maths, no booking code, no bookmaker chips, and no target-odds slider
- [ ] The 20-cell gap bar splits by share of belief, uses the two gradients, and carves the overhang out of the favoured side
- [ ] Driver wording flips with the direction of the gap
- [ ] Free tier sees one call per band; Premium sees five; the paywall unlocks Top calls
- [ ] Odds format switch in the Hub changes every price on screen
- [ ] Profile has no hit-rate card and no stat tiles
- [ ] Nothing overflows at 390px width, and no team, league or country string wraps to a second line
- [ ] Every pick carries an `as of` line in its expanded panel; `was {n}%` shows only when the
      call actually moved, and never as a placeholder
- [ ] A pick below the completeness floor renders dimmed with `limited data`, worded as a limit
      of the data rather than a hedge on the number
- [ ] Saving records the pick as shown; the saved screen renders that receipt and re-ranks
      nothing — verified by changing the live pick and confirming the saved one does not follow
- [ ] Settled saved picks stay visible with their result, losses included
- [ ] A saved row with no recorded pick says so rather than inventing one
- [ ] League headers keep real flag/competition images; letter codes are only used inline in the
      country picker
- [ ] A pick with no odds survives the odds floor
- [ ] No factor bar displays a fabricated weight; if attribution is unavailable the panel is
      labelled as inputs, not causes
- [ ] Kick-off alert copy states the interval the backend actually fires at
