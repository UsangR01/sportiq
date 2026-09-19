/** The advertising seam. Screens import `AdSlot` and `useAdsEnabled` from here and nothing else.
 *
 * ONE ADAPTER, MANY SLOTS. A screen says "an ad may go here, and its name is picks_native_2".
 * It does not know which network fills it, whether one is configured, or what a fill failure
 * looks like. That is what makes swapping networks a one-file change instead of a sweep.
 *
 * TODAY NOTHING FILLS ANY SLOT, and every `AdSlot` renders null. That is the intended state,
 * not a stub awaiting completion: AdMob cannot earn until this app is publicly listed (it will
 * not link an unlisted app, an unlinked app cannot pass the readiness review, and app-ads.txt
 * verification resolves through a store listing), and its native module cannot ship over the
 * air. So the seams, the flags and the placement rules land now and the provider lands with the
 * Play listing — at which point the only change here is registerAdProvider().
 */

import { createContext, useContext, type ReactNode } from "react";
import { View, Text } from "react-native";

import { RADIUS, TYPE, useTheme } from "@/lib/theme";

import { formatForSlot, type AdFormat, type AdSlotId } from "./slots";

export * from "./slots";
export * from "./placement";
export * from "./policy";

/** What an ad network has to provide to be usable here.
 *
 * Deliberately tiny. Anything richer would leak a particular network's model into the app —
 * AdMob's `useRewardedAd` hook shape, say — and the point of this file is that it cannot.
 */
export interface AdProvider {
  /** Render the unit, or return null when the network has no fill. A provider that cannot fill
   * MUST return null rather than a placeholder: the slot collapses, which is the documented
   * behaviour for every format here. */
  render(slot: AdSlotId, format: AdFormat): ReactNode;
}

/** The registered provider, or null. Module-level rather than context state because it is set
 * once at boot and never changes at runtime — a context would imply it can. */
let provider: AdProvider | null = null;

/** Called once at app startup when a network is configured. Nothing calls it today. */
export function registerAdProvider(next: AdProvider | null): void {
  provider = next;
}

interface AdsState {
  /** False for a subscriber. The premium tier does not exist yet, so this is false for nobody —
   * but every slot already reads it, so introducing the tier is a one-line change here rather
   * than an edit to each screen. */
  isPremium: boolean;
}

const AdsContext = createContext<AdsState>({ isPremium: false });

export function AdsProvider({
  isPremium = false,
  children,
}: {
  isPremium?: boolean;
  children: ReactNode;
}) {
  return <AdsContext.Provider value={{ isPremium }}>{children}</AdsContext.Provider>;
}

/** `showAds` for the calling screen.
 *
 * TWO CONDITIONS, AND BOTH MATTER. A premium subscriber sees no ad frames anywhere; and with no
 * provider registered there is nothing to show, so the placement logic must be told not to
 * reserve space it cannot fill. Screens call this to decide WHETHER, never to decide what.
 */
export function useAdsEnabled(): boolean {
  const { isPremium } = useContext(AdsContext);
  return !isPremium && provider !== null;
}

/** The only advertising component a screen may render.
 *
 * Renders null when there is no provider, when the viewer is premium, or when the network has
 * no fill — three different reasons, one visible outcome, which is what "collapse any slot the
 * adapter cannot fill" means. A caller never has to distinguish them.
 */
export function AdSlot({ id }: { id: AdSlotId }) {
  const enabled = useAdsEnabled();
  if (!enabled || !provider) return null;
  const filled = provider.render(id, formatForSlot(id));
  if (!filled) return null;
  return <AdFrame slot={id}>{filled}</AdFrame>;
}

/** The chrome around every unit: an AD mark, and separation from whatever sits next to it.
 *
 * THE MARK IS NOT DECORATION. Apple 2.5.18 requires an ad that interrupts the experience to
 * "clearly indicate that they are an ad", and AdMob's own placement policy treats a unit that
 * looks like content as the deception case. An in-feed native unit sitting inside a league card
 * is the exact shape that rule exists for, so the label is applied HERE — once, to every
 * format — rather than left to each call site to remember.
 */
function AdFrame({ slot, children }: { slot: AdSlotId; children: ReactNode }) {
  const { colors } = useTheme();
  return (
    <View
      accessibilityLabel="Advertisement"
      // The slot id, so a placement can be asserted by name once this app has a test runner —
      // "is there an ad after the second league" is otherwise only answerable by eye.
      testID={`ad-slot-${slot}`}
      style={{
        borderRadius: RADIUS.button,
        backgroundColor: colors.surfaceAlt,
        padding: 10,
        gap: 6,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
        <View
          style={{
            paddingHorizontal: 5,
            paddingVertical: 1,
            borderRadius: RADIUS.chipTight,
            backgroundColor: colors.mutedBg,
          }}
        >
          <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, letterSpacing: 0.5 }]}>
            Ad
          </Text>
        </View>
        {/* Apple 2.5.18 also requires a way to report an inappropriate ad. It belongs on this
            frame, so every format inherits it, and it is wired when a provider exists — a
            report control with no network to report to would be a dead button. */}
      </View>
      {children}
    </View>
  );
}
