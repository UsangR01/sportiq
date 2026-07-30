import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import type { FixtureSummary } from "@/lib/api/types";
import { buildMarketBreakdown, evaluatePickCorrectness, pickHeadline, selectionLabel } from "@/lib/pickFormat";
import { LiveBadge } from "./LiveBadge";

function ScoreBadge({ fixture }: { fixture: FixtureSummary }) {
  const { live_state, status, best_pick } = fixture;
  if (!live_state) return null;

  const isCompleted = status === "completed";
  // Covers h2h/double-chance/goals-total (all derivable from home/away goals); corners_total
  // stays null (no corner count is tracked on FixtureLiveState) — see
  // lib/pickFormat.ts:evaluatePickCorrectness for the full per-market breakdown. null means
  // "genuinely can't verify this one", not "wrong" — shown as a neutral grey badge, not red.
  const wasCorrect =
    isCompleted && best_pick
      ? evaluatePickCorrectness(best_pick, live_state.home_score, live_state.away_score)
      : null;

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
          users too. Grey + no mark for the rare case correctness can't be determined at all
          (a corners_total best pick — see evaluatePickCorrectness). */}
      {isCompleted && best_pick && (
        <View
          className={`mt-1 flex-row items-center rounded px-2 py-0.5 ${
            wasCorrect === null ? "bg-gray-500" : wasCorrect ? "bg-green-600" : "bg-red-500"
          }`}
        >
          <Text className="text-[10px] font-bold text-white">
            {wasCorrect === null ? "" : wasCorrect ? "✓ " : "✗ "}
            {pickHeadline(best_pick)} {Math.round(best_pick.probability * 100)}%
          </Text>
        </View>
      )}
    </View>
  );
}

/** Full past-performance breakdown for a completed fixture — every market the model called
 * (h2h, double chance, goals/corners O/U), each with its own win/loss verdict, not just
 * best_pick's single winner. Per explicit user request: "I need all markets predicted in the
 * past to still be shown to evaluate performance... Everything should be shown." */
function MarketBreakdown({ fixture }: { fixture: FixtureSummary }) {
  const { live_state, all_market_picks } = fixture;
  if (!live_state || all_market_picks.length === 0) return null;

  const items = buildMarketBreakdown(
    all_market_picks,
    live_state.home_score,
    live_state.away_score,
  );
  if (items.length === 0) return null;

  return (
    <View className="mt-3 flex-row flex-wrap gap-1.5 border-t border-gray-100 pt-2 dark:border-gray-800">
      {items.map((item) => (
        <View
          key={item.key}
          className={`rounded px-2 py-1 ${
            item.correct === null
              ? "bg-gray-100 dark:bg-gray-800"
              : item.correct
                ? "bg-green-100 dark:bg-green-900"
                : "bg-red-100 dark:bg-red-900"
          }`}
        >
          <Text
            className={`text-[10px] font-medium ${
              item.correct === null
                ? "text-gray-500 dark:text-gray-400"
                : item.correct
                  ? "text-green-800 dark:text-green-200"
                  : "text-red-800 dark:text-red-200"
            }`}
          >
            {item.correct === null ? "" : item.correct ? "✓ " : "✗ "}
            {item.label}: {selectionLabel(item.selection)} {Math.round(item.probability * 100)}%
          </Text>
        </View>
      ))}
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
      <Pressable className="mb-2 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
        <View className="flex-row items-center justify-between">
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
        </View>

        {isCompleted && <MarketBreakdown fixture={fixture} />}
      </Pressable>
    </Link>
  );
}
