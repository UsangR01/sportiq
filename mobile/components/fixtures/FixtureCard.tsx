import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import type { FixtureSummary } from "@/lib/api/types";
import { evaluatePickCorrectness, pickHeadline } from "@/lib/pickFormat";
import { LiveBadge } from "./LiveBadge";

// One fixed width for every pick badge (verdict pill and upcoming-pick box alike) so a card
// showing "1X 80%" lines up exactly with one showing "UNDER 9.5 100%" — the frame never
// reflows around the market/text length.
const BADGE_WIDTH = "w-[104px]";

function ScoreBadge({ fixture }: { fixture: FixtureSummary }) {
  const { live_state, status, best_pick } = fixture;
  if (!live_state) return null;

  const isCompleted = status === "completed";
  // Covers every market, including corners_total when real corner counts were fetched at
  // settlement time (see lib/pickFormat.ts:evaluatePickCorrectness) — null only for a fixture
  // settled before that existed, or NBA. null means "genuinely can't verify this one", not
  // "wrong" — shown as a neutral grey badge, not red.
  const wasCorrect =
    isCompleted && best_pick
      ? evaluatePickCorrectness(
          best_pick,
          live_state.home_score,
          live_state.away_score,
          live_state.home_corners,
          live_state.away_corners,
        )
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
          users too. Grey + no mark only for the rare case correctness can't be determined at
          all (a corners_total pick on a fixture settled before real corner counts existed). */}
      {isCompleted && best_pick && (
        <View
          className={`mt-1 items-center justify-center rounded px-1 py-1 ${BADGE_WIDTH} ${
            wasCorrect === null ? "bg-gray-500" : wasCorrect ? "bg-green-600" : "bg-red-500"
          }`}
        >
          <Text className="text-center text-[10px] font-bold text-white" numberOfLines={1}>
            {wasCorrect === null ? "" : wasCorrect ? "✓ " : "✗ "}
            {pickHeadline(best_pick)} {Math.round(best_pick.probability * 100)}%
          </Text>
        </View>
      )}
    </View>
  );
}

/** Shown in place of a score or a market prediction/odds badge for a fixture that isn't
 * actually being played as originally scheduled (postponed/cancelled/abandoned/... — see
 * backend/app/fixtures/models.py:FixtureStatus.POSTPONED). The backend already nulls out
 * best_pick/all_market_picks/prediction for these, so there's nothing stale to accidentally
 * render here — this is the ONLY thing shown, never a leftover pre-postponement pick. */
function PostponedBadge() {
  return (
    <View
      className={`items-center justify-center rounded-lg bg-gray-200 px-2 py-2 dark:bg-gray-700 ${BADGE_WIDTH}`}
    >
      <Text
        className="text-center text-xs font-bold text-gray-600 dark:text-gray-300"
        numberOfLines={1}
      >
        POSTPONED
      </Text>
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
    <View className={`items-center justify-center rounded-lg bg-blue-600 px-2 py-2 ${BADGE_WIDTH}`}>
      <Text className="text-center text-xs font-bold text-white" numberOfLines={1}>
        {pickHeadline(pick)}
      </Text>
      <Text className="text-center text-xs text-blue-100" numberOfLines={1}>
        {Math.round(pick.probability * 100)}%{pick.odds ? ` · ${pick.odds.toFixed(2)}` : ""}
      </Text>
    </View>
  );
}

export function FixtureCard({ fixture }: { fixture: FixtureSummary }) {
  const kickoff = new Date(fixture.kickoff_utc);
  const isLive = fixture.status === "live";
  const isCompleted = fixture.status === "completed";
  const isPostponed = fixture.status === "postponed";

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
              ) : isPostponed ? (
                <Text className="text-xs font-semibold uppercase text-amber-600 dark:text-amber-500">
                  Postponed
                </Text>
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

          {isPostponed ? (
            <PostponedBadge />
          ) : isLive || isCompleted ? (
            <ScoreBadge fixture={fixture} />
          ) : (
            <PredictionBadge fixture={fixture} />
          )}
        </View>
      </Pressable>
    </Link>
  );
}
