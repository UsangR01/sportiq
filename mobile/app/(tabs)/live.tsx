import { useQuery } from "@tanstack/react-query";
import { FlatList, Text, View } from "react-native";

import { FixtureCard } from "@/components/fixtures/FixtureCard";
import { listFixtures } from "@/lib/api/fixtures";

export default function LiveScreen() {
  const liveQuery = useQuery({
    queryKey: ["fixtures", "live"],
    queryFn: () => listFixtures({ status: "live", limit: 100 }),
    refetchInterval: 60 * 1000,
  });

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <FlatList
        data={liveQuery.data ?? []}
        keyExtractor={(item) => item.id}
        contentContainerClassName="px-4 pt-2 pb-4"
        renderItem={({ item }) => <FixtureCard fixture={item} />}
        ListEmptyComponent={
          liveQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading…</Text>
          ) : liveQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportPIQ API.
            </Text>
          ) : (
            <Text className="mt-8 text-center text-gray-400">
              Nothing is live right now.
            </Text>
          )
        }
      />
    </View>
  );
}
