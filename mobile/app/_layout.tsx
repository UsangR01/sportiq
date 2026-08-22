import { QueryClientProvider } from "@tanstack/react-query";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { useFonts } from "expo-font";
import { DarkTheme, DefaultTheme, router, Stack, ThemeProvider } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import "react-native-reanimated";

import "../global.css";
import { initErrorReporting } from "@/lib/errorReporting";
import { useColorScheme } from "@/components/useColorScheme";
import { addNotificationTapListener } from "@/lib/notifications";
import { getPreferences } from "@/lib/api/users";
import { queryClient } from "@/lib/queryClient";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";
import { useThemeStore } from "@/store/themeStore";

export {
  // Catch any errors thrown by the Layout component.
  ErrorBoundary,
} from "expo-router";

export const unstable_settings = {
  initialRouteName: "(tabs)",
};

SplashScreen.preventAutoHideAsync();

// Before any component renders. An error thrown during the first render — the class of bug
// that shows a user a blank screen — happens before any useEffect could have installed a
// handler, so initialising here is what makes those reportable at all.
initErrorReporting();

export default function RootLayout() {
  const [loaded, error] = useFonts({
    SpaceMono: require("../assets/fonts/SpaceMono-Regular.ttf"),
  });
  const hydrateAuth = useAuthStore((s) => s.hydrate);
  const authHydrated = useAuthStore((s) => s.isHydrated);
  const accessToken = useAuthStore((s) => s.accessToken);
  const hydratePreferences = usePreferencesStore((s) => s.hydrate);
  const preferencesHydrated = usePreferencesStore((s) => s.isHydrated);
  const ensureGuestSession = usePreferencesStore((s) => s.ensureGuestSession);
  const hydrateTheme = useThemeStore((s) => s.hydrate);

  useEffect(() => {
    if (error) throw error;
  }, [error]);

  useEffect(() => {
    hydrateAuth();
    hydratePreferences();
    // Applied before first paint where possible, so a user who chose dark doesn't get a
    // light flash while auth/preferences resolve over the network.
    hydrateTheme();
  }, [hydrateAuth, hydratePreferences, hydrateTheme]);

  useEffect(() => {
    if (preferencesHydrated && authHydrated && !accessToken) {
      ensureGuestSession();
    }
  }, [preferencesHydrated, authHydrated, accessToken, ensureGuestSession]);

  useEffect(() => {
    if (loaded && authHydrated && preferencesHydrated) {
      SplashScreen.hideAsync();
    }
  }, [loaded, authHydrated, preferencesHydrated]);

  // Deep-links a tapped push notification straight to the fixture it's about (TDD §5.4) —
  // notify_users.py's data payload is always {fixture_id}. A safe no-op wherever
  // expo-notifications itself isn't available (Expo Go on Android, SDK 53+ — see
  // lib/notifications.ts).
  useEffect(() => {
    return addNotificationTapListener((fixtureId) => {
      router.push(`/fixture/${fixtureId}`);
    });
  }, []);

  if (!loaded || !authHydrated || !preferencesHydrated) {
    return null;
  }

  return <RootLayoutNav />;
}

function RootLayoutNav() {
  const colorScheme = useColorScheme();

  return (
    // REQUIRED for useSafeAreaInsets to report anything — without it every inset reads 0 and
    // screens fall back to hardcoded iPhone numbers, which is exactly how the tab bar ended up
    // underneath Android's navigation buttons.
    <SafeAreaProvider>
      <QueryClientProvider client={queryClient}>
        <AccountThemeSync />
        <ThemeProvider value={colorScheme === "dark" ? DarkTheme : DefaultTheme}>
          <Stack>
            <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
            <Stack.Screen name="fixture/[id]" options={{ title: "Fixture" }} />
            <Stack.Screen name="history/index" options={{ title: "History" }} />
            <Stack.Screen name="how-it-works/index" options={{ title: "How It Works" }} />
            <Stack.Screen
              name="auth/login"
              options={{ title: "Log In", presentation: "modal" }}
            />
            <Stack.Screen
              name="auth/register"
              options={{ title: "Sign Up", presentation: "modal" }}
            />
          </Stack>
        </ThemeProvider>
      </QueryClientProvider>
    </SafeAreaProvider>
  );
}

/** Adopts the account's saved appearance once preferences load.
 *
 * Renders nothing, and lives inside QueryClientProvider because it needs useQuery. It exists
 * so the theme follows the account app-wide rather than only after the user happens to open
 * Profile — the same ["preferences"] query key that screen uses, so this shares its cache
 * rather than issuing a second request.
 *
 * Deliberately additive to the on-device copy, not a replacement: the local value applies
 * instantly at cold start (no light flash while the network resolves) and is the only store a
 * guest has, while the account value is what carries the choice to another device. */
function AccountThemeSync() {
  const accessToken = useAuthStore((s) => s.accessToken);
  const applyFromAccount = useThemeStore((s) => s.applyFromAccount);
  const { data } = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: accessToken !== null,
  });

  useEffect(() => {
    if (data?.theme_preference) applyFromAccount(data.theme_preference);
  }, [data?.theme_preference, applyFromAccount]);

  return null;
}
