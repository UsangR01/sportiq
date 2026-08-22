import { useSafeAreaInsets } from "react-native-safe-area-context";

/** Safe screen padding, measured from the device rather than assumed.
 *
 * WHY THIS EXISTS. The design canvas is an iPhone 14 (390x844), and its numbers were taken
 * literally: every screen hardcoded `paddingTop: 60` and the tab bar `paddingBottom: 22`. On
 * Android that is wrong in both directions — a three-button navigation bar is around 48dp tall,
 * so the tab bar's labels and glyphs sat UNDERNEATH the system buttons and were partly
 * untappable. Reported from a real device.
 *
 * Worse, `react-native-safe-area-context` was installed but never used and `SafeAreaProvider`
 * was missing from the root, so even code that asked for insets would have been told zero. The
 * provider is now in app/_layout.tsx.
 *
 * Nothing here invents a number for a device that reports nothing (web, older Android with
 * hardware keys): it falls back to a small constant, which is correct there because those
 * platforms genuinely have no overlay to clear.
 */

/** The design's own gap between the status bar and the first row of content.
 *
 * Derived, not guessed: the spec's 60px top padding on a canvas whose status-bar inset is ~47
 * leaves ~13px of breathing room. Adding that to the REAL inset reproduces the intended look on
 * any device instead of only on the one it was drawn for. */
const CONTENT_GAP_BELOW_STATUS_BAR = 13;

/** Used where a platform reports no inset at all. Not a device assumption — just enough that
 * content is not flush against the edge. */
const MIN_TOP = 20;
const MIN_BOTTOM = 8;

export interface ScreenInsets {
  /** Top padding for a screen's own content. */
  top: number;
  /** Bottom padding that clears the home indicator or navigation bar. */
  bottom: number;
  /** The raw device inset, for anything that needs to reason about it directly. */
  rawBottom: number;
}

export function useScreenInsets(): ScreenInsets {
  const insets = useSafeAreaInsets();
  return {
    top: Math.max(insets.top + CONTENT_GAP_BELOW_STATUS_BAR, MIN_TOP),
    bottom: Math.max(insets.bottom, MIN_BOTTOM),
    rawBottom: insets.bottom,
  };
}
