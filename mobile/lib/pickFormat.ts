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
 * Was this pick actually right, given the real final score (and, for corners, the real
 * corner-kick count)? Returns null when correctness genuinely can't be determined from the
 * data we have — never a guessed true/false. Covers every market: h2h, double chance, and
 * goals totals are always derivable from home/away goals; corners totals need real
 * home_corners/away_corners (see app/adapters/api_football.py:fetch_corner_stats, fetched once
 * at fixture-settlement time) — null only when those are missing (NBA, or a fixture settled
 * before this existed), not a permanent gap for every corners pick.
 */
export function evaluatePickCorrectness(
  pick: Pick,
  homeScore: number,
  awayScore: number,
  homeCorners?: number | null,
  awayCorners?: number | null,
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
    case "corners_total": {
      if (pick.line == null || homeCorners == null || awayCorners == null) return null;
      const totalCorners = homeCorners + awayCorners;
      if (pick.selection === "over") return totalCorners > pick.line;
      if (pick.selection === "under") return totalCorners < pick.line;
      return null;
    }
    default:
      return null;
  }
}

