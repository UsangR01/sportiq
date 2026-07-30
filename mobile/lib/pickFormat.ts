// Shared display formatting for a pick's selection across every market this product supports
// — "home"/"draw"/"away" (h2h), "1X"/"X2" (double chance), "over"/"under" (goals/corners
// totals). Used by FixtureCard's badges (Picks feed) so a corners or double-chance pick reads
// as clearly as a plain h2h one.

const SELECTION_LABELS: Record<string, string> = {
  home: "HOME",
  draw: "DRAW",
  away: "AWAY",
  "1X": "1X (Home/Draw)",
  X2: "X2 (Away/Draw)",
  over: "OVER",
  under: "UNDER",
};

export function selectionLabel(selection: string): string {
  return SELECTION_LABELS[selection] ?? selection.toUpperCase();
}

export function pickHeadline(pick: { selection: string; line?: number | null }): string {
  const label = selectionLabel(pick.selection);
  return pick.line != null ? `${label} ${pick.line}` : label;
}

type Pick = { market: string; selection: string; line?: number | null };

/**
 * Was this pick actually right, given the real final score? Returns null when correctness
 * genuinely can't be determined from the data we have — never a guessed true/false. Covers
 * every market whose real outcome we CAN derive from home/away goals (h2h, double chance,
 * goals totals); corners totals stay null since FixtureLiveState has no corner count at all
 * (only home_score/away_score are tracked — see app/fixtures/models.py:FixtureLiveState).
 */
export function evaluatePickCorrectness(
  pick: Pick,
  homeScore: number,
  awayScore: number,
): boolean | null {
  const actual: "home" | "draw" | "away" =
    homeScore > awayScore ? "home" : homeScore < awayScore ? "away" : "draw";

  switch (pick.market) {
    case "h2h":
      return pick.selection === actual;
    case "double_chance":
      if (pick.selection === "1X") return actual === "home" || actual === "draw";
      if (pick.selection === "X2") return actual === "away" || actual === "draw";
      return null;
    case "goals_total": {
      if (pick.line == null) return null;
      const totalGoals = homeScore + awayScore;
      if (pick.selection === "over") return totalGoals > pick.line;
      if (pick.selection === "under") return totalGoals < pick.line;
      return null;
    }
    default:
      // corners_total (no corner count tracked) and anything unrecognised.
      return null;
  }
}
