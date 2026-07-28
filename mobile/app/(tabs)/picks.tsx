import Slider from "@react-native-community/slider";
import { useQuery } from "@tanstack/react-query";
import * as Haptics from "expo-haptics";
import { useRef } from "react";
import { FlatList, Platform, Text, View } from "react-native";

import { SportFilterChips } from "@/components/SportFilterChips";
import { getPicks } from "@/lib/api/picks";
import { listSports } from "@/lib/api/sports";
import type { PickResponse } from "@/lib/api/types";
import { usePreferencesStore } from "@/store/preferencesStore";

const EV_COLORS: Record<"positive" | "neutral" | "negative", { bg: string; text: string }> = {
  positive: { bg: "bg-green-100 dark:bg-green-900", text: "text-green-800 dark:text-green-200" },
  neutral: { bg: "bg-gray-100 dark:bg-gray-800", text: "text-gray-700 dark:text-gray-300" },
  negative: { bg: "bg-red-100 dark:bg-red-900", text: "text-red-800 dark:text-red-200" },
};

function evTier(expectedValue: number): "positive" | "neutral" | "negative" {
  if (expectedValue > 0.02) return "positive";
  if (expectedValue < -0.02) return "negative";
  return "neutral";
}

function PickRow({ pick }: { pick: PickResponse }) {
  const tier = evTier(pick.expected_value);
  return (
    <View className="mb-2 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
      <View className="mb-1 flex-row items-center justify-between">
        <Text className="text-xs uppercase text-gray-400">{pick.sport_slug}</Text>
        <View className={`rounded-full px-2 py-0.5 ${EV_COLORS[tier].bg}`}>
          <Text className={`text-xs font-semibold ${EV_COLORS[tier].text}`}>
            EV {(pick.expected_value * 100).toFixed(1)}%
          </Text>
        </View>
      </View>
      <Text className="text-base font-semibold text-gray-900 dark:text-gray-100">
        {pick.home_team} vs {pick.away_team}
      </Text>
      <Text className="mt-1 text-sm text-gray-600 dark:text-gray-400">
        {pick.selection.toUpperCase()} @ {pick.odds.toFixed(2)} · {(pick.model_probability * 100).toFixed(0)}%
        · {pick.confidence_tier}
      </Text>
    </View>
  );
}

export default function PicksScreen() {
  const sportFilter = usePreferencesStore((s) => s.sportFilter);
  const setSportFilter = usePreferencesStore((s) => s.setSportFilter);
  const minOdds = usePreferencesStore((s) => s.minOdds);
  const setMinOdds = usePreferencesStore((s) => s.setMinOdds);

  const sportsQuery = useQuery({ queryKey: ["sports"], queryFn: listSports });
  const picksQuery = useQuery({
    queryKey: ["picks", sportFilter, minOdds],
    queryFn: () => getPicks({ min_odds: minOdds, sport_slug: sportFilter ?? undefined }),
  });

  // @react-native-community/slider's Android SeekBar backing view fires onSlidingComplete
  // once on mount with no real touch involved (confirmed live: it reported minimumValue,
  // silently overwriting a fresh guest session's min_odds before the user ever touched the
  // slider). Only commit a completion that was preceded by a real onSlidingStart.
  const hasStartedSliding = useRef(false);

  function onSlidingStart() {
    hasStartedSliding.current = true;
  }

  function onSlidingComplete(value: number) {
    if (!hasStartedSliding.current) return;
    hasStartedSliding.current = false;
    const rounded = Math.round(value * 100) / 100;
    setMinOdds(rounded);
    if (Platform.OS !== "web") {
      Haptics.selectionAsync();
    }
  }

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <View className="px-4 pt-2">
        <Text className="text-sm text-gray-500 dark:text-gray-400">
          Minimum odds: <Text className="font-semibold text-gray-900 dark:text-gray-100">{minOdds.toFixed(2)}</Text>
        </Text>
        <Slider
          minimumValue={1.01}
          maximumValue={20}
          step={0.01}
          value={minOdds}
          onSlidingStart={onSlidingStart}
          onSlidingComplete={onSlidingComplete}
          minimumTrackTintColor="#2563eb"
        />
      </View>
      <SportFilterChips
        sports={sportsQuery.data ?? []}
        selected={sportFilter}
        onSelect={setSportFilter}
      />
      <FlatList
        data={picksQuery.data ?? []}
        keyExtractor={(item) => `${item.fixture_id}-${item.selection}`}
        contentContainerClassName="px-4 pb-4"
        renderItem={({ item }) => <PickRow pick={item} />}
        ListEmptyComponent={
          picksQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading picks…</Text>
          ) : picksQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportIQ API.
            </Text>
          ) : (
            <Text className="mt-8 text-center text-gray-400">
              No fixtures clear this odds threshold right now.
            </Text>
          )
        }
      />
    </View>
  );
}
