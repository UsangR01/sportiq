/** Radii, spacing and control sizes (design spec §1.3).
 *
 * Named rather than inline so the same shape cannot drift by a pixel between two screens that
 * are meant to look identical — and so a change is one edit rather than a search.
 *
 * The design canvas is 390×844 (iPhone 14/15 logical size). Nothing here may push content past
 * 390 minus two screen paddings; §10 requires no team, league or country string to wrap.
 */

export const SCREEN = {
  /** Horizontal padding on every screen. */
  padding: 18,
  /** Clears the status bar. 58 on full-screen overlays, which sit above the normal chrome. */
  paddingTop: 60,
  paddingTopOverlay: 58,
  /** Clears the home indicator under the tab bar. */
  tabBarPaddingBottom: 22,
  /** The design canvas width — assert against this when checking for overflow. */
  canvasWidth: 390,
} as const;

export const RADIUS = {
  /** League card, bottom sheet body. */
  card: 18,
  /** Small card, header control. */
  control: 14,
  /** Button, expanded panel. */
  button: 12,
  /** Chip. */
  chip: 11,
  chipTight: 10,
  /** Icon button, segmented-control thumb. */
  icon: 9,
  /** Country-code badge. */
  badge: 6,
  /** Track fills — probability bars, sliders. */
  track: 3,
  trackThin: 2,
  /** Bottom sheet top corners only. */
  sheetTop: 22,
} as const;

export const CONTROL = {
  /** Header icon buttons (hamburger, theme toggle). */
  iconButton: 36,
  /** Date stepper arrows — deliberately larger than an icon button; they are tapped often. */
  dateArrow: 40,
  /** Calendar month arrows. */
  calendarArrow: 28,
  /** Favourite star on a league header. */
  star: 30,
  toggleWidth: 46,
  toggleHeight: 28,
  toggleKnob: 24,
  toggleKnobOffsetOff: 2,
  toggleKnobOffsetOn: 20,
  /** Filter-sheet chips. */
  chipHeight: 34,
  /** Paywall primary button. */
  primaryButtonHeight: 50,
  /** Slider thumb, with a 4px accent ring. */
  sliderThumb: 22,
  sliderTrack: 5,
} as const;

export const GAP = {
  /** Between cards in a list. */
  card: 10,
  /** Between league groups — deliberately much larger than the card gap so groups read as
   * separate sections without needing a divider. */
  leagueGroup: 22,
  /** Between header controls. */
  headerControl: 10,
} as const;

/** Result disc on a settled match row (success ✓ / fail ✕). */
export const RESULT_DISC = 15;

/** Live-card and match-row progress tracks. */
export const TRACK_HEIGHT = {
  pick: 4,
  factor: 6,
  live: 3,
} as const;
