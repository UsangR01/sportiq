import { Image, Text, View } from "react-native";

// Real flag PNGs (public-domain national symbols, sourced from flagcdn.com), bundled locally —
// not Unicode flag emoji. Confirmed live via a real screenshot: Unicode regional-indicator flag
// sequences (🇧🇷, 🇨🇳, ...) don't compose into a flag glyph at all in this environment's font
// stack — they render as the raw two-letter code text ("BR", "CN"), and Scotland's flag is a
// 7-codepoint TAG sequence that degrades even further, to a bare black flag with nothing
// recognisable at all. No font/platform choice fixes this — it's a missing-glyph problem, not
// a wrong-emoji problem — so real images are the only reliable fix. This app only ever has a
// handful of real leagues/countries, so a tiny lookup table of small bundled PNGs is simpler
// and more reliable than a full flag-icon library or a live CDN fetch (which would need network
// access just to show a badge, and wouldn't work offline).
const COUNTRY_FLAG_IMAGES: Record<string, number> = {
  England: require("../assets/flags/england.png"),
  France: require("../assets/flags/france.png"),
  Germany: require("../assets/flags/germany.png"),
  Spain: require("../assets/flags/spain.png"),
  Italy: require("../assets/flags/italy.png"),
  Brazil: require("../assets/flags/brazil.png"),
  USA: require("../assets/flags/usa.png"),
  Scotland: require("../assets/flags/scotland.png"),
  China: require("../assets/flags/china.png"),
  // Added for tennis: an ATP season visits ~30 countries, far more than football's handful of
  // leagues. Every one of these corresponds to a real tournament location in
  // TOURNAMENT_LOCATION_COUNTRIES below.
  Netherlands: require("../assets/flags/netherlands.png"),
  Mexico: require("../assets/flags/mexico.png"),
  Australia: require("../assets/flags/australia.png"),
  Kazakhstan: require("../assets/flags/kazakhstan.png"),
  "New Zealand": require("../assets/flags/newzealand.png"),
  Switzerland: require("../assets/flags/switzerland.png"),
  Sweden: require("../assets/flags/sweden.png"),
  Belgium: require("../assets/flags/belgium.png"),
  Romania: require("../assets/flags/romania.png"),
  Argentina: require("../assets/flags/argentina.png"),
  Qatar: require("../assets/flags/qatar.png"),
  UAE: require("../assets/flags/uae.png"),
  Portugal: require("../assets/flags/portugal.png"),
  Austria: require("../assets/flags/austria.png"),
  Morocco: require("../assets/flags/morocco.png"),
  Monaco: require("../assets/flags/monaco.png"),
  Canada: require("../assets/flags/canada.png"),
  Chile: require("../assets/flags/chile.png"),
  Japan: require("../assets/flags/japan.png"),
  Croatia: require("../assets/flags/croatia.png"),
  "Hong Kong": require("../assets/flags/hongkong.png"),
};

/** Tennis tournament CITY -> country. BallDontLie exposes a tournament's `location` as a city
 * ("Montreal", "Indian Wells") and has NO country field at all, so this mapping has to live
 * client-side — it can't be read off the API.
 *
 * Every key below is a real, live-confirmed location string taken from the actual ATP
 * /tournaments response (all 60 distinct values for the current season), not guessed. It is
 * therefore hand-maintained: a genuinely new host city will fall through to the 🌍 globe
 * rather than showing a wrong flag, which is the safe direction to fail. "Multiple Locations"
 * (Davis Cup) is deliberately absent — it spans several countries, so the globe is correct. */
const TOURNAMENT_LOCATION_COUNTRIES: Record<string, string> = {
  "'s-Hertogenbosch": "Netherlands",
  Rotterdam: "Netherlands",
  Acapulco: "Mexico",
  "Los Cabos": "Mexico",
  Adelaide: "Australia",
  Brisbane: "Australia",
  Melbourne: "Australia",
  "Perth-Sydney": "Australia",
  Almaty: "Kazakhstan",
  Auckland: "New Zealand",
  Barcelona: "Spain",
  Madrid: "Spain",
  Mallorca: "Spain",
  Basel: "Switzerland",
  Geneva: "Switzerland",
  Gstaad: "Switzerland",
  Bastad: "Sweden",
  Stockholm: "Sweden",
  Beijing: "China",
  Chengdu: "China",
  Hangzhou: "China",
  Shanghai: "China",
  Bologna: "Italy",
  Rome: "Italy",
  Turin: "Italy",
  Brussels: "Belgium",
  Bucharest: "Romania",
  "Buenos Aires": "Argentina",
  Cincinnati: "USA",
  Dallas: "USA",
  "Delray Beach": "USA",
  Houston: "USA",
  "Indian Wells": "USA",
  Miami: "USA",
  "New York": "USA",
  Washington: "USA",
  "Winston-Salem": "USA",
  Doha: "Qatar",
  Dubai: "UAE",
  Eastbourne: "England",
  London: "England",
  Estoril: "Portugal",
  Halle: "Germany",
  Hamburg: "Germany",
  Munich: "Germany",
  Stuttgart: "Germany",
  "Hong Kong": "Hong Kong",
  Kitzbuhel: "Austria",
  Vienna: "Austria",
  Lyon: "France",
  Montpellier: "France",
  Paris: "France",
  Marrakech: "Morocco",
  "Monte-Carlo": "Monaco",
  Montreal: "Canada",
  "Rio de Janeiro": "Brazil",
  Santiago: "Chile",
  Tokyo: "Japan",
  Umag: "Croatia",
};

/** The country hosting a tennis tournament, from its city. Null (-> 🌍 globe) for an
 * unmapped/multi-country location — never a guessed flag. */
export function countryForTournamentLocation(location: string | null): string | null {
  if (!location) return null;
  return TOURNAMENT_LOCATION_COUNTRIES[location] ?? null;
}

/** A country's real flag, or a 🌍 globe for a null/unrecognised country (e.g. a global
 * competition) — matches the reference app's globe icon for competitions like the Champions
 * League. `size` is both width AND height: real flags have different aspect ratios (Brazil's
 * is far more square than the UK's or the US's), so every flag is center-cropped
 * (resizeMode="cover") to fill the exact same square rather than being stretched/distorted or
 * left with mismatched letterboxing — this is what actually guarantees every flag looks the
 * same size, not just occupies the same layout box. */
export function CountryFlag({ country, size = 24 }: { country: string | null; size?: number }) {
  const source = country ? COUNTRY_FLAG_IMAGES[country] : undefined;
  if (!source) {
    return (
      <View style={{ width: size, height: size }} className="items-center justify-center">
        <Text style={{ fontSize: size * 0.75, lineHeight: size }}>🌍</Text>
      </View>
    );
  }
  return (
    <Image
      source={source}
      style={{ width: size, height: size, borderRadius: 3 }}
      resizeMode="cover"
    />
  );
}
