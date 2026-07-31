import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { ScrollView, Text, View } from "react-native";

import { LiveBadge } from "@/components/fixtures/LiveBadge";
import { getFixture } from "@/lib/api/fixtures";
import type { ExtraMarketsResponse, HeadToHeadResponse } from "@/lib/api/types";

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

      {fixture.prediction?.extra_markets && <ExtraMarkets markets={fixture.prediction.extra_markets} />}

      {fixture.head_to_head && (
        <HeadToHead
          headToHead={fixture.head_to_head}
          homeTeam={fixture.home_team}
          awayTeam={fixture.away_team}
        />
      )}

      {/* Highlight clips (TDD §5.3 Option A) and the animated match tracker (Option B,
          Phase 2) aren't wired up yet — Highlightly isn't integrated at the backend ingest
          layer, and there's no live WebSocket feed for the tracker to consume. */}
    </ScrollView>
  );
}

function pct(value: number | null): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function ExtraMarkets({ markets }: { markets: ExtraMarketsResponse }) {
  const hasDoubleChance =
    markets.double_chance_home_or_draw_prob != null || markets.double_chance_away_or_draw_prob != null;
  const hasGoals = markets.goals_totals.some((t) => t.over_prob != null);
  const hasCorners = markets.corners_totals.some((t) => t.over_prob != null);

  if (!hasDoubleChance && !hasGoals && !hasCorners) return null;

  return (
    <View className="mb-6">
      <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">Other Markets</Text>

      {hasDoubleChance && (
        <View className="mb-3 flex-row justify-between border-b border-gray-100 py-2 dark:border-gray-800">
          <Text className="text-gray-700 dark:text-gray-300">Double Chance</Text>
          <Text className="text-gray-900 dark:text-gray-100">
            1X {pct(markets.double_chance_home_or_draw_prob)} · X2{" "}
            {pct(markets.double_chance_away_or_draw_prob)}
          </Text>
        </View>
      )}

      {hasGoals && (
        <View className="mb-1">
          <Text className="mb-1 text-gray-700 dark:text-gray-300">Goals Over/Under</Text>
          {markets.goals_totals.map((t) => (
            <View key={t.line} className="flex-row justify-between py-1">
              <Text className="text-gray-500 dark:text-gray-400">{t.line}</Text>
              <Text className="text-gray-900 dark:text-gray-100">
                Over {pct(t.over_prob)} · Under {pct(t.under_prob)}
              </Text>
            </View>
          ))}
        </View>
      )}

      {hasCorners && (
        <View className="mt-2">
          <Text className="mb-1 text-gray-700 dark:text-gray-300">Corners Over/Under</Text>
          {markets.corners_totals.map((t) => (
            <View key={t.line} className="flex-row justify-between py-1">
              <Text className="text-gray-500 dark:text-gray-400">{t.line}</Text>
              <Text className="text-gray-900 dark:text-gray-100">
                Over {pct(t.over_prob)} · Under {pct(t.under_prob)}
              </Text>
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

function formatStat(value: number | null, decimals: number, suffix: string): string {
  return value == null ? "—" : `${value.toFixed(decimals)}${suffix}`;
}

/** One stat's home-vs-away comparison — home value left-aligned, label centered, away value
 * right-aligned, mirroring how sports apps typically lay out a head-to-head stat comparison.
 * Renders nothing (not even the label) when BOTH sides are null, e.g. a stat type
 * /fixtures/statistics genuinely never returned for any of the counted meetings — never a
 * fabricated "—" / "—" row for a stat that's entirely unavailable. */
function StatRow({
  label,
  homeValue,
  awayValue,
  suffix = "",
  decimals = 1,
}: {
  label: string;
  homeValue: number | null;
  awayValue: number | null;
  suffix?: string;
  decimals?: number;
}) {
  if (homeValue == null && awayValue == null) return null;
  return (
    <View className="flex-row items-center border-b border-gray-100 py-2 dark:border-gray-800">
      <Text className="flex-1 text-left text-sm font-semibold text-gray-900 dark:text-gray-100">
        {formatStat(homeValue, decimals, suffix)}
      </Text>
      <Text className="flex-1 text-center text-xs uppercase text-gray-400">{label}</Text>
      <Text className="flex-1 text-right text-sm font-semibold text-gray-900 dark:text-gray-100">
        {formatStat(awayValue, decimals, suffix)}
      </Text>
    </View>
  );
}

/** Real head-to-head history between the two teams — replaces the raw bookmaker-odds table
 * per direct user request ("Users don't find the Odds section useful... replaced with H2H
 * statistics"). Per a follow-up ask, shows average goals/corners/shots/shots-on-goal/
 * possession over the last meetings_count real meetings instead of a list of individual match
 * scores ("important stats that will give users confidence on the prediction"). Every value
 * is already relative to THIS fixture's home/away assignment (see backend/app/adapters/
 * api_football.py:H2HDetail), so no client-side flipping is needed here. Only rendered when
 * the backend actually has real history — football-only for now, null (not a fabricated
 * empty state) for NBA or two teams with no shared past meetings. */
function HeadToHead({
  headToHead,
  homeTeam,
  awayTeam,
}: {
  headToHead: HeadToHeadResponse;
  homeTeam: string;
  awayTeam: string;
}) {
  return (
    <View className="mb-6">
      <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">Head to Head</Text>

      <View className="mb-3 flex-row rounded-lg bg-gray-50 p-3 dark:bg-gray-900">
        <View className="flex-1 items-center">
          <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
            {headToHead.home_wins}
          </Text>
          <Text className="text-center text-xs text-gray-500 dark:text-gray-400">
            {homeTeam} wins
          </Text>
        </View>
        <View className="flex-1 items-center">
          <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
            {headToHead.draws}
          </Text>
          <Text className="text-xs text-gray-500 dark:text-gray-400">Draws</Text>
        </View>
        <View className="flex-1 items-center">
          <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
            {headToHead.away_wins}
          </Text>
          <Text className="text-center text-xs text-gray-500 dark:text-gray-400">
            {awayTeam} wins
          </Text>
        </View>
      </View>

      <Text className="mb-2 text-xs text-gray-400">
        Averages over last {headToHead.meetings_count} meeting
        {headToHead.meetings_count === 1 ? "" : "s"}
      </Text>
      <View className="mb-1 flex-row">
        <Text className="flex-1 text-left text-xs font-semibold text-gray-500 dark:text-gray-400" numberOfLines={1}>
          {homeTeam}
        </Text>
        <View className="flex-1" />
        <Text className="flex-1 text-right text-xs font-semibold text-gray-500 dark:text-gray-400" numberOfLines={1}>
          {awayTeam}
        </Text>
      </View>

      <StatRow label="Goals" homeValue={headToHead.avg_goals_home} awayValue={headToHead.avg_goals_away} />
      <StatRow
        label="Corners"
        homeValue={headToHead.avg_corners_home}
        awayValue={headToHead.avg_corners_away}
      />
      <StatRow
        label="Total Shots"
        homeValue={headToHead.avg_shots_home}
        awayValue={headToHead.avg_shots_away}
      />
      <StatRow
        label="Shots on Goal"
        homeValue={headToHead.avg_shots_on_goal_home}
        awayValue={headToHead.avg_shots_on_goal_away}
      />
      <StatRow
        label="Possession"
        homeValue={headToHead.avg_possession_home}
        awayValue={headToHead.avg_possession_away}
        suffix="%"
        decimals={0}
      />
    </View>
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
