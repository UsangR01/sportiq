import Slider from "@react-native-community/slider";
import { useQuery } from "@tanstack/react-query";
import * as Haptics from "expo-haptics";
import { useMemo, useRef, useState } from "react";
import { Platform, RefreshControl, SectionList, Text, View } from "react-native";

import { FixtureCard } from "@/components/fixtures/FixtureCard";
import { GuestBanner } from "@/components/GuestBanner";
import { SportFilterChips } from "@/components/SportFilterChips";
import { listFixtures } from "@/lib/api/fixtures";
import { listSports } from "@/lib/api/sports";
import type { FixtureSummary } from "@/lib/api/types";
import { countryFlag } from "@/lib/countryFlags";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

const DEFAULT_MIN_PROBABILITY = 0.55;

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

export default function HomeScreen() {
  const isGuest = useAuthStore((s) => s.accessToken === null);
  const sportFilter = usePreferencesStore((s) => s.sportFilter);
  const setSportFilter = usePreferencesStore((s) => s.setSportFilter);
  const [refreshing, setRefreshing] = useState(false);
  // Local, not persisted — this only controls which matches get a highlighted prediction
  // badge vs. a plain odds line on this screen; the backend always returns every match's
  // best_pick regardless (see app/fixtures/router.py), so nothing needs to be re-fetched
  // when this changes.
  const [minProbability, setMinProbability] = useState(DEFAULT_MIN_PROBABILITY);
  const hasStartedSliding = useRef(false);

  const sportsQuery = useQuery({ queryKey: ["sports"], queryFn: listSports });
  const fixturesQuery = useQuery({
    queryKey: ["fixtures", "home", sportFilter],
    queryFn: () => listFixtures({ sport_slug: sportFilter ?? undefined, limit: 50 }),
  });

  const sections = useMemo(() => groupByLeague(fixturesQuery.data ?? []), [fixturesQuery.data]);

  async function onRefresh() {
    setRefreshing(true);
    await fixturesQuery.refetch();
    setRefreshing(false);
  }

  // See app/(tabs)/picks.tsx for why onSlidingComplete is gated behind a real onSlidingStart
  // (Android's SeekBar fires a spurious completion on mount otherwise).
  function onSlidingStart() {
    hasStartedSliding.current = true;
  }

  function onSlidingComplete(value: number) {
    if (!hasStartedSliding.current) return;
    hasStartedSliding.current = false;
    setMinProbability(Math.round(value * 100) / 100);
    if (Platform.OS !== "web") {
      Haptics.selectionAsync();
    }
  }

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <View className="px-4 pt-2">
        <Text className="text-sm text-gray-500 dark:text-gray-400">
          Highlight predictions above{" "}
          <Text className="font-semibold text-gray-900 dark:text-gray-100">
            {Math.round(minProbability * 100)}%
          </Text>
        </Text>
        <Slider
          minimumValue={0.34}
          maximumValue={0.9}
          step={0.01}
          value={minProbability}
          onSlidingStart={onSlidingStart}
          onSlidingComplete={onSlidingComplete}
          minimumTrackTintColor="#2563eb"
        />
      </View>
      <SportFilterChips
        sports={sportsQuery.data ?? []}
        selected={sportFilter}
        onSelect={setSportFilter}
      />
      {isGuest && <GuestBanner />}
      <SectionList
        sections={sections}
        keyExtractor={(item) => item.id}
        contentContainerClassName="px-4 pb-4"
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
        }
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
        renderItem={({ item }) => (
          <FixtureCard fixture={item} minProbability={minProbability} />
        )}
        ListEmptyComponent={
          fixturesQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading fixtures…</Text>
          ) : fixturesQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportIQ API. Pull to retry.
            </Text>
          ) : (
            <Text className="mt-8 text-center text-gray-400">
              No upcoming fixtures for this filter.
            </Text>
          )
        }
        stickySectionHeadersEnabled={false}
      />
    </View>
  );
}
