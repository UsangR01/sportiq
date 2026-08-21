/** The v5 design token map (design spec §1.1).
 *
 * ONE RULE: never write a colour at a call site. Import `useTheme()` and read a token. The
 * point is not tidiness — it is that a second theme only works if every colour flows from one
 * place, and this app already shipped a bug where a `dark:` class was silently inert and the
 * header rendered its LIGHT colour on a dark device. A token map makes that a single-file
 * problem rather than a hunt.
 *
 * Keyed by the SAME "light" | "dark" strings NativeWind resolves, so tokens and the existing
 * `dark:` classes can coexist while screens migrate — see lib/theme/index.ts for why the
 * existing theme store stays the source of truth rather than the spec's bare `darkMode`
 * boolean.
 */

export type Scheme = "light" | "dark";

export interface ThemeTokens {
  bg: string;
  surface: string;
  surfaceAlt: string;
  border: string;
  /** Track fills — progress bars, sliders, unfilled segments. */
  mutedBg: string;
  text: string;
  textSub: string;
  textFaint: string;
  accent: string;
  accentSoft: string;
  premiumBg: string;
  success: string;
  successSoft: string;
  fail: string;
  failSoft: string;
  warn: string;
  /** Favourite star — deliberately the SAME in both themes; a starred league should read as
   * starred at a glance regardless of scheme. */
  star: string;
}

export const TOKENS: Record<Scheme, ThemeTokens> = {
  light: {
    bg: "#f5f6f8",
    surface: "#ffffff",
    surfaceAlt: "#eef1f5",
    border: "#e2e5ea",
    mutedBg: "#eef0f3",
    text: "#14171c",
    textSub: "#6b7280",
    textFaint: "#9aa1ab",
    accent: "#2f5dfb",
    accentSoft: "#eaf0ff",
    premiumBg: "#152046",
    success: "#1f9d55",
    successSoft: "rgba(31,157,85,0.1)",
    fail: "#e2402f",
    failSoft: "rgba(226,64,47,0.1)",
    warn: "#b8720a",
    star: "#f5b715",
  },
  dark: {
    bg: "#0f1216",
    surface: "#171b21",
    surfaceAlt: "#1e232b",
    border: "#262c35",
    mutedBg: "#1c2027",
    text: "#f2f4f7",
    textSub: "#9aa3af",
    textFaint: "#6b7280",
    accent: "#5b82ff",
    accentSoft: "#1c2540",
    premiumBg: "#232a55",
    success: "#3ecb7e",
    successSoft: "rgba(62,203,126,0.14)",
    fail: "#ff6b5b",
    failSoft: "rgba(255,107,91,0.14)",
    warn: "#e0a458",
    star: "#f5b715",
  },
};

/** Link hover, light only — RN has no hover on touch, so this is for the web target. */
export const LINK_HOVER = "#234ad6";

/** Elevation, expressed as React Native shadow props rather than a CSS string, since RN needs
 * the parts separately and Android needs `elevation` on top. The spec's two-layer CSS shadow
 * collapses to one here: RN supports a single shadow per view, so the wider, softer layer is
 * kept (it carries the lift) and the 1px contact layer is folded into the border colour. */
export interface Elevation {
  shadowColor: string;
  shadowOffset: { width: number; height: number };
  shadowOpacity: number;
  shadowRadius: number;
  elevation: number;
}

export const ELEVATION: Record<Scheme, Record<"card" | "dropdown" | "sheet", Elevation>> = {
  light: {
    card: {
      shadowColor: "#14171c",
      shadowOffset: { width: 0, height: 4 },
      shadowOpacity: 0.06,
      shadowRadius: 12,
      elevation: 2,
    },
    dropdown: {
      shadowColor: "#000000",
      shadowOffset: { width: 0, height: 12 },
      shadowOpacity: 0.2,
      shadowRadius: 32,
      elevation: 12,
    },
    sheet: {
      shadowColor: "#0a0d14",
      shadowOffset: { width: 0, height: -12 },
      shadowOpacity: 0.26,
      shadowRadius: 36,
      elevation: 16,
    },
  },
  dark: {
    card: {
      shadowColor: "#000000",
      shadowOffset: { width: 0, height: 4 },
      shadowOpacity: 0.3,
      shadowRadius: 16,
      elevation: 2,
    },
    dropdown: {
      shadowColor: "#000000",
      shadowOffset: { width: 0, height: 12 },
      shadowOpacity: 0.4,
      shadowRadius: 32,
      elevation: 12,
    },
    sheet: {
      shadowColor: "#000000",
      shadowOffset: { width: 0, height: -12 },
      shadowOpacity: 0.45,
      shadowRadius: 36,
      elevation: 16,
    },
  },
};

/** Scrim behind bottom sheets and full-screen overlays. */
export const SCRIM = "rgba(10,13,20,0.5)";

/** Stacking order (spec §2). Kept here so a new overlay cannot invent its own number and land
 * behind something it should cover. */
export const Z = {
  sheetScrim: 44,
  sheetPanel: 45,
  hub: 49,
  paywall: 53,
} as const;

/** Gradients for the Top calls gap bar (spec §5.3). Endpoints only — the bar interpolates
 * across its own cell count, so the ramp reads as continuous at any split. */
export const GAP_GRADIENTS: Record<Scheme, { model: [string, string]; odds: [string, string] }> = {
  light: {
    model: ["hsl(248, 82%, 48%)", "hsl(202, 88%, 46%)"],
    odds: ["hsl(44, 90%, 54%)", "hsl(26, 82%, 50%)"],
  },
  dark: {
    model: ["hsl(248, 85%, 62%)", "hsl(202, 90%, 56%)"],
    odds: ["hsl(46, 92%, 62%)", "hsl(26, 85%, 58%)"],
  },
};
