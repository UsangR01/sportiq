import { Pressable, Text, View } from "react-native";

import { CountryFlag } from "@/lib/countryFlags";
import { CONTROL, GAP, ONE_LINE, RADIUS, TYPE, useTheme } from "@/lib/theme";

/** A league's header row, which sits OUTSIDE the card (design spec §3.2).
 *
 * Outside on purpose: the card holds matches, and a header inside it would read as another
 * row. Keeping it out is what makes a group scan as "this league, then its matches".
 *
 * The badge is a real flag or competition image, not a letter code — the app already bundles
 * them, and for the UEFA competitions (whose country is "Europe") a competition badge says
 * something a flag cannot.
 */
export function LeagueGroupHeader({
  title,
  country,
  leagueSlug,
  surface,
  timeUnconfirmed,
  starred,
  onToggleStar,
}: {
  title: string;
  country: string | null;
  leagueSlug: string | null;
  /** Court surface — tennis only, shown beside the country. */
  surface: string | null;
  /** Every fixture here has only an estimated kickoff. */
  timeUnconfirmed: boolean;
  starred: boolean;
  onToggleStar: () => void;
}) {
  const { colors } = useTheme();

  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 10, marginBottom: 8 }}>
      <CountryFlag country={country} leagueSlug={leagueSlug} size={22} />

      <View style={{ flex: 1 }}>
        {/* Says outright that these have no confirmed time, so the day they appear under is a
            fallback rather than a scheduled slot — better than implying a precision the
            provider never gave us. */}
        {timeUnconfirmed && (
          <Text style={[TYPE.eyebrowSmall, { color: colors.warn }]}>Time to be confirmed</Text>
        )}
        <Text {...ONE_LINE} style={[TYPE.pick, { color: colors.text, fontSize: 14 }]}>
          {title}
        </Text>
        {(country || surface) && (
          <Text {...ONE_LINE} style={[TYPE.caption, { color: colors.textFaint, fontWeight: "400" }]}>
            {[country, surface].filter(Boolean).join(" · ")}
          </Text>
        )}
      </View>

      <Pressable
        onPress={onToggleStar}
        accessibilityRole="button"
        accessibilityLabel={starred ? `Unstar ${title}` : `Star ${title}`}
        accessibilityState={{ selected: starred }}
        style={{
          width: CONTROL.star,
          height: CONTROL.star,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Text style={{ fontSize: 17, color: starred ? colors.star : colors.textFaint }}>
          {starred ? "★" : "☆"}
        </Text>
      </Pressable>
    </View>
  );
}

/** The card that holds a league's matches as rows. One surface, hairline-separated inside. */
export function LeagueCard({ children }: { children: React.ReactNode }) {
  const { colors, elevation } = useTheme();
  return (
    <View
      style={{
        backgroundColor: colors.surface,
        borderRadius: RADIUS.card,
        borderWidth: 1,
        borderColor: colors.border,
        overflow: "hidden",
        marginBottom: GAP.leagueGroup,
        ...elevation.card,
      }}
    >
      {children}
    </View>
  );
}
