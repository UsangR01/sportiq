import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import type { FixtureSummary } from "@/lib/api/types";
import { LiveBadge } from "./LiveBadge";

const SELECTION_LABEL: Record<"home" | "draw" | "away", string> = {
  home: "HOME",
  draw: "DRAW",
  away: "AWAY",
};

// Matches app/(tabs)/index.tsx's own default — callers that don't care about the highlight
// threshold (e.g. the Live tab's plain list) can omit the prop entirely.
const DEFAULT_MIN_PROBABILITY = 0.55;

function ScoreBadge({ fixture }: { fixture: FixtureSummary }) {
  const { live_state, status } = fixture;
  if (!live_state) return null;
  return (
    <View className="items-center">
      <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
        {live_state.home_score} – {live_state.away_score}
      </Text>
      {status === "live" && live_state.match_minute != null && (
        <Text className="text-xs text-red-500">{live_state.match_minute}&apos;</Text>
      )}
    </View>
  );
}

function PredictionBadge({
  fixture,
  minProbability,
}: {
  fixture: FixtureSummary;
  minProbability: number;
}) {
  const pick = fixture.best_pick;
  if (!pick) return null;
  const isHighlighted = pick.probability >= minProbability;

  if (isHighlighted) {
    return (
      <View className="items-center rounded-lg bg-blue-600 px-3 py-2">
        <Text className="text-xs font-bold text-white">{SELECTION_LABEL[pick.selection]}</Text>
        <Text className="text-xs text-blue-100">
          {Math.round(pick.probability * 100)}%{pick.odds ? ` · ${pick.odds.toFixed(2)}` : ""}
        </Text>
      </View>
    );
  }
  return (
    <View className="items-end">
      <Text className="text-xs uppercase text-gray-400">Details</Text>
      <Text className="text-sm text-gray-600 dark:text-gray-400">
        {pick.odds ? pick.odds.toFixed(2) : "—"}
      </Text>
    </View>
  );
}

export function FixtureCard({
  fixture,
  minProbability = DEFAULT_MIN_PROBABILITY,
}: {
  fixture: FixtureSummary;
  /** Below this, best_pick still renders but as a plain odds line (not a highlighted
   * prediction badge) — matches the reference app's "DETAILS/OPEN" treatment for matches
   * the model doesn't have a strong-enough read on to call out. */
  minProbability?: number;
}) {
  const kickoff = new Date(fixture.kickoff_utc);
  const isLive = fixture.status === "live";
  const isCompleted = fixture.status === "completed";

  return (
    <Link href={`/fixture/${fixture.id}`} asChild>
      <Pressable className="mb-2 flex-row items-center justify-between rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
        <View className="mr-3 flex-1">
          <View className="mb-1 flex-row items-center">
            {isLive ? (
              <LiveBadge />
            ) : isCompleted ? (
              <Text className="text-xs font-semibold uppercase text-gray-400">Full-time</Text>
            ) : (
              <Text className="text-xs text-gray-400">
                {kickoff.toLocaleDateString()}{" "}
                {kickoff.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </Text>
            )}
          </View>
          <Text className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {fixture.home_team}
          </Text>
          <Text className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {fixture.away_team}
          </Text>
        </View>

        {isLive || isCompleted ? (
          <ScoreBadge fixture={fixture} />
        ) : (
          <PredictionBadge fixture={fixture} minProbability={minProbability} />
        )}
      </Pressable>
    </Link>
  );
}
