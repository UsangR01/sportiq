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
  // A match that ended without being played out (a tennis retirement/walkover — see
  // backend/app/adapters/balldontlie_tennis.py:_match_result_type). The result stands and
  // there IS a real winner, but we deliberately withhold the win/loss verdict: most
  // bookmakers void bets on a retirement, so a green tick would imply a payout the user may
  // never have actually received. Shown as a neutral "RET" marker instead of counting the
  // prediction as either a hit or a miss.
  const isRetired = isCompleted && live_state.result_type != null;
  // Covers every market, including corners_total when real corner counts were fetched at
  // settlement time (see lib/pickFormat.ts:evaluatePickCorrectness) — null only for a fixture
  // settled before that existed, or NBA. null means "genuinely can't verify this one", not
  // "wrong" — shown as a neutral grey badge, not red.
  const wasCorrect =
    isCompleted && best_pick && !isRetired
      ? evaluatePickCorrectness(
          best_pick,
          live_state.home_score,
          live_state.away_score,
          live_state.home_corners,
          live_state.away_corners,
        )
      : null;

  return (
    // No score here — each team's score is rendered on its OWN row by TeamRow, so which side
    // scored what reads straight off the alignment. This block is purely the verdict, plus
    // the two qualifiers that describe the score rather than the pick.
    <View className="items-center">
      {status === "live" && live_state.match_minute != null && (
        <Text className="mb-0.5 text-xs text-red-500">{live_state.match_minute}&apos;</Text>
      )}
      {isRetired && (
        <Text className="mb-0.5 text-[10px] font-semibold uppercase text-amber-600 dark:text-amber-500">
          {live_state.result_type === "walkover" ? "Walkover" : "Retired"}
        </Text>
      )}
      {/* Retrodicted prediction vs. the real result (see
          app/workers/backfill_predictions.py) — colour is never the only signal (a
          checkmark/cross is redundant with it) so this stays legible for colour-blind
          users too. Grey + no mark only for the rare case correctness can't be determined at
          all (a corners_total pick on a fixture settled before real corner counts existed).
          Odds shown alongside probability, same as the upcoming PredictionBadge below —
          omitted (never fabricated) when the pick was probability-only with no real price. */}
      {isCompleted && best_pick && (
        <View
          className={`flex-1 items-center justify-center rounded px-1 py-1 ${BADGE_WIDTH} ${
            isRetired || wasCorrect === null
              ? "bg-gray-500"
              : wasCorrect
                ? "bg-green-600"
                : "bg-red-500"
          }`}
        >
          <Text className="text-center text-[10px] font-bold text-white" numberOfLines={1}>
            {isRetired ? "VOID · " : wasCorrect === null ? "" : wasCorrect ? "✓ " : "✗ "}
            {pickHeadline(best_pick)}
          </Text>
          <Text className="text-center text-[10px] text-white" numberOfLines={1}>
            {Math.round(best_pick.probability * 100)}%
            {best_pick.odds ? ` · ${best_pick.odds.toFixed(2)}` : ""}
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

/** Below this fraction of real (non-null) model inputs, a prediction is flagged as
 * low-information rather than shown with the same authority as a well-informed one.
 *
 * Calibrated against the real measured spread across upcoming fixtures, not picked by feel:
 *
 *     EPL 0.12 | Scottish Prem 0.19 | Brasileirao 0.38 | MLS 0.48 | CSL 0.49
 *
 * EPL and the Scottish Premiership are between seasons, so their teams have no played matches
 * to derive form/attack/defence from and the model is effectively returning a base rate. The
 * in-season leagues sit around 0.4-0.5. A 0.5 cutoff would therefore flag literally every
 * fixture, which tells the user nothing; 0.35 separates "no real data yet" from "partial but
 * genuine data" — which is the distinction worth surfacing.
 *
 * Worth revisiting once the European seasons are underway and the spread shifts upward. */
const LOW_CONFIDENCE_COMPLETENESS = 0.35;

function PredictionBadge({ fixture }: { fixture: FixtureSummary }) {
  const pick = fixture.best_pick;
  if (!pick) return null;

  const lowInformation =
    pick.feature_completeness != null && pick.feature_completeness < LOW_CONFIDENCE_COMPLETENESS;

  // Every fixture reaching this screen already cleared the Picks feed's own min-probability/
  // min-odds filters server-side (see app/(tabs)/index.tsx) — so this badge is always shown
  // highlighted, never demoted to a plain "Details" line the way it used to be.
  return (
    <View className="items-center">
      <View
        className={`flex-1 items-center justify-center rounded-lg px-2 py-2 ${BADGE_WIDTH} ${
          lowInformation ? "bg-blue-400" : "bg-blue-600"
        }`}
      >
        <Text className="text-center text-xs font-bold text-white" numberOfLines={1}>
          {pickHeadline(pick)}
        </Text>
        <Text className="text-center text-xs text-blue-100" numberOfLines={1}>
          {Math.round(pick.probability * 100)}%{pick.odds ? ` · ${pick.odds.toFixed(2)}` : ""}
        </Text>
      </View>
      {/* Deliberately worded as a limitation of our DATA, not a hedge on the number: the
          probability is what the model genuinely says, it just had little to go on. */}
      {lowInformation && (
        <Text className="mt-0.5 text-center text-[9px] text-amber-600 dark:text-amber-500">
          limited data
        </Text>
      )}
    </View>
  );
}

/** One team line: name on the left, that team's own score right-aligned.
 *
 * The score column is a fixed width so both rows align regardless of digit count, and so the
 * numbers form a clean vertical pair rather than drifting with the team name length. */
function TeamRow({ name, score }: { name: string; score?: number | null }) {
  return (
    <View className="flex-row items-center justify-between">
      <Text
        className="flex-1 text-base font-semibold text-gray-900 dark:text-gray-100"
        numberOfLines={1}
      >
        {name}
      </Text>
      {score != null && (
        <Text className="w-6 text-right text-base font-bold text-gray-900 dark:text-gray-100">
          {score}
        </Text>
      )}
    </View>
  );
}

export function FixtureCard({ fixture }: { fixture: FixtureSummary }) {
  const kickoff = new Date(fixture.kickoff_utc);
  const isLive = fixture.status === "live";
  const isCompleted = fixture.status === "completed";
  const isPostponed = fixture.status === "postponed";
  // Only show scores once there is something real to show — a scheduled fixture has none.
  const score =
    (isLive || isCompleted) && fixture.live_state
      ? { home: fixture.live_state.home_score, away: fixture.live_state.away_score }
      : null;

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
              ) : fixture.kickoff_is_estimated ? (
                /* The provider gave no real start time, so kickoff_utc is a DATE placeholder,
                   not a time. Showing it as "01:00" was doubly misleading: wrong by hours, and
                   because every untimed match in a tournament inherits the same placeholder,
                   later-round matches appeared on today's schedule and couldn't be found on any
                   real platform. Say what we actually know instead. */
                <Text className="text-xs text-gray-400">
                  {kickoff.toLocaleDateString()} · <Text className="text-amber-600">Time TBC</Text>
                </Text>
              ) : (
                <Text className="text-xs text-gray-400">
                  {kickoff.toLocaleDateString()}{" "}
                  {kickoff.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </Text>
              )}
            </View>
            {/* Each score sits on its OWN team's row, home above away, rather than as a
                combined "1 – 1" elsewhere on the card — so which side scored what is read
                straight off the alignment instead of inferred from left-to-right order.
                The badge is a sibling of THIS block rather than of the whole card, and
                stretches to it: that is what makes its top and bottom edges line up with the
                two team rows instead of floating centred against a taller container.

                items-stretch here is what sizes the badge — the badges must NOT also set
                h-full. height:100% against an auto-height parent, with flex-1 inside it, grows
                without bound on Yoga: on a real device one card filled the entire screen and
                only a single fixture was reachable. It collapsed harmlessly on web, which is
                why the browser pass missed it entirely. */}
            <View className="flex-row items-stretch">
              <View className="flex-1">
                <TeamRow name={fixture.home_team} score={score?.home} />
                <TeamRow name={fixture.away_team} score={score?.away} />
              </View>
              <View className="ml-3 justify-center">
                {isPostponed ? (
                  <PostponedBadge />
                ) : isLive || isCompleted ? (
                  <ScoreBadge fixture={fixture} />
                ) : (
                  <PredictionBadge fixture={fixture} />
                )}
              </View>
            </View>
          </View>
        </View>
      </Pressable>
    </Link>
  );
}
