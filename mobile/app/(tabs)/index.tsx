import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { RefreshControl, SectionList, Text, View } from "react-native";

import { AppHeader } from "@/components/AppHeader";
import { DateNavigator, startOfDay } from "@/components/DateNavigator";
import { FiltersPanel } from "@/components/FiltersPanel";
import { FixtureCard } from "@/components/fixtures/FixtureCard";
import { GuestBanner } from "@/components/GuestBanner";
import { SportDropdown } from "@/components/SportDropdown";
import { StatusDropdown, type StatusFilter } from "@/components/StatusDropdown";
import { listFixtures } from "@/lib/api/fixtures";
import { listSports } from "@/lib/api/sports";
import type { FixtureSummary } from "@/lib/api/types";
import { CountryFlag, countryForTournamentLocation } from "@/lib/countryFlags";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

// The Picks feed only ever surfaces "the best odds with the highest probability of winning" —
// per the user's own words, NOT a general schedule browser. 60% is the floor they set
// explicitly (previously 34%, which was only ever a client-side highlight threshold, not a
// real filter — this is now a real server-side filter, see app/fixtures/router.py's
// min_probability param).
// Slider bottom lowered from 0.6 to 0.5. A 60% minimum silently excluded the ENTIRE 1X2
// market: measured across every stored prediction, no home/draw/away probability ever reached
// 0.60 (the highest away probability observed was 0.588). So the feed could only ever show
// Over/Under and double chance, which is why it looked so monotonous. 0.5 lets a genuinely
// strong 1X2 call — a 57% home pick sits well above football's real 45.8% home base rate —
// actually reach the user, while the default stays at 0.6 so nothing changes unless they ask.
const MIN_PROBABILITY_FLOOR = 0.5;
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
  /** Court surface (Hard/Clay/Grass) — tennis only, shown alongside the tournament name. */
  surface: string | null;
  /** Every fixture here has only an estimated kickoff, so the day it appears under is a
   * fallback rather than a scheduled time. */
  timeUnconfirmed: boolean;
  data: FixtureSummary[];
}

/** Groups a day's fixtures into the sections the feed renders.
 *
 * Football/NBA group by LEAGUE, but tennis groups by TOURNAMENT: a whole tour is a single
 * league row ("ATP Tour"), so grouping by it would render one undifferentiated wall of
 * matches. Per explicit user request, a tennis section is one tournament, headed by its name,
 * host-country flag and surface — the details a user needs to find the right event in their
 * betting app. Surface matters enough in tennis to belong in the header rather than buried in
 * fixture detail: it materially changes who's favoured.
 *
 * A tennis fixture with no tournament data (ingested before those columns existed) falls back
 * to its league grouping rather than being dropped or grouped under a fabricated name. */
function groupByLeague(fixtures: FixtureSummary[]): LeagueSection[] {
  const bySlug = new Map<string, LeagueSection>();
  for (const fixture of fixtures) {
    const groupByTournament = fixture.tournament_name != null;
    // A fixture whose kickoff is only an estimate gets its own section rather than sitting
    // among matches with real times. Tennis schedules "after match 3 on Court 2", so the
    // provider leaves the time null and we fall back to the tournament's own date — which
    // lands it at midnight on a day nobody promised. Listing that alongside genuine times
    // presents a guess as fact, and it is why matches appeared under the wrong day at all.
    const key = groupByTournament
      ? `tournament:${fixture.tournament_name}${fixture.kickoff_is_estimated ? ":tbc" : ""}`
      : `league:${fixture.league_slug}${fixture.kickoff_is_estimated ? ":tbc" : ""}`;
    let section = bySlug.get(key);
    if (!section) {
      section = {
        title: groupByTournament ? fixture.tournament_name! : fixture.league_name,
        slug: key,
        country: groupByTournament
          ? countryForTournamentLocation(fixture.tournament_location)
          : fixture.league_country,
        surface: groupByTournament ? fixture.tournament_surface : null,
        timeUnconfirmed: fixture.kickoff_is_estimated,
        data: [],
      };
      bySlug.set(key, section);
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
  // Unconfirmed-time sections always sink below the scheduled ones: their kickoff is a
  // fallback, so ordering them by it would interleave guesses among real times.
  sections.sort((a, b) => {
    if (a.timeUnconfirmed !== b.timeUnconfirmed) return a.timeUnconfirmed ? 1 : -1;
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

  // Narrows within a sport: NBA vs WNBA, ATP vs WTA. Component state rather than the
  // preferences store because, unlike sportFilter, it is NOT synced to the guest session --
  // that blob migrates into user_preferences on registration and there is no league column
  // to migrate into. A sport is "what I follow" and persists; a league is a narrowing within
  // a browsing session. Promote it if that turns out to be the wrong read.
  const [leagueFilter, setLeagueFilter] = useState<string | null>(null);

  // Defaults to "today" — the backend only ever backfills/looks ahead 7 days either way
  // (see components/DateNavigator.tsx), so this always starts inside real, populated data.
  const [selectedDate, setSelectedDate] = useState<Date>(() => startOfDay(new Date()));
  // null means no status filter — every status for the day, which is the default.
  const [statusFilter, setStatusFilter] = useState<StatusFilter | null>(null);

  const sportsQuery = useQuery({ queryKey: ["sports"], queryFn: listSports });
  const fixturesQuery = useQuery({
    queryKey: [
      "fixtures",
      "picks",
      sportFilter,
      leagueFilter,
      selectedDate.toDateString(),
      minProbability,
      minOdds,
      statusFilter,
    ],
    queryFn: () => {
      // market/line are deliberately omitted — the backend's default (combined best pick
      // across every market: h2h, double chance, goals/corners O/U) is always what this
      // screen wants now; per-market filtering was removed as UI clutter nobody needed.
      const { from, to } = dayBounds(selectedDate);
      return listFixtures({
        sport_slug: sportFilter ?? undefined,
        league_slug: leagueFilter ?? undefined,
        date_from: from,
        date_to: to,
        limit: FIXTURES_PAGE_LIMIT,
        min_probability: minProbability,
        min_odds: minOdds,
        status: statusFilter ?? undefined,
      });
    },
  });

  const sections = useMemo(() => groupByLeague(fixturesQuery.data ?? []), [fixturesQuery.data]);

  async function onRefresh() {
    setRefreshing(true);
    await fixturesQuery.refetch();
    setRefreshing(false);
  }

  return (
    <View className="flex-1 bg-white dark:bg-black">
      <AppHeader />
      {/* Sport and date share one row: both are "what am I looking at" controls, and the
          old layout spent two full rows on them before any pick was visible. */}
      <View className="mb-2 flex-row items-center gap-2 px-4">
        <SportDropdown
          sports={sportsQuery.data ?? []}
          selected={{ sport: sportFilter, league: leagueFilter }}
          onSelect={(selection) => {
            // Both move together. Picking a different sport must clear a league that belongs
            // to the old one, or the query asks for e.g. football + wnba and returns nothing
            // while the trigger still reads like a valid choice.
            setSportFilter(selection.sport);
            setLeagueFilter(selection.league);
          }}
        />
        <DateNavigator selected={selectedDate} onSelect={setSelectedDate} />
      </View>
      {/* Status and Filters share the second row: both narrow what's listed, as opposed to
          the row above which chooses what's being looked at. */}
      <View className="mb-2 flex-row items-start gap-2 px-4">
        <StatusDropdown selected={statusFilter} onSelect={setStatusFilter} />
        <View className="flex-1">
          <FiltersPanel
            minProbability={minProbability}
            minProbabilityFloor={MIN_PROBABILITY_FLOOR}
            onMinProbabilityChange={setMinProbability}
            minOdds={minOdds}
            onMinOddsChange={setMinOdds}
          />
        </View>
      </View>
      {isGuest && <GuestBanner />}
      <SectionList
        sections={sections}
        keyExtractor={(item) => item.id}
        contentContainerClassName="px-4 pb-4"
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        renderSectionHeader={({ section }) => (
          <View className="mb-2 mt-3 flex-row items-center bg-white dark:bg-black">
            <View className="mr-2">
              <CountryFlag country={section.country} size={24} />
            </View>
            <View className="flex-1">
              {/* Says outright that these matches have no confirmed time, so the day they
                  appear under is our fallback rather than a scheduled slot. Better to admit
                  that than to imply a precision the provider has not given us. */}
              {section.timeUnconfirmed && (
                <Text className="text-[10px] font-bold uppercase tracking-wide text-amber-500">
                  Time to be confirmed
                </Text>
              )}
              <Text
                className="text-sm font-bold text-gray-900 dark:text-gray-100"
                numberOfLines={1}
              >
                {section.title}
              </Text>
              <View className="flex-row items-center">
                {section.country && (
                  <Text className="text-xs text-gray-400">{section.country}</Text>
                )}
                {section.country && section.surface && (
                  <Text className="text-xs text-gray-400"> · </Text>
                )}
                {section.surface && (
                  <Text className="text-xs font-medium text-gray-500 dark:text-gray-400">
                    {section.surface}
                  </Text>
                )}
              </View>
            </View>
          </View>
        )}
        renderItem={({ item }) => <FixtureCard fixture={item} />}
        ListEmptyComponent={
          fixturesQuery.isLoading ? (
            <Text className="mt-8 text-center text-gray-400">Loading picks…</Text>
          ) : fixturesQuery.isError ? (
            <Text className="mt-8 text-center text-red-500">
              Couldn&apos;t reach the SportPIQ API. Pull to retry.
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
