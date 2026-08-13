import { create } from "zustand";

import * as authApi from "@/lib/api/auth";
import { getItem, setItem } from "@/lib/storage";

const GUEST_SESSION_KEY = "sportiq_guest_session_id";

interface PreferencesState {
  guestSessionId: string | null;
  sportFilter: string | null;
  minOdds: number;
  oddsFormat: "decimal" | "fractional" | "american";
  isHydrated: boolean;
  hydrate: () => Promise<void>;
  ensureGuestSession: () => Promise<string | null>;
  setSportFilter: (slug: string | null) => void;
  setMinOdds: (value: number) => void;
}

/** Guest filter state (TDD §2.1/§5.4): lives server-side in Redis keyed by an anonymous UUID,
 * 24h TTL, migrated into user_preferences on registration (see authApi.register's
 * guestSessionId param). Best-effort synced to the backend on every change — a dropped sync
 * just means the next screen load falls back to these same in-memory defaults. */
export const usePreferencesStore = create<PreferencesState>((set, get) => ({
  guestSessionId: null,
  sportFilter: null,
  // The slider's own bottom (FiltersPanel's minimumValue), per direct request on 2026-08-14
  // -- "set the default odds slider to the lowest". Was 1.5.
  //
  // A 1.50 default silently HID every short-priced favourite: the model's tennis favourites
  // sit around 0.70 and price near 1.35-1.45, and CLAUDE.md already records a day whose feed
  // rendered empty at 1.50 because both surviving picks priced at 1.24 and 1.42. Worse, a
  // fixture with NO odds at all cannot be filtered on price, so the floor removed exactly the
  // fixtures that had real prices while leaving unpriced ones visible.
  //
  // Quality is unchanged: min_odds only ever filtered on price, never on whether a pick was
  // any good. MIN_EDGE_OVER_BASE_RATE, MIN_FEATURE_COMPLETENESS, MAX_EDGE_OVER_MARKET and the
  // barred-market rule all still apply server-side at full strength.
  minOdds: 1.01,
  oddsFormat: "decimal",
  isHydrated: false,

  hydrate: async () => {
    const guestSessionId = await getItem(GUEST_SESSION_KEY);
    set({ guestSessionId, isHydrated: true });
  },

  ensureGuestSession: async () => {
    const existing = get().guestSessionId;
    if (existing) return existing;
    try {
      const { guest_session_id } = await authApi.createGuestSession();
      await setItem(GUEST_SESSION_KEY, guest_session_id);
      set({ guestSessionId: guest_session_id });
      return guest_session_id;
    } catch {
      return null;
    }
  },

  setSportFilter: (slug) => {
    set({ sportFilter: slug });
    const { guestSessionId } = get();
    if (guestSessionId) {
      authApi.updateGuestSession(guestSessionId, { sport_filter: slug }).catch(() => {});
    }
  },

  setMinOdds: (value) => {
    set({ minOdds: value });
    const { guestSessionId } = get();
    if (guestSessionId) {
      authApi.updateGuestSession(guestSessionId, { min_odds: value }).catch(() => {});
    }
  },
}));
