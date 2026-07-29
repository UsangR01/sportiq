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
