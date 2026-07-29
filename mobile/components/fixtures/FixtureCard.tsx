import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import type { FixtureSummary } from "@/lib/api/types";
import { pickHeadline, selectionLabel } from "@/lib/pickFormat";
import { LiveBadge } from "./LiveBadge";

function actualResult(homeScore: number, awayScore: number): "home" | "draw" | "away" {
  if (homeScore > awayScore) return "home";
  if (homeScore < awayScore) return "away";
  return "draw";
}

function ScoreBadge({ fixture }: { fixture: FixtureSummary }) {
  const { live_state, status, best_pick } = fixture;
  if (!live_state) return null;

  const isCompleted = status === "completed";
  const actual = isCompleted ? actualResult(live_state.home_score, live_state.away_score) : null;
  // Non-h2h picks (double chance/totals) don't map onto a single "home"/"draw"/"away" actual
  // result the same way — correctness there isn't shown here (an h2h-shaped comparison would
  // be misleading for e.g. a "1X" pick), only for h2h best_picks.
  const wasCorrect =
    best_pick?.market === "h2h" && actual !== null && best_pick.selection === actual;

  return (
    <View className="items-center">
      <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
        {live_state.home_score} – {live_state.away_score}
      </Text>
      {status === "live" && live_state.match_minute != null && (
        <Text className="text-xs text-red-500">{live_state.match_minute}&apos;</Text>
      )}
      {/* Retrodicted prediction vs. the real result (see
          app/workers/backfill_predictions.py) — colour is never the only signal (a
          checkmark/cross is redundant with it) so this stays legible for colour-blind
          users too. Only rendered for h2h best_picks (see wasCorrect above); a non-h2h best
          pick still shows the badge without the correctness marker. */}
      {isCompleted && best_pick && (
        <View
          className={`mt-1 flex-row items-center rounded px-2 py-0.5 ${
            best_pick.market !== "h2h"
              ? "bg-gray-500"
              : wasCorrect
                ? "bg-green-600"
                : "bg-red-500"
          }`}
        >
          <Text className="text-[10px] font-bold text-white">
            {best_pick.market === "h2h" ? (wasCorrect ? "✓ " : "✗ ") : ""}
            {pickHeadline(best_pick)} {Math.round(best_pick.probability * 100)}%
          </Text>
        </View>
      )}
    </View>
  );
}

function PredictionBadge({ fixture }: { fixture: FixtureSummary }) {
  const pick = fixture.best_pick;
  if (!pick) return null;

  // Every fixture reaching this screen already cleared the Picks feed's own min-probability/
  // min-odds filters server-side (see app/(tabs)/index.tsx) — so this badge is always shown
  // highlighted, never demoted to a plain "Details" line the way it used to be.
  return (
    <View className="items-center rounded-lg bg-blue-600 px-3 py-2">
      <Text className="text-xs font-bold text-white">{pickHeadline(pick)}</Text>
      <Text className="text-xs text-blue-100">
        {Math.round(pick.probability * 100)}%{pick.odds ? ` · ${pick.odds.toFixed(2)}` : ""}
      </Text>
    </View>
  );
}

export function FixtureCard({ fixture }: { fixture: FixtureSummary }) {
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
          <PredictionBadge fixture={fixture} />
        )}
      </Pressable>
    </Link>
  );
}
