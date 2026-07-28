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
  minOdds: 1.5,
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
