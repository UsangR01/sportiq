import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocalSearchParams } from "expo-router";
import { Pressable, ScrollView, Text, View } from "react-native";

import { LiveBadge } from "@/components/fixtures/LiveBadge";
import { getFixture } from "@/lib/api/fixtures";
import type { ComparisonStat, ExtraMarketsResponse, HeadToHeadResponse } from "@/lib/api/types";
import { addToWatchlist, listWatchlist, removeFromWatchlist } from "@/lib/api/watchlist";
import { useAuthStore } from "@/store/authStore";

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
      <Text className="mb-3 text-sm text-gray-500 dark:text-gray-400">
        {kickoff.toLocaleString()}
      </Text>

      <SaveControl fixtureId={fixture.id} />

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

      {fixture.match_stats.length > 0 && (
        <MatchStats
          stats={fixture.match_stats}
          homeTeam={fixture.home_team}
          awayTeam={fixture.away_team}
        />
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
          {/* The confidence tier is deliberately NOT shown. Measured 2026-08-10 on settled
              pre-match predictions: HIGH claimed 74.1% and delivered 60.9% (n=69) while MEDIUM
              claimed 57.8% and delivered 68.5% (n=89) — so the badge pointed users at the
              WEAKER set. The intervals overlap, so the inversion is not established, but the
              calibration gap is not in doubt and the thresholds were always documented as
              provisional guesses. A wrong signal is worse than no signal, so it is hidden until
              recalibrated against outcomes rather than assumed. The field is still stored and
              still returned by the API — it is measurement data, just not advice. */}
          <Text className="mt-2 text-xs text-gray-400">{fixture.prediction.model_version}</Text>
        </View>
      )}

      {fixture.prediction?.extra_markets && <ExtraMarkets markets={fixture.prediction.extra_markets} />}

      <RecentForm
        homeTeam={fixture.home_team}
        awayTeam={fixture.away_team}
        homeForm={fixture.home_team_form?.recent_form ?? null}
        awayForm={fixture.away_team_form?.recent_form ?? null}
      />

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

/** The two team names above a stat comparison, so a bare pair of numbers is attributable to a
 * side. Shared by the match-stats and head-to-head panels rather than duplicated, since both
 * render the same left/centre/right row shape underneath. */
function TeamNameHeader({ homeTeam, awayTeam }: { homeTeam: string; awayTeam: string }) {
  return (
    <View className="mb-1 flex-row">
      <Text
        className="flex-1 text-left text-xs font-semibold text-gray-500 dark:text-gray-400"
        numberOfLines={1}
      >
        {homeTeam}
      </Text>
      <View className="flex-1" />
      <Text
        className="flex-1 text-right text-xs font-semibold text-gray-500 dark:text-gray-400"
        numberOfLines={1}
      >
        {awayTeam}
      </Text>
    </View>
  );
}

/** What actually happened in the match that was just played, so the prediction below can be
 * read against the result instead of taken on trust. The card already shows a tick or a cross;
 * this is the evidence behind it — a corners pick that settled at 9 and one that settled at 14
 * both render as one green tick, and only one of them was close.
 *
 * Rendered only for a COMPLETED fixture, and only when the provider actually published
 * something:
 *
 *   football   goals, corners, total shots, shots on goal, possession   (API-Football)
 *   tennis     aces, double faults, serve and return percentages        (BallDontLie)
 *   NBA/WNBA   nothing — /stats is 401 on this plan, so the final score already shown above
 *              is the only real per-match number
 *
 * WHOLE NUMBERS, unlike the H2H panel directly below it. These are one match's actual counts,
 * not averages over several, so "Corners 9" is right and "Corners 9.0" invites the reader to
 * wonder what the decimal is hiding. */
function MatchStats({
  stats,
  homeTeam,
  awayTeam,
}: {
  stats: ComparisonStat[];
  homeTeam: string;
  awayTeam: string;
}) {
  return (
    <View className="mb-6">
      <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">Match Stats</Text>
      <TeamNameHeader homeTeam={homeTeam} awayTeam={awayTeam} />
      {stats.map((stat) => (
        <StatRow
          key={stat.label}
          label={stat.label}
          homeValue={stat.home}
          awayValue={stat.away}
          suffix={stat.suffix}
          decimals={0}
        />
      ))}
    </View>
  );
}

/** Real head-to-head history between the two teams — replaces the raw bookmaker-odds table
 * per direct user request ("Users don't find the Odds section useful... replaced with H2H
 * statistics"). Per a follow-up ask, shows average goals/corners/shots/shots-on-goal/
 * possession over the last meetings_count real meetings instead of a list of individual match
 * scores ("important stats that will give users confidence on the prediction"). Every value
 * is already relative to THIS fixture's home/away assignment, so no client-side flipping is
 * needed here.
 *
 * ALL THREE SPORTS, at whatever depth the provider allows — this said "football-only for now"
 * until 2026-08-14:
 *
 *   football   5 rows   API-Football
 *   tennis     6 rows   BallDontLie /head_to_head + /match_stats (serve and return)
 *   NBA/WNBA   1 row    BallDontLie /games — /stats is 401 on this plan, so the final score
 *                       is the only real per-meeting number, and "points allowed" would be
 *                       the exact mirror of "points scored" rather than a second fact
 *
 * Null (not a fabricated empty state) when the two have genuinely never met — common in early
 * tennis rounds, where the provider answers 404 rather than a zeroed record. */
/** Each side's own recent run, which head-to-head deliberately does not tell you.
 *
 * H2H answers "how do these two compare against each other", often over meetings months or
 * years apart. It says nothing about whether a side has won four straight or lost four
 * straight going into THIS match, which is the first thing anyone checks.
 *
 * A SEQUENCE, not a count. The model already carries win_streak, but "three in a row" cannot
 * distinguish WWWLL from WWWWW, and those are not the same team. Newest first, left to right.
 *
 * Costs no request anywhere: all three adapters already hold the completed matches they derive
 * form from, so this is data that was being computed and thrown away.
 *
 * Rendered for TEAMS and PLAYERS alike -- a tennis player is a Team row of one, so the same
 * component serves both without a sport branch.
 */
function RecentForm({
  homeTeam,
  awayTeam,
  homeForm,
  awayForm,
}: {
  homeTeam: string;
  awayTeam: string;
  homeForm: string | null;
  awayForm: string | null;
}) {
  // Nothing to say for either side: show nothing rather than two empty rows. An early-season
  // fixture legitimately has one result, and one chip is worth showing.
  if (!homeForm && !awayForm) return null;

  return (
    <View className="mb-6">
      <Text className="mb-2 text-sm font-semibold uppercase text-gray-400">Recent Form</Text>
      <View className="rounded-lg bg-gray-50 p-3 dark:bg-gray-900">
        <FormRow team={homeTeam} form={homeForm} />
        <FormRow team={awayTeam} form={awayForm} />
      </View>
      <Text className="mt-1 text-xs text-gray-400">Most recent first</Text>
    </View>
  );
}

function FormRow({ team, form }: { team: string; form: string | null }) {
  const results = (form ?? "").split("");
  return (
    <View className="flex-row items-center py-1.5">
      <Text
        numberOfLines={1}
        className="flex-1 pr-3 text-sm text-gray-700 dark:text-gray-200"
      >
        {team}
      </Text>
      {results.length === 0 ? (
        // Explicitly "no results", never an empty gap that reads as a rendering fault.
        <Text className="text-xs text-gray-400">No results yet</Text>
      ) : (
        <View className="flex-row gap-1">
          {results.map((result, index) => (
            <FormChip key={index} result={result} />
          ))}
        </View>
      )}
    </View>
  );
}

/** Letter AND colour, never colour alone -- the same accessibility rule the win/loss verdict
 * badges follow, and the reason they carry a tick or a cross rather than just going green. */
function FormChip({ result }: { result: string }) {
  const style =
    result === "W"
      ? "bg-green-600"
      : result === "L"
        ? "bg-red-500"
        : "bg-gray-400";
  return (
    <View className={`h-6 w-6 items-center justify-center rounded-full ${style}`}>
      <Text className="text-xs font-bold text-white">{result}</Text>
    </View>
  );
}

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
      <TeamNameHeader homeTeam={homeTeam} awayTeam={awayTeam} />

      {/* Whatever rows the backend sends, in its order. Football gets five, tennis six,
          basketball one -- each sport's provider exposes a different depth, and hardcoding
          football's five here is what stopped the other two having a panel at all. */}
      {headToHead.stats.map((stat) => (
        <StatRow
          key={stat.label}
          label={stat.label}
          homeValue={stat.home}
          awayValue={stat.away}
          suffix={stat.suffix}
          // Percentages read as whole numbers ("66%", not "66.0%"); counts keep one decimal
          // because they are averages over a handful of meetings and 6.0 vs 6.4 is the point.
          decimals={stat.suffix === "%" ? 0 : 1}
        />
      ))}
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


/** Save / remove, plus the honest reason to bother.
 *
 * Saving records the pick AS IT IS SHOWN RIGHT NOW, server-side. That matters because
 * best_pick is recomputed on every request and never stored — the feed can legitimately show
 * a different call tomorrow (odds land, features refresh, the model re-runs), which was
 * reported as the app changing its mind after the fact. Saving is the only way to keep what
 * you actually acted on.
 *
 * Auth-only, matching the endpoint: a guest session is device-bound Redis state with a 24h
 * TTL, while a watchlist is durable and drives a push notification.
 */
function SaveControl({ fixtureId }: { fixtureId: string }) {
  const accessToken = useAuthStore((state) => state.accessToken);
  const queryClient = useQueryClient();

  const watchlistQuery = useQuery({
    queryKey: ["watchlist"],
    queryFn: listWatchlist,
    enabled: !!accessToken,
  });

  const isSaved = (watchlistQuery.data ?? []).some((item) => item.fixture_id === fixtureId);

  const mutation = useMutation({
    mutationFn: () => (isSaved ? removeFromWatchlist(fixtureId) : addToWatchlist(fixtureId)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  if (!accessToken) {
    return (
      <Link href="/auth/login" asChild>
        <Pressable className="mb-6 self-start rounded-lg border border-slate-300 px-4 py-2 dark:border-slate-700">
          <Text className="text-sm text-slate-600 dark:text-slate-300">
            Sign in to save this pick
          </Text>
        </Pressable>
      </Link>
    );
  }

  return (
    <View className="mb-6">
      <Pressable
        onPress={() => mutation.mutate()}
        disabled={mutation.isPending}
        className={`self-start rounded-lg px-4 py-2 ${
          isSaved ? "border border-slate-300 dark:border-slate-700" : "bg-blue-600"
        }`}
      >
        <Text
          className={`text-sm font-semibold ${
            isSaved ? "text-slate-600 dark:text-slate-300" : "text-white"
          }`}
        >
          {mutation.isPending ? "…" : isSaved ? "Saved · tap to remove" : "Save this pick"}
        </Text>
      </Pressable>
      {!isSaved && (
        <Text className="mt-1 text-[11px] text-slate-400">
          Keeps the pick exactly as it is now, even if it changes later.
        </Text>
      )}
    </View>
  );
}
