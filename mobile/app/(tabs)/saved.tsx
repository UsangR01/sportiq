import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Pressable, RefreshControl, SectionList, Text, View } from "react-native";

import { listWatchlist, removeFromWatchlist } from "@/lib/api/watchlist";
import type { WatchlistItem } from "@/lib/api/types";
import { pickHeadline } from "@/lib/pickFormat";
import { useAuthStore } from "@/store/authStore";

/** Saved fixtures, each showing THE PICK AS IT WAS WHEN SAVED.
 *
 * The receipt is the whole reason this screen exists. best_pick is recomputed on every
 * request and never stored, so the Picks feed can legitimately show a different call from one
 * visit to the next — reported as the app changing its mind overnight. Freezing the feed for
 * everyone would mean knowingly showing a stale number to someone still deciding; freezing
 * what THIS user acted on, at the moment they acted, costs nobody else anything.
 *
 * So this screen deliberately does NOT re-rank anything. It renders what the server recorded
 * at save time and nothing else.
 */
export default function SavedScreen() {
  const accessToken = useAuthStore((state) => state.accessToken);
  const isHydrated = useAuthStore((state) => state.isHydrated);
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const watchlistQuery = useQuery({
    queryKey: ["watchlist"],
    queryFn: listWatchlist,
    // Guests have no watchlist to fetch — the endpoint is auth-only by design (a guest session
    // is device-bound Redis state with a 24h TTL; a watchlist is durable and drives a push).
    enabled: !!accessToken,
  });

  const removeMutation = useMutation({
    mutationFn: removeFromWatchlist,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  if (!isHydrated) {
    return (
      <View className="flex-1 items-center justify-center bg-white dark:bg-black">
        <ActivityIndicator />
      </View>
    );
  }

  if (!accessToken) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="mb-2 text-center text-lg font-semibold text-slate-900 dark:text-white">
          Sign in to save picks
        </Text>
        <Text className="mb-6 text-center text-sm text-slate-500 dark:text-slate-400">
          Saving keeps a pick exactly as you saw it, even if the odds move afterwards — and lets
          us remind you before kick-off.
        </Text>
        <Link href="/auth/login" className="rounded-lg bg-blue-600 px-6 py-3 font-semibold text-white">
          Sign in
        </Link>
      </View>
    );
  }

  if (watchlistQuery.isLoading) {
    return (
      <View className="flex-1 items-center justify-center bg-white dark:bg-black">
        <ActivityIndicator />
      </View>
    );
  }

  if (watchlistQuery.isError) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="text-center text-red-500">Couldn&apos;t load your saved picks.</Text>
      </View>
    );
  }

  const items = watchlistQuery.data ?? [];
  if (items.length === 0) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="mb-2 text-center text-lg font-semibold text-slate-900 dark:text-white">
          Nothing saved yet
        </Text>
        <Text className="text-center text-sm text-slate-500 dark:text-slate-400">
          Open a fixture and tap Save to keep its pick exactly as it is now.
        </Text>
      </View>
    );
  }

  // Upcoming first: a saved list is read to answer "what am I waiting on". Settled ones are
  // kept rather than hidden — a saved match is still worth seeing the result of, and hiding
  // them would quietly delete the losses, which is the same bias that makes a retroactively
  // filtered track record look better than it was.
  const upcoming = items.filter((item) => item.status !== "completed");
  const settled = items.filter((item) => item.status === "completed");
  const sections = [
    { title: "Upcoming", data: upcoming },
    { title: "Finished", data: settled },
  ].filter((section) => section.data.length > 0);

  return (
    <SectionList
      className="flex-1 bg-white dark:bg-black"
      contentContainerClassName="px-4 pb-4"
      sections={sections}
      keyExtractor={(item) => item.fixture_id}
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
      renderSectionHeader={({ section }) => (
        <Text className="mb-2 mt-4 text-xs font-bold uppercase tracking-wide text-slate-400">
          {section.title}
        </Text>
      )}
      renderItem={({ item }) => (
        <SavedCard
          item={item}
          onRemove={() => removeMutation.mutate(item.fixture_id)}
          removing={removeMutation.isPending && removeMutation.variables === item.fixture_id}
        />
      )}
    />
  );
}

function SavedCard({
  item,
  onRemove,
  removing,
}: {
  item: WatchlistItem;
  onRemove: () => void;
  removing: boolean;
}) {
  const kickoff = new Date(item.kickoff_utc);
  const savedAt = new Date(item.created_at);
  const hasPick = item.saved_market != null && item.saved_selection != null;

  return (
    <View className="mb-3 rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <View className="mb-1 flex-row items-center justify-between">
        <Text className="text-[11px] uppercase text-slate-400">{item.league_slug}</Text>
        <Text className="text-[11px] text-slate-400">
          {item.kickoff_is_estimated ? "Time TBC" : kickoff.toLocaleString()}
        </Text>
      </View>
      <Link href={`/fixture/${item.fixture_id}`} asChild>
        <Pressable>
          <Text className="text-base font-semibold text-slate-900 dark:text-white">
            {item.home_team}
          </Text>
          <Text className="text-base font-semibold text-slate-900 dark:text-white">
            {item.away_team}
          </Text>
        </Pressable>
      </Link>

      <View className="mt-2 flex-row items-end justify-between">
        {hasPick ? (
          <View>
            {/* Labelled "You saved" rather than shown as a live pick, because that is exactly
                what it is — what the card said when this user acted, not what it says now. The
                fixture detail screen is where the current call lives. */}
            <Text className="text-[10px] uppercase tracking-wide text-slate-400">You saved</Text>
            <Text className="text-sm font-bold text-slate-900 dark:text-white">
              {pickHeadline({ selection: item.saved_selection!, line: item.saved_line })}
              {item.saved_probability != null
                ? ` · ${Math.round(item.saved_probability * 100)}%`
                : ""}
              {item.saved_odds != null ? ` · ${item.saved_odds.toFixed(2)}` : ""}
            </Text>
            <Text className="text-[10px] text-slate-400">
              on {savedAt.toLocaleDateString([], { day: "numeric", month: "short" })}
            </Text>
          </View>
        ) : (
          // Never invent a pick for a row that has none: saved before the receipt existed, or
          // saved on a fixture whose pick did not clear the guards.
          <Text className="text-xs text-slate-400">Saved · no pick recorded</Text>
        )}
        <Pressable
          onPress={onRemove}
          disabled={removing}
          className="rounded-md border border-slate-300 px-3 py-1.5 dark:border-slate-700"
        >
          <Text className="text-xs text-slate-500 dark:text-slate-400">
            {removing ? "Removing…" : "Remove"}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
