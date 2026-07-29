// Small, hardcoded map — this app only ever has a handful of real leagues/countries (5
// European football leagues + Brazil + the US for NBA), so a tiny lookup table is simpler
// and more reliable than pulling in a full country->ISO-code->flag library for one screen.
const COUNTRY_FLAGS: Record<string, string> = {
  England: "🏴",
  France: "🇫🇷",
  Germany: "🇩🇪",
  Spain: "🇪🇸",
  Italy: "🇮🇹",
  Brazil: "🇧🇷",
  USA: "🇺🇸",
};

// 🌍 for a null/unrecognised country (e.g. a global competition) rather than nothing — matches
// the reference app's globe icon for competitions like the Champions League.
export function countryFlag(country: string | null): string {
  if (!country) return "🌍";
  return COUNTRY_FLAGS[country] ?? "🌍";
}
