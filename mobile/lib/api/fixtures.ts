import { apiFetch } from "./client";
import type {
  FixtureDetail,
  FixtureSummary,
  StandingsResponse,
  TeamScheduleResponse,
} from "./types";

export interface ListFixturesParams {
  sport_slug?: string;
  /** Narrows to one competition within a sport -- NBA vs WNBA, ATP vs WTA. */
  league_slug?: string;
  status?: "scheduled" | "live" | "completed" | "postponed";
  limit?: number;
  /** ISO datetime strings — backend already supported this filter, just never had a caller. */
  date_from?: string;
  date_to?: string;
  /** Drops any fixture whose best_pick doesn't clear this probability — the Picks feed's
   * core filter ("we just want the best odds with the highest probability of winning").
   * best_pick itself is always the backend's combined-best-across-every-market choice (h2h,
   * double chance, goals/corners O/U) — there's no per-market filter in the UI anymore
   * (removed as clutter), though GET /fixtures's own market/line params still exist
   * server-side if a future screen ever wants them. */
  min_probability?: number;
  /** Drops any fixture whose best_pick's odds don't clear this (probability-only picks with
   * no real odds are dropped too when this is set — never fabricate an odds floor). */
  min_odds?: number;
}

export function listFixtures(params: ListFixturesParams = {}): Promise<FixtureSummary[]> {
  const query = new URLSearchParams();
  if (params.sport_slug) query.set("sport_slug", params.sport_slug);
  if (params.league_slug) query.set("league_slug", params.league_slug);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  if (params.date_from) query.set("date_from", params.date_from);
  if (params.date_to) query.set("date_to", params.date_to);
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

/** The league table for this fixture's competition. Null for a non-football fixture, a league
 * the provider has no table for, or a pre-season one — all ordinary, none an error.
 *
 * SEPARATE FROM getFixture ON PURPOSE. The fixture screen opens on Head to head, so fetching
 * the table with it would spend a call on a tab most viewers never select. This is queried
 * when the tab is chosen. */
export function getFixtureStandings(id: string): Promise<StandingsResponse | null> {
  return apiFetch<StandingsResponse | null>(`/fixtures/${id}/standings`);
}

/** A team's last results and next fixtures, across every competition — including the cup ties
 * and European nights we never ingest, which is why this comes from the provider rather than
 * from our own fixtures. */
export function getTeamSchedule(teamId: string): Promise<TeamScheduleResponse | null> {
  return apiFetch<TeamScheduleResponse | null>(`/teams/${teamId}/schedule`);
}
