import { apiFetch } from "./client";
import type { FixtureDetail, FixtureSummary, PickMarket } from "./types";

export interface ListFixturesParams {
  sport_slug?: string;
  status?: "scheduled" | "live" | "completed";
  limit?: number;
  /** ISO datetime strings — backend already supported this filter, just never had a caller. */
  date_from?: string;
  date_to?: string;
  /** Restricts best_pick to one market instead of the default combined-best-across-all-
   * markets ("all"/omitted) — mirrors GET /picks's own market param. */
  market?: PickMarket;
  /** Required by the backend when market is goals_total/corners_total. */
  line?: number;
  /** Drops any fixture whose best_pick doesn't clear this probability — the Picks feed's
   * core filter ("we just want the best odds with the highest probability of winning"). */
  min_probability?: number;
  /** Drops any fixture whose best_pick's odds don't clear this (probability-only picks with
   * no real odds are dropped too when this is set — never fabricate an odds floor). */
  min_odds?: number;
}

export function listFixtures(params: ListFixturesParams = {}): Promise<FixtureSummary[]> {
  const query = new URLSearchParams();
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  if (params.date_from) query.set("date_from", params.date_from);
  if (params.date_to) query.set("date_to", params.date_to);
  if (params.market) query.set("market", params.market);
  if (params.line !== undefined) query.set("line", String(params.line));
  if (params.min_probability !== undefined) {
    query.set("min_probability", String(params.min_probability));
  }
  if (params.min_odds !== undefined) query.set("min_odds", String(params.min_odds));
  const qs = query.toString();
  return apiFetch<FixtureSummary[]>(`/fixtures${qs ? `?${qs}` : ""}`);
}

export function getFixture(id: string): Promise<FixtureDetail> {
  return apiFetch<FixtureDetail>(`/fixtures/${id}`);
}
