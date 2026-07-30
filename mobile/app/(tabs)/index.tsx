import Slider from "@react-native-community/slider";
import { useQuery } from "@tanstack/react-query";
import * as Haptics from "expo-haptics";
import { useMemo, useRef, useState } from "react";
import { Platform, RefreshControl, SectionList, Text, View } from "react-native";

import { DayStrip, type DaySelection } from "@/components/DayStrip";
import { FixtureCard } from "@/components/fixtures/FixtureCard";
import { GuestBanner } from "@/components/GuestBanner";
import { SportFilterChips } from "@/components/SportFilterChips";
import { listFixtures } from "@/lib/api/fixtures";
import { listSports } from "@/lib/api/sports";
import type { FixtureSummary } from "@/lib/api/types";
import { countryFlag } from "@/lib/countryFlags";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

// The Picks feed only ever surfaces "the best odds with the highest probability of winning" —
// per the user's own words, NOT a general schedule browser. 60% is the floor they set
// explicitly (previously 34%, which was only ever a client-side highlight threshold, not a
// real filter — this is now a real server-side filter, see app/fixtures/router.py's
// min_probability param).
const MIN_PROBABILITY_FLOOR = 0.6;
const DEFAULT_MIN_PROBABILITY = 0.6;
// A full day across every league can easily exceed the old flat-list default of 50 — 200 is
// the backend's own ceiling (app/fixtures/router.py's `limit` Query(..., le=200)).
const FIXTURES_PAGE_LIMIT = 200;

function dayBounds(date: Date): { from: string; to: string } {
  const from = new Date(date);
  from.setHours(0, 0, 0, 0);
  const to = new Date(date);
  to.setHours(23, 59, 59, 999);
  return { from: from.toISOString(), to: to.toISOString() };
}

interface LeagueSection {
  title: string;
  slug: string;
  country: string | null;
  data: FixtureSummary[];
}

function groupByLeague(fixtures: FixtureSummary[]): LeagueSection[] {
  const bySlug = new Map<string, LeagueSection>();
  for (const fixture of fixtures) {
    let section = bySlug.get(fixture.league_slug);
    if (!section) {
      section = {
        title: fixture.league_name,
        slug: fixture.league_slug,
        country: fixture.league_country,
        data: [],
      };
      bySlug.set(fixture.league_slug, section);
    }
    section.data.push(fixture);
  }

  const sections = Array.from(bySlug.values());
  for (const section of sections) {
    // Live-first, then soonest kickoff — same ordering the old flat list used.
    section.data.sort((a, b) => {
      const aLive = a.status === "live";
      const bLive = b.status === "live";
      if (aLive !== bLive) return aLive ? -1 : 1;
      return new Date(a.kickoff_utc).getTime() - new Date(b.kickoff_utc).getTime();
    });
  }
  // A league with any live match sorts to the top; otherwise by its earliest kickoff.
  sections.sort((a, b) => {
    const aLive = a.data.some((f) => f.status === "live");
    const bLive = b.data.some((f) => f.status === "live");
    if (aLive !== bLive) return aLive ? -1 : 1;
    const aEarliest = Math.min(...a.data.map((f) => new Date(f.kickoff_utc).getTime()));
    const bEarliest = Math.min(...b.data.map((f) => new Date(f.kickoff_utc).getTime()));
    return aEarliest - bEarliest;
  });
  return sections;
}

export default function PicksScreen() {
  const isGuest = useAuthStore((s) => s.accessToken === null);
  const sportFilter = usePreferencesStore((s) => s.sportFilter);
  const setSportFilter = usePreferencesStore((s) => s.setSportFilter);
  const minOdds = usePreferencesStore((s) => s.minOdds);
  const setMinOdds = usePreferencesStore((s) => s.setMinOdds);
  const [refreshing, setRefreshing] = useState(false);

  const [minProbability, setMinProbability] = useState(DEFAULT_MIN_PROBABILITY);

  const hasStartedSlidingProb = useRef(false);
  const hasStartedSlidingOdds = useRef(false);
  // Defaults to "today" — the backend only ever backfills/looks ahead 7 days either way
  // (see components/DayStrip.tsx), so this always starts inside real, populated data.
  const [daySelection, setDaySelection] = useState<DaySelection>(() => ({ date: new Date() }));

  const sportsQuery = useQuery({ queryKey: ["sports"], queryFn: listSports });
  const dayKey = daySelection === "live" ? "live" : daySelection.date.toDateString();
  const fixturesQuery = useQuery({
    queryKey: ["fixtures", "picks", sportFilter, dayKey, minProbability, minOdds],
    queryFn: () => {
      // market/line are deliberately omitted — the backend's default (combined best pick
      // across every market: h2h, double chance, goals/corners O/U) is always what this
      // screen wants now; per-market filtering was removed as UI clutter nobody needed.
      const marketParams = { min_probability: minProbability, min_odds: minOdds };
      if (daySelection === "live") {
        return listFixtures({
          sport_slug: sportFilter ?? undefined,
          status: "live",
          limit: FIXTURES_PAGE_LIMIT,
          ...marketParams,
        });
      }
      const { from, to } = dayBounds(daySelection.date);
      return listFixtures({
        sport_slug: sportFilter ?? undefined,
        date_from: from,
        date_to: to,
        limit: FIXTURES_PAGE_LIMIT,
        ...marketParams,
      });
    },
  });

  const sections = useMemo(() => groupByLeague(fixturesQuery.data ?? []), [fixturesQuery.data]);

  async function onRefresh() {
    setRefreshing(true);
    await fixturesQuery.refetch();
    setRefreshing(false);
  }

  // @react-native-community/slider's Android SeekBar backing view fires onSlidingComplete
  // once on mount with no real touch involved (confirmed live in an earlier phase of this
  // project — it reported minimumValue). Only commit a completion preceded by a real
  // onSlidingStart, same guard both sliders below need.
  function onProbSlidingStart() {
    hasStartedSlidingProb.current = true;
  }
  function onProbSlidingComplete(value: number) {
    if (!hasStartedSlidingProb.current) return;
    hasStartedSlidingProb.current = false;
    setMinProbability(Math.round(value * 100) / 100);
    if (Platform.OS !== "web") Haptics.selectionAsync();
  }

  function onOddsSlidingStart() {
    hasStartedSlidingOdds.current = true;
  }
  function onOddsSlidingComplete(value: number) {
    if (!hasStartedSlidingOdds.current) return;
    hasStartedSlidingOdds.current = false;
    setMinOdds(Math.round(value * 100) / 100);
    if (Platform.OS !== "web") Haptics.selectionAsync();
  }

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <View className="px-4 pt-2">
        <Text className="text-sm text-gray-500 dark:text-gray-400">
          Minimum probability:{" "}
          <Text className="font-semibold text-gray-900 dark:text-gray-100">
            {Math.round(minProbability * 100)}%
          </Text>
        </Text>
        <Slider
          minimumValue={MIN_PROBABILITY_FLOOR}
          maximumValue={0.95}
          step={0.01}
          value={minProbability}
          onSlidingStart={onProbSlidingStart}
          onSlidingComplete={onProbSlidingComplete}
          minimumTrackTintColor="#2563eb"
        />
        <Text className="text-sm text-gray-500 dark:text-gray-400">
          Minimum odds: <Text className="font-semibold text-gray-900 dark:text-gray-100">{minOdds.toFixed(2)}</Text>
        </Text>
        <Slider
          minimumValue={1.01}
          maximumValue={20}
          step={0.01}
          value={minOdds}
          onSlidingStart={onOddsSlidingStart}
          onSlidingComplete={onOddsSlidingComplete}
          minimumTrackTintColor="#2563eb"
        />
      </View>
      <SportFilterChips
        sports={sportsQuery.data ?? []}
        selected={sportFilter}
        onSelect={setSportFilter}
      />
      <DayStrip selected={daySelection} onSelect={setDaySelection} />
      {isGuest && <GuestBanner />}
      <SectionList
        sections={sections}
        keyExtractor={(item) => item.id}
        contentContainerClassName="px-4 pb-4"
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        renderSectionHeader={({ section }) => (
          <View className="mb-2 mt-3 flex-row items-center bg-white dark:bg-black">
            <Text className="mr-2 text-base">{countryFlag(section.country)}</Text>
            <View>
              <Text className="text-sm font-bold text-gray-900 dark:text-gray-100">
                {section.title}
              </Text>
              {section.country && (
                <Text className="text-xs text-gray-400">{section.country}</Text>
              )}
            </View>
          </View>
        )}
        renderItem={({ item }) => <FixtureCard fixture={item} />}
        ListEmptyComponent={
          fixturesQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading picks…</Text>
          ) : fixturesQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportIQ API. Pull to retry.
            </Text>
          ) : (
            <Text className="mt-8 text-center text-gray-400">
              No picks clear this probability/odds threshold for this day.
            </Text>
          )
        }
        stickySectionHeadersEnabled={false}
      />
    </View>
  );
}
