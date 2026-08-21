import { apiFetch } from "./client";
import type { WatchlistItem } from "./types";

/** Saved fixtures for the signed-in user (PRD PICK-07). Authenticated only — a guest session
 * is device-bound Redis state with a 24h TTL, while a watchlist is durable and drives a push
 * notification, which needs an account's push token. */
export function listWatchlist(): Promise<WatchlistItem[]> {
  return apiFetch<WatchlistItem[]>("/user/watchlist");
}

/** Saves a fixture AND records the pick currently on its card, server-side. That receipt is
 * the point: best_pick is recomputed on every request and never stored, so without it a saved
 * fixture would show a different call every time it was opened. */
export function addToWatchlist(fixtureId: string): Promise<void> {
  return apiFetch<void>("/user/watchlist", {
    method: "POST",
    body: JSON.stringify({ fixture_id: fixtureId }),
  });
}

/** Idempotent: removing something not saved is a 204, so the client never has to reconcile
 * which state it thought it was in. */
export function removeFromWatchlist(fixtureId: string): Promise<void> {
  return apiFetch<void>(`/user/watchlist/${fixtureId}`, { method: "DELETE" });
}
