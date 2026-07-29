import { apiFetch } from "./client";
import type { FixtureDetail, FixtureSummary } from "./types";

export interface ListFixturesParams {
  sport_slug?: string;
  status?: "scheduled" | "live" | "completed";
  limit?: number;
  /** ISO datetime strings — backend already supported this filter, just never had a caller. */
  date_from?: string;
  date_to?: string;
}

export function listFixtures(params: ListFixturesParams = {}): Promise<FixtureSummary[]> {
  const query = new URLSearchParams();
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  if (params.date_from) query.set("date_from", params.date_from);
  if (params.date_to) query.set("date_to", params.date_to);
  const qs = query.toString();
  return apiFetch<FixtureSummary[]>(`/fixtures${qs ? `?${qs}` : ""}`);
}

export function getFixture(id: string): Promise<FixtureDetail> {
  return apiFetch<FixtureDetail>(`/fixtures/${id}`);
}
