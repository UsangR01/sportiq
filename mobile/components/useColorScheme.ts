import { useColorScheme as useNativeWindColorScheme } from "nativewind";

/** The app's effective light/dark scheme.
 *
 * Reads NativeWind's scheme rather than React Native's own, so that React Navigation's
 * ThemeProvider and every `dark:` class resolve from one source. That matters now the user
 * can override the OS setting in Profile (see store/themeStore.ts) — reading RN's hook
 * directly would leave navigation chrome on the OS theme while the rest of the app followed
 * the override. Falls back to light before NativeWind has resolved a scheme. */
export const useColorScheme = () => useNativeWindColorScheme().colorScheme ?? "light";
