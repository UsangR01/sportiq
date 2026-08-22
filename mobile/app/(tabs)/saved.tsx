import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { router } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import type { WatchlistItem } from "@/lib/api/types";
import { getPreferences } from "@/lib/api/users";
import { listWatchlist, removeFromWatchlist } from "@/lib/api/watchlist";
import { formatOdds, toOddsFormat, type OddsFormat } from "@/lib/oddsFormat";
import { pickHeadline } from "@/lib/pickFormat";
import { GAP, ONE_LINE, RADIUS, RESULT_DISC, SCREEN, TYPE, useTheme } from "@/lib/theme";
import { useAuthStore } from "@/store/authStore";

/** Saved picks (design spec §6.5).
 *
 * THIS SCREEN RENDERS A RECEIPT, and that is its entire purpose. Every row shows the pick as it
 * was WHEN THE USER SAVED IT — market, selection, line, probability and odds, captured
 * server-side at save time. It does not re-rank, re-price or recompute anything.
 *
 * That matters because best_pick is recomputed on every request and never stored, so the feed
 * can legitimately show a different call tomorrow as odds land and the model re-runs. Freezing
 * the feed for everyone would mean knowingly showing a stale number to someone still deciding;
 * freezing what THIS user acted on costs nobody else anything.
 */
export default function SavedScreen() {
  const { colors } = useTheme();
  const accessToken = useAuthStore((state) => state.accessToken);
  const isHydrated = useAuthStore((state) => state.isHydrated);
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const preferencesQuery = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: !!accessToken,
  });
  const oddsFormat = toOddsFormat(preferencesQuery.data?.odds_format);

  const watchlistQuery = useQuery({
    queryKey: ["watchlist"],
    queryFn: listWatchlist,
    // Guests have no watchlist to fetch — the endpoint is auth-only by design: a guest session
    // is device-bound state with a 24h TTL, while a saved pick is durable and drives a push.
    enabled: !!accessToken,
  });

  const removeMutation = useMutation({
    mutationFn: removeFromWatchlist,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const items = watchlistQuery.data ?? [];
  // Upcoming first: a saved list answers "what am I waiting on". Finished ones are KEPT rather
  // than hidden — hiding settled cards would quietly delete the losses, which is the same bias
  // that makes a retroactively filtered track record look better than it was.
  const upcoming = items.filter((item) => item.status !== "completed");
  const finished = items.filter((item) => item.status === "completed");

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <View
        style={{
          paddingTop: SCREEN.paddingTop,
          paddingHorizontal: SCREEN.padding,
          paddingBottom: 12,
          flexDirection: "row",
          alignItems: "center",
        }}
      >
        <Text {...ONE_LINE} style={[TYPE.wordmark, { color: colors.text, flex: 1 }]}>
          SportPIQ
        </Text>
        {accessToken && items.length > 0 && (
          <Text style={[TYPE.caption, { color: colors.textFaint }]}>{items.length} saved</Text>
        )}
      </View>

      {!isHydrated || (accessToken && watchlistQuery.isLoading) ? (
        <Centered>
          <ActivityIndicator color={colors.accent} />
        </Centered>
      ) : !accessToken ? (
        <Centered>
          <Text style={[TYPE.pick, { fontSize: 15, color: colors.text, marginBottom: 8 }]}>
            Sign in to save picks
          </Text>
          <Text
            style={[TYPE.body, { color: colors.textSub, textAlign: "center", marginBottom: 20 }]}
          >
            Saving keeps a pick exactly as you saw it, even if the odds move afterwards — and
            lets us remind you before kick-off.
          </Text>
          <Pressable
            onPress={() => router.push("/auth/login")}
            accessibilityRole="button"
            style={{
              paddingHorizontal: 24,
              paddingVertical: 12,
              borderRadius: RADIUS.button,
              backgroundColor: colors.accent,
            }}
          >
            <Text style={[TYPE.pick, { color: "#ffffff" }]}>Sign in</Text>
          </Pressable>
        </Centered>
      ) : watchlistQuery.isError ? (
        <Centered>
          <Text style={[TYPE.body, { color: colors.fail }]}>
            Couldn&apos;t load your saved picks.
          </Text>
        </Centered>
      ) : items.length === 0 ? (
        <Centered>
          <Text style={[TYPE.pick, { fontSize: 15, color: colors.text, marginBottom: 8 }]}>
            Nothing saved yet
          </Text>
          <Text style={[TYPE.body, { color: colors.textSub, textAlign: "center" }]}>
            Open a match, expand it, and tap Save to keep its pick exactly as it is now.
          </Text>
        </Centered>
      ) : (
        <ScrollView
          contentContainerStyle={{ paddingHorizontal: SCREEN.padding, paddingBottom: 24 }}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={async () => {
                setRefreshing(true);
                await watchlistQuery.refetch();
                setRefreshing(false);
              }}
            />
          }
        >
          {upcoming.length > 0 && (
            <Section label="Upcoming">
              {upcoming.map((item) => (
                <SavedCard
                  key={item.fixture_id}
                  item={item}
                  oddsFormat={oddsFormat}
                  onRemove={() => removeMutation.mutate(item.fixture_id)}
                  removing={
                    removeMutation.isPending && removeMutation.variables === item.fixture_id
                  }
                />
              ))}
            </Section>
          )}
          {finished.length > 0 && (
            <Section label="Finished">
              {finished.map((item) => (
                <SavedCard
                  key={item.fixture_id}
                  item={item}
                  oddsFormat={oddsFormat}
                  onRemove={() => removeMutation.mutate(item.fixture_id)}
                  removing={
                    removeMutation.isPending && removeMutation.variables === item.fixture_id
                  }
                />
              ))}
            </Section>
          )}
        </ScrollView>
      )}
    </View>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <View
      style={{
        flex: 1,
        alignItems: "center",
        justifyContent: "center",
        paddingHorizontal: 32,
        paddingBottom: 60,
      }}
    >
      {children}
    </View>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  const { colors } = useTheme();
  return (
    <View style={{ marginBottom: GAP.leagueGroup }}>
      <Text style={[TYPE.eyebrow, { color: colors.textFaint, marginBottom: 9, marginTop: 4 }]}>
        {label}
      </Text>
      {children}
    </View>
  );
}

function SavedCard({
  item,
  oddsFormat,
  onRemove,
  removing,
}: {
  item: WatchlistItem;
  oddsFormat: OddsFormat;
  onRemove: () => void;
  removing: boolean;
}) {
  const { colors, elevation } = useTheme();
  const kickoff = new Date(item.kickoff_utc);
  const savedAt = new Date(item.created_at);
  const hasPick = item.saved_market != null && item.saved_selection != null;

  return (
    <View
      style={{
        backgroundColor: colors.surface,
        borderRadius: RADIUS.control,
        borderWidth: 1,
        borderColor: colors.border,
        padding: 14,
        marginBottom: GAP.card,
        gap: 10,
        ...elevation.card,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Text {...ONE_LINE} style={[TYPE.eyebrowSmall, { color: colors.textFaint, flex: 1 }]}>
          {item.league_slug}
        </Text>
        <Text style={[TYPE.caption, { color: colors.textFaint, fontWeight: "400" }]}>
          {item.kickoff_is_estimated
            ? "Time TBC"
            : kickoff.toLocaleString(undefined, {
                day: "numeric",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
              })}
        </Text>
      </View>

      <Pressable onPress={() => router.push(`/fixture/${item.fixture_id}`)} accessibilityRole="link">
        <Text {...ONE_LINE} style={[TYPE.team, { color: colors.text }]}>
          {item.home_team}
        </Text>
        <Text {...ONE_LINE} style={[TYPE.team, { color: colors.text }]}>
          {item.away_team}
        </Text>
      </Pressable>

      <View style={{ flexDirection: "row", alignItems: "flex-end", gap: 12 }}>
        <View style={{ flex: 1 }}>
          {hasPick ? (
            <>
              {/* Labelled as what it is: what the card said when this user acted, not what it
                  says now. The fixture screen is where the current call lives. */}
              <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint }]}>You saved</Text>
              <Text {...ONE_LINE} style={[TYPE.pick, { color: colors.text }]}>
                {pickHeadline({ selection: item.saved_selection!, line: item.saved_line })}
                {item.saved_probability != null
                  ? ` · ${Math.round(item.saved_probability * 100)}%`
                  : ""}
                {item.saved_odds != null ? ` · ${formatOdds(item.saved_odds, oddsFormat)}` : ""}
              </Text>
              <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, letterSpacing: 0 }]}>
                on {savedAt.toLocaleDateString(undefined, { day: "numeric", month: "short" })}
              </Text>
            </>
          ) : (
            // Never invent a pick for a row that has none — saved before the receipt existed,
            // or saved on a fixture whose pick did not clear the guards.
            <Text style={[TYPE.caption, { color: colors.textFaint, fontWeight: "400" }]}>
              Saved · no pick recorded
            </Text>
          )}
        </View>

        <Pressable
          onPress={onRemove}
          disabled={removing}
          accessibilityRole="button"
          style={{
            paddingHorizontal: 12,
            paddingVertical: 7,
            borderRadius: RADIUS.chipTight,
            borderWidth: 1,
            borderColor: colors.border,
            opacity: removing ? 0.5 : 1,
          }}
        >
          <Text style={[TYPE.caption, { color: colors.textSub }]}>
            {removing ? "Removing…" : "Remove"}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
