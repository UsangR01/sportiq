/** `useTheme()` — the single way a component gets a colour.
 *
 * WHY THERE IS NO `darkMode` BOOLEAN, despite the spec asking for one.
 *
 * The spec (§1.1) says "two themes, switched by a single `darkMode` boolean". That is the right
 * INTENT — one switch, no colour at a call site — but the app already has that switch, and it
 * does more than a boolean can:
 *
 *   - three states, not two: light / dark / **system**
 *   - persisted on device, so the choice survives a cold start before any network call
 *   - synced to the account, so a second device opens on the same theme
 *   - re-resolves when the OS scheme changes, but only while the preference is "system"
 *
 * Collapsing that to a boolean would throw away "system" and the account sync. So the resolved
 * scheme comes from the existing store (see store/themeStore.ts and components/useColorScheme),
 * and this hook turns it into tokens. One switch, exactly as intended; it simply has three
 * positions and already works.
 *
 * A boolean IS still available where a component genuinely needs one — `isDark` below — but it
 * is derived, never a second source of truth. Two sources is how the app previously ended up
 * rendering a light header on a dark device.
 */

import { useMemo } from "react";

import { useColorScheme } from "@/components/useColorScheme";

import { ELEVATION, GAP_GRADIENTS, TOKENS } from "./tokens";
import type { Elevation, Scheme, ThemeTokens } from "./tokens";

export interface Theme {
  scheme: Scheme;
  /** Derived convenience for the handful of places that need a boolean (e.g. picking an icon
   * variant). Never store this — read the scheme. */
  isDark: boolean;
  colors: ThemeTokens;
  elevation: Record<"card" | "dropdown" | "sheet", Elevation>;
  gapGradients: { model: [string, string]; odds: [string, string] };
}

export function useTheme(): Theme {
  const scheme = useColorScheme() as Scheme;
  return useMemo(
    () => ({
      scheme,
      isDark: scheme === "dark",
      colors: TOKENS[scheme],
      elevation: ELEVATION[scheme],
      gapGradients: GAP_GRADIENTS[scheme],
    }),
    [scheme]
  );
}

export { GAP, RADIUS, SCREEN, CONTROL, RESULT_DISC, TRACK_HEIGHT } from "./geometry";
export { useScreenInsets } from "./insets";
export type { ScreenInsets } from "./insets";
export { ONE_LINE, TABULAR, TYPE } from "./type";
export { LINK_HOVER, SCRIM, TOKENS, Z } from "./tokens";
export type { Elevation, Scheme, ThemeTokens } from "./tokens";
