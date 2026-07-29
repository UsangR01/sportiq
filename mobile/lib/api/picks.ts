import { apiFetch } from "./client";
import type { PickMarket, PickResponse } from "./types";

export interface GetPicksParams {
  min_odds: number;
  sport_slug?: string;
  market?: PickMarket; // defaults to "h2h" server-side
  line?: number; // required by the API for goals_total/corners_total
  limit?: number;
}

export function getPicks(params: GetPicksParams): Promise<PickResponse[]> {
  const query = new URLSearchParams({ min_odds: String(params.min_odds) });
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.market) query.set("market", params.market);
  if (params.line !== undefined) query.set("line", String(params.line));
  if (params.limit) query.set("limit", String(params.limit));
  return apiFetch<PickResponse[]>(`/picks?${query.toString()}`);
}
