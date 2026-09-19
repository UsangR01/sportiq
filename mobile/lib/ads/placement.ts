/** Where in-feed ads go, as a pure function.
 *
 * SEPARATED FROM RENDERING ON PURPOSE. This is the part with rules in it, and it is the part
 * that will be wrong in a way nobody notices — an off-by-one here shows up as "ads feel
 * relentless" rather than as an error. Keeping it pure means it can be reasoned about, and
 * tested the moment this app has a test runner (mobile has no Jest yet; it is on the board).
 */

import type { AdSlotId } from "./slots";

export interface AdPlacement<T> {
  group: T;
  /** The slot to render AFTER this group, or null when no ad belongs here. */
  adAfter: AdSlotId | null;
}

/** CADENCE IS COUNTED IN INSIGHT CARDS, NOT IN GROUPS — and that distinction is the whole rule.
 *
 * A league with one fixture and a league with twelve are one group each; pacing by group would
 * put an ad after a single card on a quiet day and bury one in the middle of a busy league. The
 * counter accumulates MATCHES and resets when a unit is placed, so the spacing a reader
 * experiences is constant regardless of how the day happens to be grouped.
 *
 * NEVER AFTER THE LAST GROUP. An ad at the end of the feed is not feed furniture, it is a
 * trailing advert — and the end-of-feed MPU already occupies that position, which would put two
 * units adjacent and break the "never two visible at once" rule.
 *
 * @param groups   the feed's league groups, in render order
 * @param countOf  how many insight cards a group holds
 * @param enabled  false renders nothing — no provider, or a subscriber who has paid them away
 */
export function placeFeedAds<T>(
  groups: T[],
  countOf: (group: T) => number,
  enabled: boolean
): AdPlacement<T>[] {
  let sinceAd = 0;
  let sequence = 0;

  return groups.map((group, index) => {
    sinceAd += countOf(group);
    const isLast = index === groups.length - 1;
    const place = enabled && !isLast && sinceAd >= MIN_CARDS_BETWEEN_ADS;
    if (place) {
      sinceAd = 0;
      sequence += 1;
    }
    return {
      group,
      adAfter: place ? (`picks_native_${sequence}` as AdSlotId) : null,
    };
  });
}

/** Three insight cards between units. Low enough to be worth selling, high enough that the feed
 * still reads as a feed — the complaint this pacing exists to avoid is "an ad between every few
 * prediction cards", which the design spec names as a frequency violation in its own right. */
export const MIN_CARDS_BETWEEN_ADS = 3;
