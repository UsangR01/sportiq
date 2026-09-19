/** Every advertising placement this app has, named once.
 *
 * WHY NAMED SLOTS RATHER THAN SDK CALLS. A screen decides WHETHER an ad belongs somewhere and
 * WHERE; it never decides which network fills it. Swapping AdMob for anything else, or adding
 * mediation, must not touch a screen file. That is the whole contract, and the slot id is the
 * seam.
 *
 * NOTHING HERE TALKS TO AN AD NETWORK, and that is deliberate rather than unfinished. AdMob
 * needs a native module, which cannot ship over the air and forces a rebuild plus a reinstall
 * for everyone holding the current APK. It also cannot earn until the app is publicly listed:
 * AdMob will not link an app that is not in a supported store, an unlinked app cannot pass the
 * readiness review, and app-ads.txt verification (mandatory for new apps since January 2025)
 * resolves through a store listing that does not exist yet. So the placement rules, the flags
 * and the seams go in now; the provider arrives with the Play listing.
 */

/** The formats the design calls for. Each one has different placement rules — see placement.ts
 * and the frequency rules below. */
export type AdFormat = "native" | "mpu" | "banner" | "rewarded";

/** Slot ids, fixed and enumerated so a typo cannot invent a placement.
 *
 * `picks_native_{n}` is sequential rather than positional: the feed's own cadence decides how
 * many are placed on a given day, and numbering them by sequence is what lets a network report
 * on "the second in-feed unit" consistently.
 */
export type AdSlotId =
  | `picks_native_${number}`
  | "picks_mpu_1"
  | "saved_native_1"
  | "global_banner"
  | "reward_fixture_unlock";

export const AD_FORMAT_BY_SLOT: Record<string, AdFormat> = {
  picks_mpu_1: "mpu",
  saved_native_1: "native",
  global_banner: "banner",
  reward_fixture_unlock: "rewarded",
};

export function formatForSlot(id: AdSlotId): AdFormat {
  return id.startsWith("picks_native_") ? "native" : (AD_FORMAT_BY_SLOT[id] ?? "native");
}

/** Minimum feed length before the end-of-feed MPU is worth showing.
 *
 * The unit COLLAPSES on a short feed rather than padding it with empty space. This app's own
 * filters legitimately empty a day — the odds floor alone can take a card to nothing — and an
 * ad is the last thing that should occupy a screen that had no picks to show. */
export const MIN_FEED_FOR_MPU = 4;

/** Minimum saved picks before the saved-tab native unit appears. Same collapse-don't-pad rule. */
export const MIN_SAVED_FOR_AD = 3;
