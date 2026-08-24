import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { router } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import { RefreshControl, ScrollView, Text, View } from "react-native";

import { GuestBanner } from "@/components/GuestBanner";
import { DateStepper, startOfDay } from "@/components/picks/DateStepper";
import { FilterSheet } from "@/components/picks/FilterSheet";
import { LeagueCard, LeagueGroupHeader } from "@/components/picks/LeagueGroup";
import { MatchRow } from "@/components/picks/MatchRow";
import { PicksHeader } from "@/components/picks/PicksHeader";
import { SegmentedControl } from "@/components/picks/SegmentedControl";
import { SummaryStrip, type CountryOption } from "@/components/picks/SummaryStrip";
import { listFixtures } from "@/lib/api/fixtures";
import type { FixtureSummary } from "@/lib/api/types";
import { getPreferences } from "@/lib/api/users";
import { addToWatchlist, listWatchlist, removeFromWatchlist } from "@/lib/api/watchlist";
import { countryForTournamentLocation } from "@/lib/countryFlags";
import { toOddsFormat } from "@/lib/oddsFormat";
import { GAP, SCREEN, TYPE, useTheme } from "@/lib/theme";
import { useAuthStore } from "@/store/authStore";
import {
  hasActiveFilters,
  SUB_SPORTS,
  usePicksStore,
  type Segment,
} from "@/store/picksStore";
import { useThemeStore } from "@/store/themeStore";

const FIXTURES_PAGE_LIMIT = 200;
const SEGMENTS: readonly Segment[] = ["All", "Upcoming", "Finished"];

function dayBounds(date: Date): { from: string; to: string } {
  const from = startOfDay(date);
  const to = new Date(from);
  to.setHours(23, 59, 59, 999);
  return { from: from.toISOString(), to: to.toISOString() };
}

interface LeagueGroup {
  key: string;
  title: string;
  leagueSlug: string | null;
  country: string | null;
  surface: string | null;
  timeUnconfirmed: boolean;
  matches: FixtureSummary[];
}

/** A short display code for the country picker's badge.
 *
 * Derived rather than looked up: the app has no country→ISO table, and inventing one for
 * every country a provider might return would go stale silently. Three letters of the name is
 * imperfect ("Czech-Republic" → "CZE") but it is honest, stable, and only ever appears beside
 * the full country name, which carries the real meaning.
 */
function countryCode(name: string): string {
  return name.replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
}

/** Group a day's fixtures the way the feed renders them.
 *
 * Football and basketball group by LEAGUE; tennis groups by TOURNAMENT, because a whole tour is
 * one league row and grouping by it renders an undifferentiated wall of matches. A fixture with
 * no tournament data falls back to its league rather than being dropped.
 *
 * Fixtures with only an ESTIMATED kickoff get their own group: they are stored at midnight and
 * would otherwise interleave with real times, presenting a guess as fact.
 */
function groupFixtures(fixtures: FixtureSummary[]): LeagueGroup[] {
  const groups = new Map<string, LeagueGroup>();
  for (const fixture of fixtures) {
    const byTournament = fixture.tournament_name != null;
    const suffix = fixture.kickoff_is_estimated ? ":tbc" : "";
    const key = byTournament
      ? `tournament:${fixture.tournament_name}${suffix}`
      : `league:${fixture.league_slug}${suffix}`;

    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        title: byTournament ? fixture.tournament_name! : fixture.league_name,
        leagueSlug: byTournament ? null : fixture.league_slug,
        country: byTournament
          ? countryForTournamentLocation(fixture.tournament_location)
          : fixture.league_country,
        surface: byTournament ? fixture.tournament_surface : null,
        timeUnconfirmed: fixture.kickoff_is_estimated,
        matches: [],
      };
      groups.set(key, group);
    }
    group.matches.push(fixture);
  }

  const out = Array.from(groups.values());
  for (const group of out) {
    group.matches.sort((a, b) => {
      const aLive = a.status === "live";
      const bLive = b.status === "live";
      if (aLive !== bLive) return aLive ? -1 : 1;
      return new Date(a.kickoff_utc).getTime() - new Date(b.kickoff_utc).getTime();
    });
  }
  return out;
}

export default function PicksScreen() {
  const { colors, isDark } = useTheme();
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((state) => state.accessToken);
  const isGuest = !accessToken;
  const setThemePreference = useThemeStore((state) => state.setPreference);

  const store = usePicksStore();
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!store.favouritesHydrated) void store.hydrateFavourites();
  }, [store]);

  const preferencesQuery = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: !isGuest,
  });
  const oddsFormat = toOddsFormat(preferencesQuery.data?.odds_format);

  const fixturesQuery = useQuery({
    queryKey: [
      "fixtures",
      store.selectedDate.toDateString(),
      // NO sport/tour here, deliberately. The request does not carry them, so including them
      // would mint a fresh cache entry on every chip tap — and with no placeholder the feed
      // empties to "0 matches" mid-fetch. Measured: adding Tennis alongside Football took a
      // 5-match card to 0 until the needless refetch landed.
      store.minProbability,
      store.minOdds,
    ],
    queryFn: () => {
      const { from, to } = dayBounds(store.selectedDate);
      // FETCHED WITHOUT A SPORT PARAM and filtered below, because the endpoint takes a single
      // slug and the selection is now a set. One request per day beats one per selected sport,
      // the day's whole card is ~60 fixtures, and it means the cache key is the DAY rather
      // than day-plus-selection — so toggling a chip re-filters instantly instead of refetching.
      //
      // It also keeps league suppression applied: passing league_slug deliberately BYPASSES
      // SUPPRESSED_LEAGUES server-side so deep links keep working, which would have quietly
      // reintroduced MLS the moment a tour chip was selected.
      return listFixtures({
        date_from: from,
        date_to: to,
        limit: FIXTURES_PAGE_LIMIT,
        min_probability: store.minProbability,
        min_odds: store.minOdds,
      });
    },
  });

  const watchlistQuery = useQuery({
    queryKey: ["watchlist"],
    queryFn: listWatchlist,
    enabled: !isGuest,
  });
  const savedIds = useMemo(
    () => new Set((watchlistQuery.data ?? []).map((item) => item.fixture_id)),
    [watchlistQuery.data]
  );
  const saveMutation = useMutation({
    mutationFn: ({ id, saved }: { id: string; saved: boolean }) =>
      saved ? removeFromWatchlist(id) : addToWatchlist(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  /** The filter pipeline, in the order §9.2 documents. Order matters: the country list is built
   * from the result of the FAVOURITES step but BEFORE the country filter, so switching country
   * is never a dead end. */
  const { groups, countries, visibleCount } = useMemo(() => {
    const everything = fixturesQuery.data ?? [];

    // 1. SPORT / TOUR, applied here rather than server-side because the selection is a SET and
    //    the endpoint takes one slug. An empty list means "no restriction" — that is what the
    //    All chip produces, and a feed emptied by deselecting every chip would be a trap.
    //
    //    A tour selection only narrows the sport it belongs to: picking ATP must not also hide
    //    football. Sports with no tours (football) are unaffected by the second test.
    const all = everything.filter((fixture) => {
      if (store.sports.length && !store.sports.includes(fixture.sport_slug)) return false;
      const tours = SUB_SPORTS[fixture.sport_slug];
      if (!tours || !store.subSports.length) return true;
      const chosen = store.subSports.filter((tour) => tours.includes(tour));
      return chosen.length === 0 || chosen.includes(fixture.league_slug);
    });

    // 2. Postponed fixtures survive every threshold — the server already exempts them, and a
    //    called-off match is worth seeing on the day's schedule.
    // 3. probability/odds floors are applied SERVER-side (see listFixtures above).
    // 4. segment
    const bySegment = all.filter((fixture) => {
      if (store.segment === "All") return true;
      if (store.segment === "Finished") return fixture.status === "completed";
      // UPCOMING MEANS NOT STARTED. A match in progress is not upcoming, and lumping it in
      // here was reported: live games appeared among fixtures that had not kicked off, with
      // nothing distinguishing them. In-play matches belong in All and on the Live tab.
      //
      // Postponed IS kept: it was on today's card as an upcoming fixture, and someone
      // browsing "what's coming up" needs to see it was called off rather than have it
      // silently vanish.
      return fixture.status === "scheduled" || fixture.status === "postponed";
    });

    // 5. group, dropping any that end up empty
    let built = groupFixtures(bySegment).filter((group) => group.matches.length > 0);

    // 6. favourites-only
    if (store.onlyFavourites) {
      built = built.filter((group) => store.favourites.includes(group.title));
    }

    // 7. starred leagues to the front, stable within each band
    const starred = built.filter((group) => store.favourites.includes(group.title));
    const rest = built.filter((group) => !store.favourites.includes(group.title));
    built = [...starred, ...rest];

    // 8. country list from THIS point, before the country filter is applied
    const counts = new Map<string, number>();
    for (const group of built) {
      if (!group.country) continue;
      counts.set(group.country, (counts.get(group.country) ?? 0) + group.matches.length);
    }
    const countryOptions: CountryOption[] = Array.from(counts.entries())
      .map(([name, count]) => ({ name, code: countryCode(name), count }))
      .sort((a, b) => a.name.localeCompare(b.name));

    // 9. country filter
    const filtered = store.country
      ? built.filter((group) => group.country === store.country)
      : built;

    return {
      groups: filtered,
      countries: countryOptions,
      visibleCount: filtered.reduce((sum, group) => sum + group.matches.length, 0),
    };
  }, [
    fixturesQuery.data,
    // Both are replaced wholesale on every toggle, so referential deps are enough here.
    store.sports,
    store.subSports,
    store.segment,
    store.onlyFavourites,
    store.favourites,
    store.country,
  ]);

  async function onRefresh() {
    setRefreshing(true);
    await fixturesQuery.refetch();
    setRefreshing(false);
  }

  const emptyReason = store.onlyFavourites
    ? "You're only showing starred leagues. None of them have a pick today."
    : store.country
      ? `No picks in ${store.country} today.`
      : "No pick clears your probability and odds thresholds for this day.";

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <PicksHeader
        isPremium={false}
        isDark={isDark}
        onToggleTheme={() => setThemePreference(isDark ? "light" : "dark")}
        onOpenHub={() => router.push("/how-it-works")}
        onOpenFilters={() => setFiltersOpen(true)}
        filtersActive={hasActiveFilters(store)}
      >
        <DateStepper selected={store.selectedDate} onSelect={store.setSelectedDate} />
        <SummaryStrip
          callCount={visibleCount}
          leagueCount={groups.length}
          country={store.country}
          countries={countries}
          onSelectCountry={store.setCountry}
        />
        <SegmentedControl options={SEGMENTS} value={store.segment} onChange={store.setSegment} />
      </PicksHeader>

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: SCREEN.padding,
          paddingTop: GAP.card,
          paddingBottom: 24,
        }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
      >
        {isGuest && (
          <View style={{ marginBottom: GAP.leagueGroup }}>
            <GuestBanner />
          </View>
        )}

        {fixturesQuery.isLoading ? (
          <Text style={[TYPE.body, { color: colors.textFaint, textAlign: "center", marginTop: 32 }]}>
            Loading picks…
          </Text>
        ) : fixturesQuery.isError ? (
          <Text style={[TYPE.body, { color: colors.fail, textAlign: "center", marginTop: 32 }]}>
            Couldn&apos;t reach the SportPIQ API. Pull to retry.
          </Text>
        ) : groups.length === 0 ? (
          <View style={{ alignItems: "center", marginTop: 40, paddingHorizontal: 12 }}>
            <Text style={[TYPE.pick, { fontSize: 15, color: colors.text, marginBottom: 6 }]}>
              No picks match your filters
            </Text>
            <Text style={[TYPE.body, { color: colors.textSub, textAlign: "center" }]}>
              {emptyReason}
            </Text>
          </View>
        ) : (
          groups.map((group) => (
            <View key={group.key}>
              <LeagueGroupHeader
                title={group.title}
                country={group.country}
                leagueSlug={group.leagueSlug}
                surface={group.surface}
                timeUnconfirmed={group.timeUnconfirmed}
                starred={store.favourites.includes(group.title)}
                onToggleStar={() => store.toggleFavourite(group.title)}
              />
              <LeagueCard>
                {group.matches.map((fixture, index) => (
                  <MatchRow
                    key={fixture.id}
                    fixture={fixture}
                    first={index === 0}
                    expanded={store.expanded === fixture.id}
                    onToggle={() => store.toggleExpanded(fixture.id)}
                    oddsFormat={oddsFormat}
                    isSaved={savedIds.has(fixture.id)}
                    canSave={!isGuest}
                    onToggleSaved={() =>
                      isGuest
                        ? router.push("/auth/login")
                        : saveMutation.mutate({
                            id: fixture.id,
                            saved: savedIds.has(fixture.id),
                          })
                    }
                  />
                ))}
              </LeagueCard>
            </View>
          ))
        )}
      </ScrollView>

      <FilterSheet
        visible={filtersOpen}
        onClose={() => setFiltersOpen(false)}
        sports={store.sports}
        subSports={store.subSports}
        onToggleSport={store.toggleSport}
        onToggleSubSport={store.toggleSubSport}
        onClearSports={store.clearSports}
        minProbability={store.minProbability}
        onMinProbabilityChange={store.setMinProbability}
        minOdds={store.minOdds}
        onMinOddsChange={store.setMinOdds}
        oddsFormat={oddsFormat}
        matchCount={visibleCount}
        onReset={store.resetFilters}
      />
    </View>
  );
}
