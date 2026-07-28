import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

export function GuestBanner() {
  return (
    <View className="mx-4 mb-3 flex-row items-center justify-between rounded-lg bg-blue-50 px-4 py-2 dark:bg-blue-950">
      <Text className="flex-1 text-sm text-blue-900 dark:text-blue-100">
        Sign up to save picks and get alerts.
      </Text>
      <Link href="/auth/register" asChild>
        <Pressable>
          <Text className="text-sm font-semibold text-blue-600 dark:text-blue-300">
            Sign up
          </Text>
        </Pressable>
      </Link>
    </View>
  );
}
