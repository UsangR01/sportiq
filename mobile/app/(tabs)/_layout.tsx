import { Tabs } from "expo-router";
import { Text } from "react-native";
import type { ColorValue } from "react-native";

import {
  LiveGlyph,
  PicksGlyph,
  PremiumGlyph,
  ProfileGlyph,
  SavedGlyph,
} from "@/components/tabs/TabGlyphs";
import { SCREEN, TYPE, useTheme } from "@/lib/theme";

/** Five tabs (design spec §2).
 *
 * Glyphs are drawn to the spec's own geometry rather than pulled from a platform symbol set —
 * see components/tabs/TabGlyphs.tsx for why SymbolView could not match the design on more than
 * one platform at a time.
 *
 * At 390px this is ~78px per tab, which fits an 11px label without truncation.
 *
 * DELIBERATELY the JS `Tabs` component rather than SDK 57's `NativeTabs` — checked against the
 * v57 docs, not assumed. NativeTabs wraps the platform tab bar and accepts only SF Symbols,
 * Material icons, drawables or image sources as icons; it cannot render an arbitrary React
 * component, and it does not expose the bar's background, border and padding. This design
 * specifies all four, so the native bar cannot express it.
 *
 * Headers stay off: every screen renders the wordmark itself (spec §10), so the router header
 * would sit in a separate band above the design's own header.
 */
/** The tab label, rendered by us rather than by React Navigation.
 *
 * The design specifies the label's size, weight, tracking and colour exactly (spec §1.2), and
 * `tabBarLabelStyle` is merged into an element whose height React Navigation sets itself.
 * Supplying `tabBarLabel` replaces that element outright, so what ships is what the spec says
 * rather than what survives a merge. */
function TabLabel({ label, color }: { label: string; color: ColorValue }) {
  return (
    <Text numberOfLines={1} style={[TYPE.tabLabel, { color, textAlign: "center" }]}>
      {label}
    </Text>
  );
}

export default function TabLayout() {
  const { colors } = useTheme();

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textFaint,
        tabBarStyle: {
          backgroundColor: colors.surface,
          borderTopColor: colors.border,
          borderTopWidth: 1,
          paddingTop: 8,
          paddingHorizontal: 8,
          // Clears the home indicator; without it the labels sit under the gesture bar.
          paddingBottom: SCREEN.tabBarPaddingBottom,
          height: 58 + SCREEN.tabBarPaddingBottom,
          // The design's own 1px rule is the separator; RN's default elevation would add a
          // second, heavier one on Android and make the bar look detached from the screen.
          elevation: 0,
          shadowOpacity: 0,
        },
      }}
    >
      {/* Home and Picks are one tab: "I don't think we need two different pages - home and
          picks." index.tsx IS the Picks feed. */}
      <Tabs.Screen
        name="index"
        options={{
          title: "Picks",
          tabBarLabel: ({ color }) => <TabLabel label="Picks" color={color} />,
          tabBarIcon: ({ color }) => <PicksGlyph color={color} />,
        }}
      />
      <Tabs.Screen
        name="live"
        options={{
          title: "Live",
          tabBarLabel: ({ color }) => <TabLabel label="Live" color={color} />,
          tabBarIcon: ({ color }) => <LiveGlyph color={color} />,
        }}
      />
      {/* Top calls — the premium surface. Placed centre-right rather than last so it sits in
          the thumb's reach without displacing Profile, which users expect at the end. */}
      <Tabs.Screen
        name="premium"
        options={{
          title: "Top calls",
          tabBarLabel: ({ color }) => <TabLabel label="Top calls" color={color} />,
          tabBarIcon: ({ color }) => <PremiumGlyph color={color} />,
        }}
      />
      {/* Saved keeps its own tab. The Hub's SAVED block is a second route to the same screen,
          not a replacement — saved picks are opened often enough to earn a permanent slot. */}
      <Tabs.Screen
        name="saved"
        options={{
          title: "Saved",
          tabBarLabel: ({ color }) => <TabLabel label="Saved" color={color} />,
          tabBarIcon: ({ color }) => <SavedGlyph color={color} />,
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: "Profile",
          tabBarLabel: ({ color }) => <TabLabel label="Profile" color={color} />,
          tabBarIcon: ({ color }) => <ProfileGlyph color={color} />,
        }}
      />
    </Tabs>
  );
}
