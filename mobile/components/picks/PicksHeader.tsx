import { Pressable, Text, View } from "react-native";

import { CONTROL, GAP, ONE_LINE, RADIUS, SCREEN, TYPE, useTheme, useScreenInsets } from "@/lib/theme";

/** The fixed, non-scrolling Picks header (design spec §3.1 row 1).
 *
 * Rows 2–4 (date stepper, summary strip, segmented control) are composed by the screen and
 * passed as `children`, so this file owns only the identity row and the header's frame. That
 * keeps the row that never changes separate from the three that depend on the day's data.
 */
export function PicksHeader({
  isPremium,
  isDark,
  onToggleTheme,
  onOpenHub,
  onOpenFilters,
  filtersActive,
  children,
}: {
  isPremium: boolean;
  isDark: boolean;
  onToggleTheme: () => void;
  onOpenHub: () => void;
  onOpenFilters: () => void;
  /** Any filter off its default — drives the accent dot. */
  filtersActive: boolean;
  children?: React.ReactNode;
}) {
  const { colors } = useTheme();
  const insets = useScreenInsets();

  return (
    <View
      style={{
        paddingTop: insets.top,
        paddingHorizontal: SCREEN.padding,
        paddingBottom: 12,
        backgroundColor: colors.bg,
        gap: GAP.headerControl,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
        <IconButton onPress={onOpenHub} label="Open menu">
          {/* Two bars rather than three: at 15×1.8 a third crowds the 36px button, and two
              reads as a menu just as clearly. */}
          <View style={{ gap: 4 }}>
            {[0, 1].map((i) => (
              <View key={i} style={{ width: 15, height: 1.8, backgroundColor: colors.text }} />
            ))}
          </View>
        </IconButton>

        <Text {...ONE_LINE} style={[TYPE.wordmark, { color: colors.text, flex: 1 }]}>
          SportPIQ
        </Text>

        {/* Only when subscribed — an always-visible PRO pill would advertise a state the user
            is not in, which reads as a nag rather than a status. */}
        {isPremium && (
          <View
            style={{
              paddingHorizontal: 7,
              paddingVertical: 3,
              borderRadius: RADIUS.badge,
              backgroundColor: colors.accentSoft,
            }}
          >
            <Text style={[TYPE.eyebrowSmall, { color: colors.accent }]}>PRO</Text>
          </View>
        )}

        <IconButton onPress={onToggleTheme} label="Toggle theme">
          <Text style={{ fontSize: 15, color: colors.text }}>{isDark ? "☀" : "☾"}</Text>
        </IconButton>

        <Pressable
          onPress={onOpenFilters}
          accessibilityRole="button"
          style={{
            flexDirection: "row",
            alignItems: "center",
            gap: 6,
            paddingHorizontal: 11,
            height: CONTROL.iconButton,
            borderRadius: RADIUS.button,
            backgroundColor: colors.surfaceAlt,
          }}
        >
          <Text style={[TYPE.caption, { color: colors.text, fontWeight: "700" }]}>Filters</Text>
          {filtersActive && (
            <View
              style={{ width: 5, height: 5, borderRadius: 2.5, backgroundColor: colors.accent }}
            />
          )}
        </Pressable>
      </View>

      {children}
    </View>
  );
}

function IconButton({
  onPress,
  label,
  children,
}: {
  onPress: () => void;
  label: string;
  children: React.ReactNode;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      style={{
        width: CONTROL.iconButton,
        height: CONTROL.iconButton,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: RADIUS.button,
        backgroundColor: colors.surfaceAlt,
      }}
    >
      {children}
    </Pressable>
  );
}
