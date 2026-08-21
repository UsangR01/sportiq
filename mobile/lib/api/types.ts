// Mirrors backend/app/*/schemas.py exactly — keep these in lock-step with the FastAPI
// response_models by hand, since there's no shared schema generation between the two yet.

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface LeagueOption {
  slug: string;
  name: string;
}

export interface SportResponse {
  id: string;
  slug: string;
  name: string;
  model_type: string;
  league_count: number;
  /** Only populated when the sport has few enough leagues to offer as filters (see
   * app/sports/router.py:LEAGUE_PICKER_MAX). Football's 18 come back empty on purpose --
   * the feed already groups by league internally, which is the right affordance at that
   * count. Basketball's NBA/WNBA and tennis's ATP/WTA come back populated. */
  leagues: LeagueOption[];
}

export interface BestPick {
  // "home"|"draw"|"away" (h2h); "1X"|"X2" (double_chance); "over"|"under" (totals) — drawn
  // from ACROSS every market, see backend/app/fixtures/router.py:_all_market_candidates.
  selection: string;
  probability: number;
  odds: number | null;
  market: PickMarket;
  line: number | null; // goals_total/corners_total only
  // Fraction of the model's feature vector that had a real value (0.0-1.0); null for
  // predictions made before this was recorded. A low value means the model had little real
  // information to go on, so the probability shown is closer to a base-rate guess than a
  // considered call — see LOW_CONFIDENCE_COMPLETENESS in components/fixtures/FixtureCard.tsx.
  feature_completeness: number | null;
  // When the underlying prediction was generated (ISO), and the last materially different
  // probability it superseded (null when it has not moved, which is the common case).
  //
  // best_pick is recomputed server-side on every request and never stored, so a card really
  // can read differently between visits — reported by the user as the app changing its mind
  // overnight. The churn is mostly legitimate (odds landing is new information), so the fix is
  // to stop presenting a moving estimate as timeless rather than to freeze it.
  as_of: string | null;
  previous_probability: number | null;
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
  // null for a normally-played-out result; "retired"/"walkover" for a match that ended
  // without being played out (tennis in practice). These render a neutral "RET" badge with
  // NO win/loss verdict — bookmakers generally void bets on a retirement, so a tick would
  // imply a payout the user may never have received.
  result_type: string | null;
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
  // Tennis only — a tour (ATP/WTA) is a single league, so the feed groups by TOURNAMENT
  // instead, giving users something they can actually look up in a betting app. Null for
  // football/NBA, where league_name/league_country already serve that role.
  // tournament_location is a CITY, not a country (the provider has no country field) — see
  // lib/countryFlags.tsx:countryForTournamentLocation.
  tournament_name: string | null;
  tournament_surface: string | null;
  tournament_location: string | null;
  // True when kickoff_utc was INFERRED, not reported by the provider - the card shows
  // "Time TBC" rather than asserting a precise time. Real and common for tennis: ~95% of ATP
  // matches carry no usable kickoff time, so they all inherit the tournament's start date.
  kickoff_is_estimated: boolean;
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
/** One labelled comparison row. A LIST rather than named fields because the sports do not
 * share a stat vocabulary, and their providers do not expose the same depth:
 *
 *   football   goals, corners, shots, shots on goal, possession
 *   tennis     aces, double faults, 1st serve %, break points, total points won
 *   NBA/WNBA   points -- BallDontLie's /stats is 401 on this plan, so the final score is the
 *              only real per-meeting number that exists
 *
 * The screen renders whatever rows it is given, so a new sport needs no change here. */
export interface ComparisonStat {
  label: string;
  home: number | null;
  away: number | null;
  /** Appended verbatim: "%" for percentages, "" for counts. */
  suffix: string;
}

export interface HeadToHeadResponse {
  meetings_count: number;
  home_wins: number;
  draws: number;
  away_wins: number;
  stats: ComparisonStat[];
}

export interface FixtureDetail extends FixtureSummary {
  odds: OddsLineResponse[];
  prediction: PredictionResponse | null;
  home_team_form: TeamFeaturesResponse | null;
  away_team_form: TeamFeaturesResponse | null;
  head_to_head: HeadToHeadResponse | null;
  /** What happened in THIS match, for a fixture that has been played — so the prediction can
   * be read against the result. Empty until the fixture is completed, and empty for basketball
   * at any time (BallDontLie's /stats is 401 on this plan, so the final score shown above is
   * the only real per-match number). Same row shape as the H2H panel, so both render through
   * the same component. */
  match_stats: ComparisonStat[];
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
  theme_preference: "light" | "dark" | "system";
}

export interface UserPreferencesUpdate {
  default_sport_id?: string | null;
  default_min_odds?: number | null;
  odds_format?: string | null;
  theme_preference?: "light" | "dark" | "system";
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

export interface WatchlistItem {
  fixture_id: string;
  sport_slug: string;
  league_slug: string;
  home_team: string;
  away_team: string;
  kickoff_utc: string;
  kickoff_is_estimated: boolean;
  status: string;
  created_at: string;
  // THE PICK AS IT WAS SHOWN when this fixture was saved — a receipt, not a live
  // recomputation. best_pick is recomputed per request and never stored, so the feed can
  // legitimately say something else by the time this list is opened. Null for a fixture saved
  // before the backend started recording it, or one that had no pick at all when saved.
  saved_market: PickMarket | null;
  saved_selection: string | null;
  saved_line: number | null;
  saved_probability: number | null;
  saved_odds: number | null;
}
