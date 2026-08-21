/** The v5 type scale (design spec §1.2).
 *
 * Every role is a ready-made RN style object rather than loose numbers, so a call site picks a
 * ROLE and cannot quietly invent a fourteen-and-a-half-pixel semibold of its own.
 *
 * TWO THINGS APPLY EVERYWHERE and are baked in rather than left to remember:
 *
 *  - `fontVariant: ["tabular-nums"]` on anything numeric. Proportional digits make a column of
 *    probabilities jitter as the values change, which on a screen whose whole job is comparing
 *    numbers reads as sloppiness.
 *  - `numberOfLines={1}` + ellipsis on team, league and country strings. The 390px canvas has
 *    no room for a second line, and a wrapped team name pushes a card's whole layout out of
 *    alignment with its neighbours.
 *
 * Colour is NOT set here. It comes from the token map at the call site, so one role can be
 * `text` in one place and `textFaint` in another without a second style.
 *
 * EVERY ROLE CARRIES AN EXPLICIT `lineHeight` (~1.3x the size). React Native does not derive a
 * consistent line box across platforms, and multi-line body copy is visibly tighter on web than
 * on native without one.
 *
 * Stated honestly, because the comment here previously claimed more: this was added while
 * chasing an apparent descender clip on the tab labels, on the strength of
 * `scrollHeight > clientHeight`. That metric differs by a subpixel on an 11px label and is NOT
 * a reliable clipping test — inspected at 6x zoom, the descenders render fine both ways. The
 * explicit line heights are kept because they are right for multi-line text, not because they
 * fixed a bug that was ever demonstrated.
 */

import type { TextStyle } from "react-native";

const SYSTEM_STACK = undefined; // RN uses the platform system face by default; no web fonts.

/** Numerals that do not shift width as they change. */
export const TABULAR: TextStyle = { fontVariant: ["tabular-nums"] };

export const TYPE = {
  /** "SportPIQ" — every screen shows the wordmark instead of a per-screen title. */
  wordmark: {
    fontFamily: SYSTEM_STACK,
    fontSize: 26,
    lineHeight: 32,
    fontWeight: "800",
    letterSpacing: -0.65,
  } as TextStyle,

  /** Slightly tighter wordmark used on Top calls. */
  wordmarkCompact: {
    fontSize: 24,
    lineHeight: 30,
    fontWeight: "800",
    letterSpacing: -0.72,
  } as TextStyle,

  /** All-caps section label, e.g. CALLS TODAY. */
  eyebrow: {
    fontSize: 10,
    lineHeight: 13,
    fontWeight: "800",
    letterSpacing: 1.0,
    textTransform: "uppercase",
  } as TextStyle,

  /** The smaller eyebrow used inside cards, e.g. YOU SAVED. */
  eyebrowSmall: {
    fontSize: 9.5,
    lineHeight: 12,
    fontWeight: "800",
    letterSpacing: 0.76,
    textTransform: "uppercase",
  } as TextStyle,

  /** Summary-strip value. */
  summaryValue: {
    fontSize: 20,
    lineHeight: 25,
    fontWeight: "800",
    letterSpacing: -0.7,
    ...TABULAR,
  } as TextStyle,

  /** The big model percentage on Top calls. */
  bigStat: {
    fontSize: 22,
    lineHeight: 27,
    fontWeight: "800",
    letterSpacing: -0.66,
    ...TABULAR,
  } as TextStyle,

  team: {
    fontSize: 14.5,
    lineHeight: 19,
    fontWeight: "600",
    letterSpacing: -0.29,
  } as TextStyle,

  /** A settled winner's name goes heavy — the result should be readable without the score. */
  teamWinner: {
    fontSize: 14.5,
    lineHeight: 19,
    fontWeight: "800",
    letterSpacing: -0.29,
  } as TextStyle,

  score: {
    fontSize: 14.5,
    lineHeight: 19,
    fontWeight: "800",
    ...TABULAR,
  } as TextStyle,

  pick: {
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "700",
    letterSpacing: -0.13,
  } as TextStyle,

  body: {
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "400",
  } as TextStyle,

  bodyLarge: {
    fontSize: 13.5,
    lineHeight: 18,
    fontWeight: "400",
  } as TextStyle,

  caption: {
    fontSize: 11.5,
    lineHeight: 15,
    fontWeight: "600",
  } as TextStyle,

  tabLabel: {
    fontSize: 11,
    lineHeight: 14,
    fontWeight: "600",
  } as TextStyle,
} as const;

export type TypeRole = keyof typeof TYPE;

/** Props for any string that must never reach a second line (spec §1.2, §10). Spread onto a
 * `<Text>`: `<Text {...ONE_LINE} style={...}>`. */
export const ONE_LINE = { numberOfLines: 1, ellipsizeMode: "tail" } as const;
