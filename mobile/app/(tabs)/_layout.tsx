import { SymbolView } from "expo-symbols";
import { Tabs } from "expo-router";

import Colors from "@/constants/Colors";
import { useColorScheme } from "@/components/useColorScheme";

export default function TabLayout() {
  const colorScheme = useColorScheme();

  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: Colors[colorScheme].tint,
        // The Picks screen renders its own AppHeader so the title, sport/date controls and
        // filters read as one surface; the router header sat in a separate band above them.
        headerShown: false,
      }}
    >
      {/* Home and Picks were merged into one tab — the user's own words: "I don't think we
          need two different pages - home and picks." index.tsx now IS the Picks feed
          (day-strip/league-grouping retained per their explicit follow-up, but only surfacing
          fixtures whose best pick — across every market — clears a real probability/odds
          floor, not a general schedule browser). */}
      <Tabs.Screen
        name="index"
        options={{
          title: "Picks",
          tabBarIcon: ({ color }) => (
            <SymbolView
              name={{ ios: "target", android: "my_location", web: "my_location" }}
              tintColor={color}
              size={26}
            />
          ),
        }}
      />
      <Tabs.Screen
        name="live"
        options={{
          title: "Live",
          tabBarIcon: ({ color }) => (
            <SymbolView
              name={{
                ios: "dot.radiowaves.left.and.right",
                android: "podcasts",
                web: "podcasts",
              }}
              tintColor={color}
              size={26}
            />
          ),
        }}
      />
      {/* Saved shows each fixture's pick AS IT WAS WHEN SAVED — a receipt. best_pick is
          recomputed per request and never stored, so the Picks feed can legitimately show a
          different call later; this is the one place a call is frozen. */}
      <Tabs.Screen
        name="saved"
        options={{
          title: "Saved",
          tabBarIcon: ({ color }) => (
            <SymbolView
              name={{ ios: "bookmark.fill", android: "bookmark", web: "bookmark" }}
              tintColor={color}
              size={26}
            />
          ),
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: "Profile",
          tabBarIcon: ({ color }) => (
            <SymbolView
              name={{ ios: "person.fill", android: "person", web: "person" }}
              tintColor={color}
              size={26}
            />
          ),
        }}
      />
    </Tabs>
  );
}
