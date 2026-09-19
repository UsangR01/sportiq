/** Advertising rules this app must not break, encoded rather than remembered.
 *
 * These come from reading the actual store and network policies (Sept 2026), not from custom.
 * They live in code because the one that matters most is a single switch in a dashboard that
 * nobody would think to check twice.
 */

/** NEVER ENABLE THESE AD CATEGORIES. This is the single highest-consequence setting in the
 * whole integration, and it is a dashboard toggle rather than code — so this constant exists to
 * make the reasoning findable from the codebase.
 *
 * Google Play permits gambling ads inside a non-gambling app only on conditions, one of which
 * this app fails by design: the app "must not provide ... companion functionality (for example,
 * functionality that assists with wagering, payouts, SPORTS SCORE/ODDS/PERFORMANCE TRACKING...)".
 * Play's own violation example is "a dedicated sports odds tracker app containing integrated
 * gambling ads linking to a sports betting site" — which is this app plus gambling ads.
 *
 * Apple arrives at the same place from the other direction: guideline 2.5.18 requires ads to be
 * "appropriate for the app's age rating", and a developer reported an app with NO gambling
 * functionality rejected under 5.3.4 purely for carrying gambling ad banners — with the
 * rejection surviving their removal.
 *
 * AdMob blocks "Gambling & Betting (18+)" by default. The correct action is to leave it blocked
 * and never opt in for the higher eCPM.
 */
export const NEVER_ALLOWED_AD_CATEGORIES = ["gambling_and_betting"] as const;

/** Apple guideline 2.5.18: "Apps that contain ads must also include the ability for users to
 * report any inappropriate or age-inappropriate ads."
 *
 * A hard requirement, commonly missed, and it is a UI affordance rather than a network setting —
 * so it has to be built. Wired when a provider exists; flagged here so it is not discovered at
 * submission. */
export const REQUIRES_REPORT_AD_CONTROL = true;

/** Google's certified-CMP requirement: as of 16 January 2024 a TCF-integrated consent flow is
 * required to serve PERSONALIZED ads to users in the EEA, UK and (from July 2024) Switzerland.
 * Without it those users are downgraded to non-personalized or limited ads rather than cut off.
 *
 * `react-native-google-mobile-ads` bundles Google's UMP SDK, so no third-party CMP is needed —
 * but its own docs are blunt that it "only provides you with the tools", and honouring the
 * resulting status is the app's job. Nigeria is outside Google's mandate; its own NDPA is a
 * separate legal question and not one this file answers. */
export const CONSENT_REQUIRED_REGIONS = ["EEA", "UK", "CH"] as const;

/** Frequency discipline (design spec §3.4), kept as prose because each rule binds a different
 * part of the app and none of them is a number:
 *
 *   - No interstitial on a screen transition. The only full-screen unit permitted is a REWARDED
 *     one the user opted into.
 *   - Never two units visible at once.
 *   - Nothing between every few prediction cards — see placement.ts for the cadence.
 *   - Nothing placed over a statistic.
 *   - Collapse any slot the adapter cannot fill, rather than reserving empty space.
 */
export const FREQUENCY_RULES_DOCUMENTED = true;
