import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { ScrollView, Text, View } from "react-native";

import { LiveBadge } from "@/components/fixtures/LiveBadge";
import { getFixture } from "@/lib/api/fixtures";

export default function FixtureDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const fixtureQuery = useQuery({
    queryKey: ["fixture", id],
    queryFn: () => getFixture(id),
    enabled: !!id,
  });

  if (fixtureQuery.isLoading) {
    return (
      <View className="flex-1 items-center justify-center bg-white dark:bg-black">
        <Text className="text-gray-400">Loading…</Text>
      </View>
    );
  }

  if (fixtureQuery.isError || !fixtureQuery.data) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="text-center text-red-500">Couldn&apos;t load this fixture.</Text>
      </View>
    );
  }

  const fixture = fixtureQuery.data;
  const kickoff = new Date(fixture.kickoff_utc);

  return (
    <ScrollView className="flex-1 bg-white dark:bg-black" contentContainerClassName="p-4">
      <View className="mb-4 flex-row items-center justify-between">
        <Text className="text-xs uppercase text-gray-400">
          {fixture.sport_slug} · {fixture.league_slug}
        </Text>
        {fixture.status === "live" ? (
          <LiveBadge />
        ) : (
          <Text className="text-xs text-gray-400">{fixture.status}</Text>
        )}
      </View>

      <Text className="mb-1 text-2xl font-bold text-gray-900 dark:text-gray-100">
        {fixture.home_team} vs {fixture.away_team}
      </Text>
      <Text className="mb-6 text-sm text-gray-500 dark:text-gray-400">
        {kickoff.toLocaleString()}
      </Text>

      {fixture.live_state && (
        <View className="mb-6 rounded-xl bg-gray-50 p-4 dark:bg-gray-900">
          <Text className="text-center text-3xl font-bold text-gray-900 dark:text-gray-100">
            {fixture.live_state.home_score} – {fixture.live_state.away_score}
          </Text>
          <Text className="mt-1 text-center text-sm text-gray-500 dark:text-gray-400">
            {[fixture.live_state.period, fixture.live_state.match_minute && `${fixture.live_state.match_minute}'`]
              .filter(Boolean)
              .join(" · ")}
          </Text>
        </View>
      )}

      {fixture.prediction && (
        <View className="mb-6">
          <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">
            Model Prediction
          </Text>
          <ProbabilityBar
            homeProb={fixture.prediction.home_prob}
            drawProb={fixture.prediction.draw_prob}
            awayProb={fixture.prediction.away_prob}
          />
          <Text className="mt-2 text-xs text-gray-400">
            {fixture.prediction.model_version} · {fixture.prediction.confidence_tier} confidence
          </Text>
        </View>
      )}

      {fixture.odds.length > 0 && (
        <View className="mb-6">
          <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">Odds</Text>
          {fixture.odds.map((line, i) => (
            <View
              key={`${line.bookmaker}-${i}`}
              className="mb-1 flex-row justify-between border-b border-gray-100 py-2 dark:border-gray-800"
            >
              <Text className="text-gray-700 dark:text-gray-300">{line.bookmaker}</Text>
              <Text className="text-gray-900 dark:text-gray-100">
                {[line.home_odds, line.draw_odds, line.away_odds]
                  .filter((v): v is number => v !== null)
                  .map((v) => v.toFixed(2))
                  .join(" / ")}
              </Text>
            </View>
          ))}
        </View>
      )}

      {/* Highlight clips (TDD §5.3 Option A) and the animated match tracker (Option B,
          Phase 2) aren't wired up yet — Highlightly isn't integrated at the backend ingest
          layer, and there's no live WebSocket feed for the tracker to consume. */}
    </ScrollView>
  );
}

function ProbabilityBar({
  homeProb,
  drawProb,
  awayProb,
}: {
  homeProb: number;
  drawProb: number | null;
  awayProb: number;
}) {
  const home = Math.round(homeProb * 100);
  const draw = drawProb ? Math.round(drawProb * 100) : 0;
  const away = 100 - home - draw;

  return (
    <View>
      <View className="h-3 flex-row overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
        <View style={{ flex: home }} className="bg-blue-600" />
        {draw > 0 && <View style={{ flex: draw }} className="bg-gray-400" />}
        <View style={{ flex: away }} className="bg-orange-500" />
      </View>
      <View className="mt-1 flex-row justify-between">
        <Text className="text-xs text-gray-500 dark:text-gray-400">Home {home}%</Text>
        {draw > 0 && <Text className="text-xs text-gray-500 dark:text-gray-400">Draw {draw}%</Text>}
        <Text className="text-xs text-gray-500 dark:text-gray-400">Away {away}%</Text>
      </View>
    </View>
  );
}
