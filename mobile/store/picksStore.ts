import { create } from "zustand";

import { getItem, setItem } from "@/lib/storage";

/** Picks-screen state (design spec §9.1).
 *
 * In a store rather than screen state because three surfaces read the same values: the feed,
 * the filter sheet, and the Hub (whose Favourites tile filters the feed and whose count comes
 * from the same list). Screen state would mean passing all of it down two component trees and
 * back up again.
 *
 * ONLY FAVOURITES ARE PERSISTED. Starring a league is a lasting preference — a user who
 * follows the Scottish Premiership follows it next week too. The rest (date, segment, country,
 * thresholds) describe "what am I looking at right now" and should reset on a fresh open;
 * restoring yesterday's date and a country filter would silently show an emptier feed than the
 * user expects, which is the shape of complaint this project has had before.
 */

const FAVOURITES_KEY = "sportiq_favourite_leagues";

export type Segment = "All" | "Upcoming" | "Finished";

/** Defaults, per §9.1. 0.6 matches the server-side floor the feed has always applied.
 *
 * MIN ODDS WENT 1.50 -> 1.01 -> 1.20, and the middle step is why the last one is safe.
 *
 * 1.50 was dropped because it HID CARDS: the floor was applied to the winning pick after it had
 * been chosen, so a fixture whose best pick priced short vanished entirely — including the ones
 * the model was most confident about, tennis favourites at 1.35-1.45 among them. A day's feed
 * once rendered empty because both surviving picks sat at 1.24 and 1.42.
 *
 * That is no longer what happens. Since 2026-08-23 the floor filters CANDIDATES before the pick
 * is chosen, so raising it swaps in the next qualifying market instead of deleting the card —
 * measured at the time, 10 of the 11 fixtures lost at 1.2 had a >=70% alternative waiting. So a
 * 1.20 default now trades a short price for a different pick rather than for nothing.
 *
 * Deliberately a DEFAULT and not a floor: MIN_ODDS_FLOOR stays 1.01, because short-priced picks
 * are measured as the most reliable band we have (1.00-1.25 hit 87.0% against a claimed 83.9%),
 * and a user who wants them should be able to ask for them. */
export const DEFAULT_MIN_PROBABILITY = 0.6;
export const DEFAULT_MIN_ODDS = 1.2;

interface PicksState {
  selectedDate: Date;
  sport: string | null;
  subSport: string | null;
  segment: Segment;
  minProbability: number;
  minOdds: number;
  /** null = every country. */
  country: string | null;
  /** League NAMES, matching what the group header displays. */
  favourites: string[];
  onlyFavourites: boolean;
  /** Fixture id of the row open in the feed; only one at a time. */
  expanded: string | null;
  favouritesHydrated: boolean;

  setSelectedDate: (date: Date) => void;
  setSport: (sport: string | null, subSport: string | null) => void;
  setSegment: (segment: Segment) => void;
  setMinProbability: (value: number) => void;
  setMinOdds: (value: number) => void;
  setCountry: (country: string | null) => void;
  toggleFavourite: (league: string) => void;
  setOnlyFavourites: (value: boolean) => void;
  toggleExpanded: (fixtureId: string) => void;
  resetFilters: () => void;
  hydrateFavourites: () => Promise<void>;
}

/** The sport the feed opens on.
 *
 * FOOTBALL, not "All". It is the product's centre of gravity — 18 leagues of trained model
 * against one basketball model shared across two leagues and a single tennis tour — and an
 * all-sports feed buries a day's football under whatever else happens to be playing. "All"
 * remains one tap away in the filter sheet.
 *
 * Being a DEFAULT rather than a lock also matters for the Filters dot: opening on football is
 * not "a filter is active", so the dot stays off until the user changes something themselves.
 */
export const DEFAULT_SPORT = "football";

/** Everything the "Reset filters" affordance clears. Deliberately NOT the date or favourites:
 * the user did not set the date by accident, and a reset that silently unstars their leagues
 * would be destructive rather than corrective. */
const FILTER_DEFAULTS = {
  sport: DEFAULT_SPORT as string | null,
  subSport: null as string | null,
  segment: "All" as Segment,
  minProbability: DEFAULT_MIN_PROBABILITY,
  minOdds: DEFAULT_MIN_ODDS,
  country: null as string | null,
  onlyFavourites: false,
};

export const usePicksStore = create<PicksState>((set, get) => ({
  selectedDate: new Date(),
  ...FILTER_DEFAULTS,
  favourites: [],
  expanded: null,
  favouritesHydrated: false,

  setSelectedDate: (selectedDate) =>
    // Collapse any open row: the expanded id belongs to a fixture on the previous day, and
    // leaving it set makes an unrelated row on the new day appear pre-opened.
    set({ selectedDate, expanded: null }),

  setSport: (sport, subSport) => set({ sport, subSport }),
  setSegment: (segment) => set({ segment }),
  setMinProbability: (minProbability) => set({ minProbability }),
  setMinOdds: (minOdds) => set({ minOdds }),
  setCountry: (country) => set({ country }),
  setOnlyFavourites: (onlyFavourites) => set({ onlyFavourites }),

  toggleFavourite: (league) => {
    const current = get().favourites;
    const favourites = current.includes(league)
      ? current.filter((name) => name !== league)
      : [...current, league];
    set({ favourites });
    // Best-effort: a failed write costs the user one re-tap, which is not worth blocking the
    // UI or surfacing an error for.
    setItem(FAVOURITES_KEY, JSON.stringify(favourites)).catch(() => {});
  },

  toggleExpanded: (fixtureId) =>
    set({ expanded: get().expanded === fixtureId ? null : fixtureId }),

  resetFilters: () => set({ ...FILTER_DEFAULTS }),

  hydrateFavourites: async () => {
    const stored = await getItem(FAVOURITES_KEY);
    let favourites: string[] = [];
    try {
      const parsed = stored ? JSON.parse(stored) : [];
      // Guard the shape as well as the parse: a corrupted value must not put non-strings into
      // a list that is compared against league names.
      if (Array.isArray(parsed)) favourites = parsed.filter((v) => typeof v === "string");
    } catch {
      favourites = [];
    }
    set({ favourites, favouritesHydrated: true });
  },
}));

/** True when any filter differs from its default — drives the accent dot on the Filters
 * button (§3.1). Deliberately ignores the DATE: browsing to another day is navigation, not
 * filtering, and dotting the button for it would train users to ignore the dot. */
export function hasActiveFilters(state: PicksState): boolean {
  return (
    state.sport !== FILTER_DEFAULTS.sport ||
    state.subSport !== FILTER_DEFAULTS.subSport ||
    state.segment !== FILTER_DEFAULTS.segment ||
    state.minProbability !== FILTER_DEFAULTS.minProbability ||
    state.minOdds !== FILTER_DEFAULTS.minOdds ||
    state.country !== FILTER_DEFAULTS.country ||
    state.onlyFavourites !== FILTER_DEFAULTS.onlyFavourites
  );
}
