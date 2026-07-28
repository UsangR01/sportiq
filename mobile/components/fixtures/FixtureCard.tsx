import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import type { FixtureSummary } from "@/lib/api/types";
import { LiveBadge } from "./LiveBadge";

export function FixtureCard({ fixture }: { fixture: FixtureSummary }) {
  const kickoff = new Date(fixture.kickoff_utc);
  const isLive = fixture.status === "live";

  return (
    <Link href={`/fixture/${fixture.id}`} asChild>
      <Pressable className="mb-2 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
        <View className="mb-1 flex-row items-center justify-between">
          <Text className="text-xs uppercase text-gray-400">{fixture.sport_slug}</Text>
          {isLive ? (
            <LiveBadge />
          ) : (
            <Text className="text-xs text-gray-400">
              {kickoff.toLocaleDateString()} {kickoff.toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </Text>
          )}
        </View>
        <Text className="text-base font-semibold text-gray-900 dark:text-gray-100">
          {fixture.home_team} vs {fixture.away_team}
        </Text>
      </Pressable>
    </Link>
  );
}
