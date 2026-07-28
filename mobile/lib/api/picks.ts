import { apiFetch } from "./client";
import type { PickResponse } from "./types";

export interface GetPicksParams {
  min_odds: number;
  sport_slug?: string;
  limit?: number;
}

export function getPicks(params: GetPicksParams): Promise<PickResponse[]> {
  const query = new URLSearchParams({ min_odds: String(params.min_odds) });
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.limit) query.set("limit", String(params.limit));
  return apiFetch<PickResponse[]>(`/picks?${query.toString()}`);
}
