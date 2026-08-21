/** One converter for every price on screen (design spec §9.3, §10).
 *
 * The backend stores and serves DECIMAL odds, always — that is what the model, the EV maths and
 * every guard reason about. This is a display concern only: nothing here ever feeds a
 * calculation, and no caller should convert back.
 *
 * Every price the user sees goes through this: the feed pick line, the filter sheet's
 * minimum-odds label, the expanded panel, Top calls, and saved receipts. §10 requires the Hub's
 * format switch to change all of them, which only holds if there is exactly one function.
 */

export type OddsFormat = "EU" | "UK" | "US";

const FORMATS: OddsFormat[] = ["EU", "UK", "US"];

export function isOddsFormat(value: string | null | undefined): value is OddsFormat {
  return value != null && (FORMATS as string[]).includes(value);
}

/** The stored preference, narrowed. Anything unrecognised falls back to decimal rather than
 * throwing — a bad preference must not blank out every price in the app. */
export function toOddsFormat(value: string | null | undefined): OddsFormat {
  return isOddsFormat(value) ? value : "EU";
}

/** Largest denominator considered when approximating a fraction (spec §9.3: 1–20).
 *
 * Bounded on purpose. An unbounded best-fit gives technically-closer but unreadable fractions
 * like 137/98; real bookmakers quote from a conventional ladder, so a slightly less exact
 * 7/5 is the more useful answer. */
const MAX_DENOMINATOR = 20;

/** Decimal → the nearest clean fraction, as UK books quote it.
 *
 * Fractional odds express PROFIT against stake, so the decimal's 1.0 stake is removed first:
 * 1.50 → 0.5 → 1/2. Searches every denominator up to MAX_DENOMINATOR and keeps the closest,
 * preferring the smaller denominator on a tie because that is the one a book would print. */
function toFraction(decimal: number): string {
  const profit = decimal - 1;
  let best = { numerator: 1, denominator: 1, error: Number.POSITIVE_INFINITY };
  for (let denominator = 1; denominator <= MAX_DENOMINATOR; denominator += 1) {
    const numerator = Math.round(profit * denominator);
    if (numerator < 1) continue;
    const error = Math.abs(profit - numerator / denominator);
    if (error < best.error - 1e-9) best = { numerator, denominator, error };
  }
  return `${best.numerator}/${best.denominator}`;
}

/** Decimal → American (moneyline).
 *
 * The convention flips at evens: a price of 2.00 or better is quoted as the profit on a 100
 * stake (+150), and anything shorter as the stake needed to profit 100 (−200). */
function toAmerican(decimal: number): string {
  if (decimal >= 2) return `+${Math.round((decimal - 1) * 100)}`;
  return `−${Math.round(100 / (decimal - 1))}`; // U+2212 minus, not a hyphen
}

/** Format a decimal price for display.
 *
 * Returns an em dash for a missing price rather than "0.00" or an empty string: a fixture with
 * no odds is a real and common state (tennis has no odds coverage in some windows, and a
 * league's provider may not price a market at all), and it must read as ABSENT rather than as
 * a price of zero. */
export function formatOdds(decimal: number | null | undefined, format: OddsFormat): string {
  if (decimal == null || !Number.isFinite(decimal) || decimal <= 1) return "—";
  switch (format) {
    case "UK":
      return toFraction(decimal);
    case "US":
      return toAmerican(decimal);
    case "EU":
    default:
      return decimal.toFixed(2);
  }
}

/** The worked example beside each option in the Hub's format switch (spec §7.4), so the choice
 * is legible without having to try it. All three describe the same 1.50 price. */
export const FORMAT_EXAMPLES: Record<OddsFormat, string> = {
  EU: "e.g. 1.50",
  UK: "e.g. 1/2",
  US: "e.g. −200",
};
