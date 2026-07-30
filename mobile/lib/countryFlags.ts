// Small, hardcoded map — this app only ever has a handful of real leagues/countries (8
// football leagues + the US for NBA), so a tiny lookup table is simpler and more reliable
// than pulling in a full country->ISO-code->flag library for one screen. Every value here
// must be a single real flag emoji — see countryFlag()'s own comment for why the render site
// (not this map) is what actually guarantees uniform on-screen size across all of them.
const COUNTRY_FLAGS: Record<string, string> = {
  England: "🏴",
  France: "🇫🇷",
  Germany: "🇩🇪",
  Spain: "🇪🇸",
  Italy: "🇮🇹",
  Brazil: "🇧🇷",
  USA: "🇺🇸",
  Scotland: "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
  China: "🇨🇳",
};

// 🌍 for a null/unrecognised country (e.g. a global competition) rather than nothing — matches
// the reference app's globe icon for competitions like the Champions League. A previously-
// missing country here (Scotland/China, before the 3-new-leagues addition) silently fell back
// to this globe icon instead of a real flag — the actual bug the map above now fixes.
export function countryFlag(country: string | null): string {
  if (!country) return "🌍";
  return COUNTRY_FLAGS[country] ?? "🌍";
}
