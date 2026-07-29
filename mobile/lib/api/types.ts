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
  selection: "home" | "draw" | "away";
  probability: number;
  odds: number | null;
}

export interface LiveStateResponse {
  home_score: number;
  away_score: number;
  match_minute: number | null;
  period: string | null;
  status: string;
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
  status: "scheduled" | "live" | "completed";
  season: string;
  best_pick: BestPick | null;
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

export interface FixtureDetail extends FixtureSummary {
  odds: OddsLineResponse[];
  prediction: PredictionResponse | null;
  home_team_form: TeamFeaturesResponse | null;
  away_team_form: TeamFeaturesResponse | null;
}

export type PickMarket = "h2h" | "double_chance" | "goals_total" | "corners_total";

export interface PickResponse {
  fixture_id: string;
  sport_slug: string;
  home_team: string;
  away_team: string;
  kickoff_utc: string;
  market: PickMarket;
  // "home" | "draw" | "away" (h2h); "1X" | "X2" (double_chance); "over" | "under" (totals)
  selection: string;
  line: number | null; // set for goals_total/corners_total only
  odds: number;
  model_probability: number;
  expected_value: number;
  confidence_tier: string;
}

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
