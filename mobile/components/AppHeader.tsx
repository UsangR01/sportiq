import { Link } from "expo-router";
import { Pressable, Text, View } from "react-native";

/** App title bar rendered inside the screen rather than by Expo Router's own header.
 *
 * The native header is disabled in (tabs)/_layout.tsx so the title, sport selector, date
 * navigator and filters read as one continuous control surface — the router header sat in a
 * separate band above them and broke that grouping. */
export function AppHeader() {
  return (
    <View className="flex-row items-center justify-between px-4 pb-1 pt-2">
      <Text className="text-3xl font-extrabold tracking-tight text-gray-900 dark:text-white">
        SportPIQ
      </Text>
      <Link href="/how-it-works" asChild>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="How SportPIQ works"
          className="h-11 w-11 items-center justify-center rounded-2xl bg-gray-100 active:opacity-70 dark:bg-gray-800"
        >
          <LogoMark />
        </Pressable>
      </Link>
    </View>
  );
}

/** Three dots, drawn with plain Views rather than an icon font or SVG — the app has no vector
 * dependency and this avoids adding one for a single decorative mark. */
function LogoMark() {
  return (
    <View className="h-5 w-5">
      <View className="absolute right-0 top-0 h-2 w-2 rounded-full bg-blue-500" />
      <View className="absolute bottom-0 left-0 h-2.5 w-2.5 rounded-full bg-blue-600" />
      <View className="absolute bottom-0 right-0 h-2 w-2 rounded-full bg-blue-400" />
    </View>
  );
}
