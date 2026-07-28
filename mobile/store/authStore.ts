import { create } from "zustand";

import * as authApi from "@/lib/api/auth";
import { clearTokens, hydrateTokens, setTokens, subscribe, type Tokens } from "@/lib/tokenStore";

/** The access token's `sub` claim (the user id), read without verifying the signature — this
 * is display-only on the client; every real authorization check happens server-side against
 * the signed token. */
function decodeUserId(accessToken: string): string | null {
  try {
    const payload = accessToken.split(".")[1];
    const decoded = JSON.parse(
      globalThis.atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
    );
    return decoded.sub ?? null;
  } catch {
    return null;
  }
}

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  userId: string | null;
  email: string | null;
  isHydrated: boolean;
  hydrate: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    guestSessionId?: string | null
  ) => Promise<void>;
  clearAuth: () => Promise<void>;
}

function tokensToState(tokens: Tokens) {
  return {
    accessToken: tokens.accessToken,
    refreshToken: tokens.refreshToken,
    email: tokens.email,
    userId: tokens.accessToken ? decodeUserId(tokens.accessToken) : null,
  };
}

/** Thin reactive wrapper around lib/tokenStore.ts (the actual source of truth). The API
 * client also reads/writes tokenStore directly during its own refresh-on-401 flow, without
 * going through Zustand — subscribing here is what keeps this store (and every screen
 * reading it) in sync with token rotations the client triggers on its own. */
export const useAuthStore = create<AuthState>((set) => {
  subscribe((tokens) => set(tokensToState(tokens)));

  return {
    accessToken: null,
    refreshToken: null,
    userId: null,
    email: null,
    isHydrated: false,

    hydrate: async () => {
      const tokens = await hydrateTokens();
      set({ ...tokensToState(tokens), isHydrated: true });
    },

    login: async (email, password) => {
      const pair = await authApi.login(email, password);
      await setTokens(pair.access_token, pair.refresh_token, email);
    },

    register: async (email, password, guestSessionId) => {
      const pair = await authApi.register(email, password, guestSessionId);
      await setTokens(pair.access_token, pair.refresh_token, email);
    },

    clearAuth: async () => {
      await clearTokens();
    },
  };
});
