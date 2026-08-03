import { colorScheme } from "nativewind";
import { Appearance } from "react-native";
import { create } from "zustand";

import { updatePreferences } from "@/lib/api/users";
import { getItem, setItem } from "@/lib/storage";

const THEME_KEY = "sportiq_theme_preference";

export type ThemePreference = "light" | "dark" | "system";

const PREFERENCES: ThemePreference[] = ["light", "dark", "system"];

function isThemePreference(value: string | null): value is ThemePreference {
  return value !== null && (PREFERENCES as string[]).includes(value);
}

/** Applies a preference to NativeWind, resolving "system" to the OS scheme ourselves.
 *
 * tailwind.config.js sets darkMode: "class" so Light/Dark can override the OS at all. The
 * cost is that NativeWind no longer derives anything from the prefers-color-scheme media
 * query, so handing it "system" verbatim leaves it with no scheme and every `dark:` class
 * silently inert — the app renders light on a dark device. Caught by sampling a real rendered
 * colour: the header read rgb(17,24,39) (gray-900, the LIGHT value) under both OS schemes.
 * So "system" is resolved to a concrete light/dark here instead. */
function applyPreference(preference: ThemePreference): void {
  colorScheme.set(preference === "system" ? osScheme() : preference);
}

/** The OS scheme narrowed to something NativeWind accepts.
 *
 * React Native's ColorSchemeName also covers null/undefined and "unspecified"; all three mean
 * "no stated preference", which is light — the same normalisation components/useColorScheme
 * used to do before it started reading NativeWind. */
function osScheme(): "light" | "dark" {
  return Appearance.getColorScheme() === "dark" ? "dark" : "light";
}

interface ThemeState {
  preference: ThemePreference;
  isHydrated: boolean;
  hydrate: () => Promise<void>;
  setPreference: (preference: ThemePreference) => void;
  /** Adopt the account's saved theme after preferences load. */
  applyFromAccount: (preference: ThemePreference) => void;
}

/** Light/dark/system preference, persisted on-device.
 *
 * NativeWind's `colorScheme` is the lever rather than our own React state: it is what drives
 * every `dark:` class in the app, so setting it here keeps the styling, React Navigation's
 * ThemeProvider (via components/useColorScheme, which reads the same source) and this store
 * from disagreeing. "system" hands control back to the OS.
 *
 * Stored via lib/storage rather than the backend: it's a device-level display choice, not an
 * account setting — the same account on a phone and a tablet can reasonably differ, and it
 * must apply before any network call resolves, including for guests. */
export const useThemeStore = create<ThemeState>((set) => ({
  preference: "system",
  isHydrated: false,

  hydrate: async () => {
    const stored = await getItem(THEME_KEY);
    const preference = isThemePreference(stored) ? stored : "system";
    applyPreference(preference);
    set({ preference, isHydrated: true });

    // Because "system" is resolved to a concrete scheme once (see applyPreference), a later
    // OS switch would otherwise leave the app stuck on whatever it resolved to at launch.
    // Registered here rather than at module scope so it attaches exactly once, on the single
    // hydrate() the root layout runs; it intentionally lives for the app's lifetime.
    Appearance.addChangeListener(({ colorScheme: scheme }) => {
      if (useThemeStore.getState().preference !== "system") return;
      colorScheme.set(scheme === "dark" ? "dark" : "light");
    });
  },

  setPreference: (preference) => {
    applyPreference(preference);
    set({ preference });
    // Written locally AND to the account. The local copy is what makes the choice survive a
    // cold start without waiting on the network; the account copy is what carries it to
    // another device. Both are best-effort — a failed write costs the user nothing worse
    // than re-picking, so it isn't worth blocking the UI or surfacing an error.
    setItem(THEME_KEY, preference).catch(() => {});
    updatePreferences({ theme_preference: preference }).catch(() => {});
  },

  applyFromAccount: (preference) => {
    // The account is authoritative across devices, but only worth acting on when it actually
    // differs — an unconditional set() would re-render every screen on each preferences
    // fetch. The local copy is refreshed too, so the next cold start opens on the account's
    // theme rather than this device's last local choice.
    if (useThemeStore.getState().preference === preference) return;
    applyPreference(preference);
    set({ preference });
    setItem(THEME_KEY, preference).catch(() => {});
  },
}));
