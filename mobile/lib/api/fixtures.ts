import { apiFetch } from "./client";
import type { FixtureDetail, FixtureSummary } from "./types";

export interface ListFixturesParams {
  sport_slug?: string;
  status?: "scheduled" | "live" | "completed";
  limit?: number;
}

export function listFixtures(params: ListFixturesParams = {}): Promise<FixtureSummary[]> {
  const query = new URLSearchParams();
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  const qs = query.toString();
  return apiFetch<FixtureSummary[]>(`/fixtures${qs ? `?${qs}` : ""}`);
}

export function getFixture(id: string): Promise<FixtureDetail> {
  return apiFetch<FixtureDetail>(`/fixtures/${id}`);
}
