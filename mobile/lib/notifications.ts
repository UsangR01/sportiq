import Constants from "expo-constants";
import * as Device from "expo-device";
import { Platform } from "react-native";

import { deleteItem, getItem, setItem } from "@/lib/storage";

const PUSH_ENABLED_FLAG_KEY = "sportiq_push_enabled";

export class PushRegistrationError extends Error {}

/** True specifically for "Expo Go on Android" — the one combination expo-notifications
 * refuses to run under at all (SDK 53+ dropped Expo Go support for Android remote push
 * entirely). Checked BEFORE ever calling require("expo-notifications"): confirmed live that
 * merely requiring the module in this situation throws in a way that still reaches React
 * Native's uncaught-error overlay even from inside a try/catch (the module appears to report
 * it directly to the native error handler, not just via a normal JS exception) — so a
 * try/catch around the require() call alone isn't enough, the call has to be skipped. */
function isExpoGoOnAndroid(): boolean {
  if (Platform.OS !== "android") return false;
  // appOwnership is deprecated and, confirmed live, comes back null on this Expo Go/SDK
  // version — executionEnvironment is the currently-recommended replacement, checked as the
  // primary signal with appOwnership kept as a belt-and-suspenders fallback.
  return (
    Constants.executionEnvironment === "storeClient" || Constants.appOwnership === "expo"
  );
}

function loadNotificationsModule(): typeof import("expo-notifications") | null {
  if (isExpoGoOnAndroid()) return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    return require("expo-notifications");
  } catch {
    return null;
  }
}

let handlerRegistered = false;

function ensureHandlerRegistered(notifications: typeof import("expo-notifications")): void {
  if (handlerRegistered) return;
  notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
  handlerRegistered = true;
}

/** Local-only flag (not the source of truth — the backend's users.expo_push_token column
 * is) so the Profile toggle can render its initial state without an extra round trip. */
export async function isPushNotificationsEnabled(): Promise<boolean> {
  return (await getItem(PUSH_ENABLED_FLAG_KEY)) === "true";
}

export async function setPushNotificationsEnabledFlag(enabled: boolean): Promise<void> {
  if (enabled) {
    await setItem(PUSH_ENABLED_FLAG_KEY, "true");
  } else {
    await deleteItem(PUSH_ENABLED_FLAG_KEY);
  }
}

/** Requests permission and returns a real Expo push token, or throws PushRegistrationError
 * with a message worth showing the user. Two independent things can make this fail on this
 * project today, both pre-existing infrastructure gaps rather than app bugs: (1) Expo Go on
 * Android (SDK 53+) has no remote-push support at all — confirmed live, crashes on import if
 * not loaded lazily; a real development/EAS build is required. (2) even on a build that
 * supports it, minting a token needs a real EAS project id
 * (Constants.expoConfig.extra.eas.projectId), and this project has never been linked to a
 * real Expo/EAS account — same "correct code, no live credential yet" status as
 * RotoWire/BallDontLie injuries (see CLAUDE.md). */
export async function registerForPushNotificationsAsync(): Promise<string> {
  const notifications = loadNotificationsModule();
  if (!notifications) {
    throw new PushRegistrationError(
      "Push notifications aren't available in Expo Go on Android — this needs a development build."
    );
  }
  ensureHandlerRegistered(notifications);

  if (!Device.isDevice) {
    throw new PushRegistrationError(
      "Push notifications need a physical device or emulator with Google Play services — not a plain simulator."
    );
  }

  if (Platform.OS === "android") {
    await notifications.setNotificationChannelAsync("default", {
      name: "default",
      importance: notifications.AndroidImportance.DEFAULT,
    });
  }

  const existing = await notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== "granted") {
    const requested = await notifications.requestPermissionsAsync();
    status = requested.status;
  }
  if (status !== "granted") {
    throw new PushRegistrationError("Notification permission was denied.");
  }

  const projectId = Constants.expoConfig?.extra?.eas?.projectId;
  if (!projectId) {
    throw new PushRegistrationError(
      "No EAS project is linked yet, so Expo can't mint a push token for this build."
    );
  }

  const { data } = await notifications.getExpoPushTokenAsync({ projectId });
  return data;
}

/** Safe to call unconditionally at app startup — a no-op (not a throw) when the module
 * can't be loaded (see loadNotificationsModule's docstring). */
export function addNotificationTapListener(
  onFixtureId: (fixtureId: string) => void
): () => void {
  const notifications = loadNotificationsModule();
  if (!notifications) return () => {};

  const subscription = notifications.addNotificationResponseReceivedListener((response) => {
    const fixtureId = response.notification.request.content.data?.fixture_id;
    if (typeof fixtureId === "string") {
      onFixtureId(fixtureId);
    }
  });
  return () => subscription.remove();
}
