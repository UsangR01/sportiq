import { useQuery } from "@tanstack/react-query";
import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

import { getPreferences } from "@/lib/api/users";
import { useAuthStore } from "@/store/authStore";

export default function ProfileScreen() {
  const email = useAuthStore((s) => s.email);
  const accessToken = useAuthStore((s) => s.accessToken);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const isGuest = accessToken === null;

  const preferencesQuery = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: !isGuest,
  });

  if (isGuest) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="mb-2 text-center text-lg font-semibold text-gray-900 dark:text-gray-100">
          Sign in to save picks and get alerts
        </Text>
        <Text className="mb-6 text-center text-sm text-gray-500 dark:text-gray-400">
          Home, Picks, and Live all work without an account — profile settings and saved
          picks need one.
        </Text>
        <Link href="/auth/login" asChild>
          <Pressable className="mb-3 w-full rounded-lg bg-blue-600 py-3">
            <Text className="text-center font-semibold text-white">Log In</Text>
          </Pressable>
        </Link>
        <Link href="/auth/register" asChild>
          <Pressable className="w-full rounded-lg border border-blue-600 py-3">
            <Text className="text-center font-semibold text-blue-600">Sign Up</Text>
          </Pressable>
        </Link>
      </View>
    );
  }

  return (
    <View className="flex-1 bg-white px-4 pt-4 dark:bg-black">
      <Text className="mb-6 text-base text-gray-900 dark:text-gray-100">{email}</Text>

      {preferencesQuery.isLoading && (
        <Text className="text-gray-400">Loading preferences…</Text>
      )}
      {preferencesQuery.data && (
        <View className="mb-6">
          <Row label="Default min odds" value={String(preferencesQuery.data.default_min_odds ?? "—")} />
          <Row label="Odds format" value={preferencesQuery.data.odds_format} />
        </View>
      )}

      <Pressable
        className="w-full rounded-lg border border-red-500 py-3"
        onPress={() => clearAuth()}
      >
        <Text className="text-center font-semibold text-red-500">Log Out</Text>
      </Pressable>
    </View>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View className="mb-2 flex-row justify-between border-b border-gray-100 py-2 dark:border-gray-800">
      <Text className="text-gray-500 dark:text-gray-400">{label}</Text>
      <Text className="font-medium text-gray-900 dark:text-gray-100">{value}</Text>
    </View>
  );
}
