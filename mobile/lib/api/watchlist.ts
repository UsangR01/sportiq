import { apiFetch } from "./client";
import type { BestPick, WatchlistItem } from "./types";

/** Saved fixtures for the signed-in user (PRD PICK-07). Authenticated only — a guest session
 * is device-bound Redis state with a 24h TTL, while a watchlist is durable and drives a push
 * notification, which needs an account's push token. */
export function listWatchlist(): Promise<WatchlistItem[]> {
  return apiFetch<WatchlistItem[]>("/user/watchlist");
}

/** Saves a fixture AND records the pick currently on its card. That receipt is the point:
 * best_pick is recomputed on every request and never stored, so without it a saved fixture
 * would show a different call every time it was opened.
 *
 * SEND THE PICK THE CARD IS RENDERING. The server cannot work it out: best_pick is chosen from
 * the candidates that clear the min-odds and min-probability sliders, and those live here, not
 * in user_preferences. Reported when a saved "HOME at 1.55" read back as "1X at 1.17" -- the
 * short price is excluded at the 1.20 default the card applied, and included by the server,
 * which applied no floor at all.
 *
 * Omitting it is still valid and falls back to the server's own choice, which is right for a
 * surface that shows no single headline pick (the fixture detail screen). */
export function addToWatchlist(fixtureId: string, shown?: BestPick | null): Promise<void> {
  return apiFetch<void>("/user/watchlist", {
    method: "POST",
    body: JSON.stringify({
      fixture_id: fixtureId,
      shown_market: shown?.market,
      shown_selection: shown?.selection,
      shown_line: shown?.line,
      shown_probability: shown?.probability,
      shown_odds: shown?.odds,
    }),
  });
}

/** Idempotent: removing something not saved is a 204, so the client never has to reconcile
 * which state it thought it was in. */
export function removeFromWatchlist(fixtureId: string): Promise<void> {
  return apiFetch<void>(`/user/watchlist/${fixtureId}`, { method: "DELETE" });
}
