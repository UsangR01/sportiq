import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { FlatList, RefreshControl, Text, View } from "react-native";

import { FixtureCard } from "@/components/fixtures/FixtureCard";
import { GuestBanner } from "@/components/GuestBanner";
import { SportFilterChips } from "@/components/SportFilterChips";
import { listFixtures } from "@/lib/api/fixtures";
import { listSports } from "@/lib/api/sports";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

export default function HomeScreen() {
  const isGuest = useAuthStore((s) => s.accessToken === null);
  const sportFilter = usePreferencesStore((s) => s.sportFilter);
  const setSportFilter = usePreferencesStore((s) => s.setSportFilter);
  const [refreshing, setRefreshing] = useState(false);

  const sportsQuery = useQuery({ queryKey: ["sports"], queryFn: listSports });
  const fixturesQuery = useQuery({
    queryKey: ["fixtures", "home", sportFilter],
    queryFn: () =>
      listFixtures({ sport_slug: sportFilter ?? undefined, limit: 50 }),
  });

  async function onRefresh() {
    setRefreshing(true);
    await fixturesQuery.refetch();
    setRefreshing(false);
  }

  const fixtures = fixturesQuery.data ?? [];
  const live = fixtures.filter((f) => f.status === "live");
  const upcoming = fixtures.filter((f) => f.status !== "live");
  const ordered = [...live, ...upcoming];

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <SportFilterChips
        sports={sportsQuery.data ?? []}
        selected={sportFilter}
        onSelect={setSportFilter}
      />
      {isGuest && <GuestBanner />}
      <FlatList
        data={ordered}
        keyExtractor={(item) => item.id}
        contentContainerClassName="px-4 pb-4"
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
        }
        renderItem={({ item }) => <FixtureCard fixture={item} />}
        ListEmptyComponent={
          fixturesQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading fixtures…</Text>
          ) : fixturesQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportIQ API. Pull to retry.
            </Text>
          ) : (
            <Text className="mt-8 text-center text-gray-400">
              No upcoming fixtures for this filter.
            </Text>
          )
        }
      />
    </View>
  );
}
