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
};

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
