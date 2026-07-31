// Mirrors backend/app/*/schemas.py exactly — keep these in lock-step with the FastAPI
// response_models by hand, since there's no shared schema generation between the two yet.

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface SportResponse {
  id: string;
  slug: string;
  name: string;
  model_type: string;
  league_count: number;
}

export interface BestPick {
  // "home"|"draw"|"away" (h2h); "1X"|"X2" (double_chance); "over"|"under" (totals) — drawn
  // from ACROSS every market, see backend/app/fixtures/router.py:_all_market_candidates.
  selection: string;
  probability: number;
  odds: number | null;
  market: PickMarket;
  line: number | null; // goals_total/corners_total only
}

export interface LiveStateResponse {
  home_score: number;
  away_score: number;
  match_minute: number | null;
  period: string | null;
  status: string;
  // Football only — real corner-kick counts, null for NBA and for fixtures settled before
  // this existed. See lib/pickFormat.ts:evaluatePickCorrectness.
  home_corners: number | null;
  away_corners: number | null;
  last_updated_utc: string;
}

export interface FixtureSummary {
  id: string;
  sport_slug: string;
  league_slug: string;
  league_name: string;
  league_country: string | null;
  home_team: string;
  away_team: string;
  kickoff_utc: string;
  // "postponed" covers every provider status that isn't actually live/scheduled/completed
  // (postponed/cancelled/abandoned/suspended/...) — see
  // backend/app/fixtures/models.py:FixtureStatus. best_pick/all_market_picks are always
  // null/empty for a postponed fixture (the backend never shows a pre-postponement
  // prediction as if it were still live), so FixtureCard renders a plain neutral badge here.
  status: "scheduled" | "live" | "completed" | "postponed";
  season: string;
  best_pick: BestPick | null;
  // Every real candidate across all four markets (h2h, double chance, goals/corners O/U) —
  // not just best_pick's single winner. Used to show a full win/loss breakdown for a
  // completed fixture, per explicit user request ("I need all markets predicted in the past
  // to still be shown... Everything should be shown").
  all_market_picks: BestPick[];
  live_state: LiveStateResponse | null;
}

export interface OddsLineResponse {
  bookmaker: string;
  market: string;
  home_odds: number | null;
  draw_odds: number | null;
  away_odds: number | null;
  updated_at: string;
}

export interface TotalsProbability {
  line: number;
  under_prob: number | null;
  over_prob: number | null;
}

export interface ExtraMarketsResponse {
  double_chance_home_or_draw_prob: number | null;
  double_chance_away_or_draw_prob: number | null;
  goals_totals: TotalsProbability[];
  corners_totals: TotalsProbability[];
}

export interface PredictionResponse {
  model_version: string;
  home_prob: number;
  draw_prob: number | null;
  away_prob: number;
  confidence_tier: string;
  expected_value: number | null;
  extra_markets: ExtraMarketsResponse | null;
}

export interface TeamFeaturesResponse {
  elo_rating: number | null;
  attack_str: number | null;
  defence_str: number | null;
  form_pts_5: number | null;
  xg_for_5: number | null;
  xg_against_5: number | null;
  days_since_last_match: number | null;
  home_win_rate: number | null;
  away_win_rate: number | null;
}

// Real head-to-head history — replaces the raw bookmaker-odds table on the fixture detail
// screen per direct user request. Per a follow-up ask, shows average goals/corners/shots/
// shots-on-goal/possession over the last 5 real meetings per side instead of a list of
// individual match scores ("important stats that will give users confidence on the
// prediction"). home_wins/draws/away_wins and every avg_*_home/away field are relative to
// THIS fixture's home/away assignment, not each past meeting's own. Football only for now;
// null (never a fabricated empty record) for NBA or two teams with no shared history. Each
// avg_* field is independently null when none of the counted meetings had a real value for
// that specific stat.
export interface HeadToHeadResponse {
  meetings_count: number;
  home_wins: number;
  draws: number;
  away_wins: number;
  avg_goals_home: number | null;
  avg_goals_away: number | null;
  avg_corners_home: number | null;
  avg_corners_away: number | null;
  avg_shots_home: number | null;
  avg_shots_away: number | null;
  avg_shots_on_goal_home: number | null;
  avg_shots_on_goal_away: number | null;
  avg_possession_home: number | null;
  avg_possession_away: number | null;
}

export interface FixtureDetail extends FixtureSummary {
  odds: OddsLineResponse[];
  prediction: PredictionResponse | null;
  home_team_form: TeamFeaturesResponse | null;
  away_team_form: TeamFeaturesResponse | null;
  head_to_head: HeadToHeadResponse | null;
}

// Shared across BestPick (GET /fixtures, the merged Picks feed) and the still-live GET /picks
// backend endpoint (not currently called from mobile, but kept as a real, tested, reusable API
// for other consumers) — "all" additionally means "combined best pick across every market" on
// the GET /fixtures side, which /picks itself doesn't support.
export type PickMarket = "all" | "h2h" | "double_chance" | "goals_total" | "corners_total";

export interface UserPreferencesResponse {
  default_sport_id: string | null;
  default_min_odds: number | null;
  odds_format: string;
}

export interface UserPreferencesUpdate {
  default_sport_id?: string | null;
  default_min_odds?: number | null;
  odds_format?: string | null;
}

export interface GuestSessionResponse {
  guest_session_id: string;
}

export interface GuestSessionState {
  sport_filter?: string | null;
  min_odds?: number | null;
  odds_format?: string | null;
}

export interface ApiErrorBody {
  detail?: string;
}
