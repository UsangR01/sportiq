import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { router } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  Text,
  View,
} from "react-native";

import { GuestBanner } from "@/components/GuestBanner";
import { AdSlot, MIN_FEED_FOR_MPU, placeFeedAds, useAdsEnabled } from "@/lib/ads";
import { DateStepper, startOfDay } from "@/components/picks/DateStepper";
import { FilterSheet } from "@/components/picks/FilterSheet";
import { LeagueCard, LeagueGroupHeader } from "@/components/picks/LeagueGroup";
import { MatchRow } from "@/components/picks/MatchRow";
import { PicksHeader } from "@/components/picks/PicksHeader";
import { SegmentedControl } from "@/components/picks/SegmentedControl";
import { SportTabs, SPORT_TABS } from "@/components/picks/SportTabs";
import { SummaryStrip, type CountryOption } from "@/components/picks/SummaryStrip";
import { listFixturesWithCounts } from "@/lib/api/fixtures";
import type { MarketFamily } from "@/store/picksStore";
import type { BestPick, FixtureSummary, MarketCountsResponse } from "@/lib/api/types";
import { getPreferences } from "@/lib/api/users";
import { addToWatchlist, listWatchlist, removeFromWatchlist } from "@/lib/api/watchlist";
import { countryForTournamentLocation } from "@/lib/countryFlags";
import { toOddsFormat } from "@/lib/oddsFormat";
import { GAP, RADIUS, SCREEN, TYPE, useTheme } from "@/lib/theme";
import { useAuthStore } from "@/store/authStore";
import {
  hasActiveFilters,
  SUB_SPORTS,
  usePicksStore,
  type Segment,
} from "@/store/picksStore";

const FIXTURES_PAGE_LIMIT = 200;
const SEGMENTS: readonly Segment[] = ["All", "Live", "Upcoming", "Finished"];

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

/** Chip labels, in display order. Keys match the server's MARKET_FAMILIES. */
const MARKET_CHIPS: { key: MarketFamily; label: string }[] = [
  { key: "h2h", label: "1X2" },
  { key: "double_chance", label: "Double chance" },
  { key: "goals_total", label: "Goals" },
  { key: "corners_total", label: "Corners" },
];

interface MarketChip {
  key: MarketFamily;
  label: string;
  /** null while the server has not said -- rendered without a number, because "(0)" would
   *  read as "empty" when it means "not known yet". */
  count: number | null;
}

/** The chips to offer for the sports currently on screen, each with its own count.
 *
 * WHICH CHIPS: the server says, per sport, and this never keeps its own sport-to-market map.
 * Basketball and tennis predictions carry no draw_prob, xg or corners_xg, so double chance,
 * goals and corners cannot ever fill there -- but the day a basketball totals model lands, the
 * server starts reporting those families and the chip appears here with no release at all. A
 * copy of that knowledge on this side is exactly how the chips drifted before, when the client
 * held the totals line and counted 49 goals picks while tapping it asked for 43.
 *
 * NOT THE SAME TEST AS A COUNT OF ZERO, and the two must stay apart. Zero means "nothing
 * today" and keeps a dimmed, tappable chip, because a row whose chips come and go between days
 * cannot be learned. Inapplicable means "never, in this sport" and belongs off the row.
 *
 * THE COUNTS ARE SUMMED OVER THE SELECTED SPORTS, because the feed is fetched for the whole day
 * with no sport param and narrowed here (see the queryFn comment below). The server's pooled
 * number therefore describes fixtures that are not on screen: measured in production, with
 * Tennis selected the double-chance chip showed football's 85 and tapping it delivered nothing.
 *
 * Returns null when the server has said nothing about applicability -- an older API, or a
 * malformed header. The caller then shows every chip, which is the safe direction to fail: a
 * useless chip costs a tap, a missing one hides real picks.
 */
function marketChipsFor(
  counts: MarketCountsResponse | null | undefined,
  sports: string[]
): MarketChip[] | null {
  const applicable = counts?.applicable_by_sport;
  if (!applicable) return null;
  // An empty selection means every sport -- the filter sheet reaches it in one tap.
  const selected = sports.length ? sports : Object.keys(applicable);
  const allowed = new Set<string>();
  for (const sport of selected) {
    // A sport the server did not report gets every family, matching the server's own default
    // for an unknown sport: withholding a market from something newly added is the worse
    // failure of the two.
    for (const family of applicable[sport] ?? MARKET_CHIPS.map((c) => c.key)) {
      allowed.add(family);
    }
  }
  const breakdown = counts?.counts_by_sport;
  return MARKET_CHIPS.filter((chip) => allowed.has(chip.key)).map((chip) => ({
    ...chip,
    count: breakdown
      ? selected.reduce((n, sport) => n + (breakdown[sport]?.[chip.key] ?? 0), 0)
      : // No breakdown: the pooled count is wrong for a narrowed selection, but it is the only
        // number there is, and it was what shipped before this.
        (counts?.counts?.[chip.key] ?? null),
  }));
}

export default function PicksScreen() {
  const { colors } = useTheme();
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((state) => state.accessToken);
  const isGuest = !accessToken;

  const store = usePicksStore();
  // WHETHER, not what. False today for everyone — no provider is registered — so every slot
  // below collapses and the feed renders exactly as it does now.
  const adsEnabled = useAdsEnabled();
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
      // The market DOES belong in the key, unlike the sport: narrowing to one market changes
      // which candidate wins each fixture, so it cannot be re-filtered client-side the way
      // the sport chips are. keepPreviousData below is what stops the feed emptying between
      // the tap and the response -- the exact symptom the sport-chip comment above records.
      store.market,
    ],
    placeholderData: (previous) => previous,
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
      return listFixturesWithCounts({
        date_from: from,
        date_to: to,
        limit: FIXTURES_PAGE_LIMIT,
        min_probability: store.minProbability,
        min_odds: store.minOdds,
        market: store.market ?? undefined,
        // The totals markets require a line server-side. The chip is a FAMILY, so it picks
        // the market's own most-traded line rather than asking the user for one -- another
        // selector is the clutter that got the first version of these chips removed.
        // THE SERVER'S OWN LINE, from the counts response. Keeping a copy here is what made
        // the chip overstate: it counted every goals line while tapping it asked for 2.5.
        // NO LINE SENT. The server defaults a totals market to the line it counted, so the
        // client holds no copy to drift from -- which is what made the goals chip overstate.
      });
    },
  });

  // Derived once and used twice -- by the row, and by the reset below. Computing it in each
  // place would let the two disagree about whether the selected market still exists.
  const marketChips = useMemo(
    () => marketChipsFor(fixturesQuery.data?.marketCounts, store.sports),
    [fixturesQuery.data?.marketCounts, store.sports]
  );

  // A SELECTED MARKET THAT NO LONGER APPLIES MUST BE DROPPED, or it keeps filtering from
  // off-screen. Tapping Basketball with "Goals" active would otherwise leave the request
  // narrowed to a market basketball cannot produce, with no chip anywhere to show why the
  // card came back empty -- an invisible filter, which this project has twice decided is
  // worse than one the user cannot set (see toggleSport dropping orphaned tours).
  //
  // Guarded on marketChips being non-null: an older API says nothing about applicability, and
  // clearing the user's filter on the strength of silence would be its own bug.
  useEffect(() => {
    if (!marketChips || !store.market) return;
    if (!marketChips.some((chip) => chip.key === store.market)) store.setMarket(null);
  }, [marketChips, store.market, store.setMarket]);

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
    mutationFn: ({ id, saved, pick }: { id: string; saved: boolean; pick?: BestPick | null }) =>
      // The rendered pick travels with the save. The server ranks candidates without this
      // screen's odds slider, so left to itself it can record a market the card never showed.
      saved ? removeFromWatchlist(id) : addToWatchlist(id, pick),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  /** The filter pipeline, in the order §9.2 documents. Order matters: the country list is built
   * from the result of the FAVOURITES step but BEFORE the country filter, so switching country
   * is never a dead end. */
  const { groups, countries, visibleCount, liveCount } = useMemo(() => {
    const everything = fixturesQuery.data?.fixtures ?? [];

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
      if (store.segment === "Live") return fixture.status === "live";
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
      // FROM THE WHOLE DAY, before sport/segment/country narrowing. "Live (2)" answers "is
      // anything happening right now"; computing it after the filters would make it report
      // what survived them, which is a different and much less useful question.
      liveCount: everything.filter((f) => f.status === "live").length,
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

  // Which sport is on screen, when exactly one is -- used to name the empty state after the
  // thing the user actually chose.
  const soleSport =
    store.sports.length === 1
      ? SPORT_TABS.find((t) => t.slug === store.sports[0])
      : undefined;
  // Does that sport have ANY fixtures today, before this screen's own narrowing? This is the
  // difference between "your thresholds hid everything" and "there is no tennis today", and
  // sending someone to the odds slider when the truth is the latter wastes their time.
  const sportHasFixtures = (fixturesQuery.data?.fixtures ?? []).some(
    (f) => !soleSport || f.sport_slug === soleSport.slug
  );

  const emptyTitle =
    soleSport && !sportHasFixtures
      ? `No ${soleSport.label.toLowerCase()} today`
      : "No picks match your filters";
  const emptyReason =
    soleSport && !sportHasFixtures
      ? `We have no ${soleSport.label.toLowerCase()} fixtures on this date. Try another day or another sport.`
      : store.onlyFavourites
        ? "You're only showing starred leagues. None of them have a pick today."
        : store.country
          ? `No picks in ${store.country} today.`
          : "No pick clears your probability and odds thresholds for this day.";

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <PicksHeader
        isPremium={false}
        onOpenFilters={() => setFiltersOpen(true)}
        filtersActive={hasActiveFilters(store)}
      >
        <SportTabs selected={store.sports} onSelect={store.selectSport} />
        <DateStepper selected={store.selectedDate} onSelect={store.setSelectedDate} />
        <SummaryStrip
          callCount={visibleCount}
          leagueCount={groups.length}
          country={store.country}
          countries={countries}
          onSelectCountry={store.setCountry}
        />
        <SegmentedControl
          options={SEGMENTS}
          value={store.segment}
          onChange={store.setSegment}
          // Only when something IS live. A "Live (0)" label is noise, and the whole point of
          // this count is that it means something is happening right now.
          badges={liveCount > 0 ? { Live: String(liveCount) } : undefined}
        />
        <MarketChips
          value={store.market}
          markets={marketChips ?? MARKET_CHIPS.map((chip) => ({ ...chip, count: null }))}
          onChange={store.setMarket}
          busy={fixturesQuery.isFetching}
        />
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

        {/* isFetching, NOT isLoading. `isLoading` is true only on a FIRST load with no cached
            data, so with keepPreviousData every subsequent filter change refetched silently --
            the card list sat unchanged with no indication, then swapped. Reported as "nothing
            happens and after a while the cards are published". */}
        {fixturesQuery.isFetching && !fixturesQuery.isLoading && (
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              paddingVertical: 8,
            }}
          >
            <ActivityIndicator size="small" color={colors.accent} />
            <Text style={[TYPE.body, { color: colors.textSub }]}>Updating picks…</Text>
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
              {emptyTitle}
            </Text>
            <Text style={[TYPE.body, { color: colors.textSub, textAlign: "center" }]}>
              {emptyReason}
            </Text>
            {soleSport && !sportHasFixtures && (
              <Pressable
                onPress={store.clearSports}
                accessibilityRole="button"
                style={{
                  marginTop: 16,
                  paddingHorizontal: 20,
                  paddingVertical: 10,
                  borderRadius: RADIUS.button,
                  backgroundColor: colors.accent,
                }}
              >
                <Text style={[TYPE.pick, { color: "#ffffff" }]}>Show all sports</Text>
              </Pressable>
            )}
          </View>
        ) : (
          placeFeedAds(groups, (group) => group.matches.length, adsEnabled).map(
            ({ group, adAfter }) => (
            <View key={group.key}>
              <LeagueGroupHeader
                title={group.title}
                country={group.country}
                leagueSlug={group.leagueSlug}
                surface={group.surface}
                timeUnconfirmed={group.timeUnconfirmed}
                starred={store.favourites.includes(group.title)}
                onToggleStar={() => store.toggleFavourite(group.title)}
                count={group.matches.length}
                liveCount={group.matches.filter((f) => f.status === "live").length}
                collapsed={store.collapsed.includes(group.title)}
                onToggleCollapsed={() => store.toggleCollapsed(group.title)}
              />
              {/* The HEADER stays when collapsed, and so does any ad slot after the group --
                  collapsing hides a league's matches, it does not remove the league. */}
              {!store.collapsed.includes(group.title) && (
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
                    // Scoped to THIS fixture, not the mutation as a whole: a single
                    // isPending would show every visible card as saving at once.
                    savePending={
                      saveMutation.isPending && saveMutation.variables?.id === fixture.id
                    }
                    canSave={!isGuest}
                    onToggleSaved={() =>
                      isGuest
                        ? router.push("/auth/login")
                        : saveMutation.mutate({
                            id: fixture.id,
                            saved: savedIds.has(fixture.id),
                            pick: fixture.best_pick,
                          })
                    }
                  />
                ))}
              </LeagueCard>
              )}
              {/* Feed furniture rather than an interruption: the unit sits inside the group's
                  own bottom spacing, after a whole league rather than between two matches.
                  Renders nothing until a provider exists — see lib/ads. */}
              {adAfter && (
                <View style={{ marginTop: GAP.card }}>
                  <AdSlot id={adAfter} />
                </View>
              )}
            </View>
            )
          )
        )}

        {/* End-of-feed MPU, and it COLLAPSES on a short day rather than padding it. This app's
            own odds floor can legitimately empty a card, and an advert is the last thing that
            should fill a screen which had no picks to show. */}
        {adsEnabled && visibleCount >= MIN_FEED_FOR_MPU && (
          <View style={{ marginTop: GAP.leagueGroup }}>
            <AdSlot id="picks_mpu_1" />
          </View>
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


/** Market filter chips, each carrying its own count.
 *
 * THE COUNT IS THE WHOLE POINT. These chips existed once and were removed as clutter -- a row
 * of filters that mostly led to empty feeds. The count comes from the server running the SAME
 * selection restricted to that market, so a chip reading "Corners (0)" tells the user not to
 * tap it rather than letting them discover it.
 *
 * A market with no picks today is rendered DIMMED AND STILL TAPPABLE rather than hidden:
 * chips that appear and disappear between days make the row impossible to learn.
 */
function MarketChips({
  value,
  markets,
  onChange,
  busy,
}: {
  value: MarketFamily | null;
  /** Only the markets this sport can produce, already counted for the current selection --
   *  see marketChipsFor. */
  markets: MarketChip[];
  onChange: (next: MarketFamily | null) => void;
  /** A fetch is in flight for the current selection. The SELECTED chip shows a spinner in
   *  place of its count, so the feedback sits where the finger just was rather than only at
   *  the top of a list the user may have scrolled past. */
  busy: boolean;
}) {
  const { colors } = useTheme();
  // ONE market means "All" and that market are the same filter, so the row would be two chips
  // that do nothing different -- worse than no row. True for basketball and tennis today,
  // which have h2h alone.
  if (markets.length < 2) return null;
  const chips: { key: MarketFamily | null; label: string; count: number | null }[] = [
    { key: null, label: "All", count: null },
    ...markets.map((chip) => ({
      key: chip.key as MarketFamily | null,
      label: chip.label,
      count: chip.count,
    })),
  ];
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      className="grow-0"
      contentContainerStyle={{ gap: 6, paddingVertical: 6 }}
    >
      {chips.map((chip) => {
        const selected = value === chip.key;
        const empty = chip.count === 0;
        return (
          <Pressable
            key={chip.key ?? "all"}
            onPress={() => onChange(chip.key)}
            accessibilityRole="button"
            accessibilityState={{ selected }}
            accessibilityLabel={
              chip.count === null ? chip.label : `${chip.label}, ${chip.count} picks`
            }
            style={{
              paddingHorizontal: 12,
              paddingVertical: 6,
              borderRadius: 999,
              backgroundColor: selected ? colors.accent : colors.surfaceAlt,
              opacity: !selected && empty ? 0.45 : 1,
            }}
          >
            <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
              <Text
                style={{
                  fontSize: 13,
                  fontWeight: selected ? "700" : "500",
                  color: selected ? "#fff" : colors.text,
                }}
              >
                {chip.label}
                {selected && busy ? "" : chip.count === null ? "" : ` (${chip.count})`}
              </Text>
              {selected && busy && <ActivityIndicator size="small" color="#fff" />}
            </View>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}
