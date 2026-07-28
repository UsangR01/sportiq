import { QueryClientProvider } from "@tanstack/react-query";
import { useFonts } from "expo-font";
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { useEffect } from "react";
import "react-native-reanimated";

import "../global.css";
import { useColorScheme } from "@/components/useColorScheme";
import { queryClient } from "@/lib/queryClient";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

export {
  // Catch any errors thrown by the Layout component.
  ErrorBoundary,
} from "expo-router";

export const unstable_settings = {
  initialRouteName: "(tabs)",
};

SplashScreen.preventAutoHideAsync();

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

  useEffect(() => {
    if (error) throw error;
  }, [error]);

  useEffect(() => {
    hydrateAuth();
    hydratePreferences();
  }, [hydrateAuth, hydratePreferences]);

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

  if (!loaded || !authHydrated || !preferencesHydrated) {
    return null;
  }

  return <RootLayoutNav />;
}

function RootLayoutNav() {
  const colorScheme = useColorScheme();

  return (
    <QueryClientProvider client={queryClient}>
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
  );
}
