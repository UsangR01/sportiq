import { Link, Stack } from "expo-router";
import { Text, View } from "react-native";

export default function NotFoundScreen() {
  return (
    <>
      <Stack.Screen options={{ title: "Oops!" }} />
      <View className="flex-1 items-center justify-center bg-white p-5 dark:bg-black">
        <Text className="text-lg font-bold text-gray-900 dark:text-gray-100">
          This screen doesn&apos;t exist.
        </Text>
        <Link href="/" className="mt-4 py-4">
          <Text className="text-sm text-blue-600">Go to home screen!</Text>
        </Link>
      </View>
    </>
  );
}
