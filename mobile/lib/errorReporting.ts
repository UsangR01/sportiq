import Constants from "expo-constants";
import * as Sentry from "@sentry/react-native";

/**
 * Crash and error reporting for the app.
 *
 * The backend has had Sentry since the beginning and the mobile app has had NONE, so every
 * crash a real user hit was invisible — the only ones ever seen were the ones reproduced by
 * hand on a dev machine. The DSN for this project ("sportpiq-mobile") existed in keys.docx the
 * whole time; nothing was ever wired to it.
 *
 * WHY THERE IS NO EXPO-GO GUARD HERE, unlike lib/notifications.ts
 *
 * expo-notifications throws on IMPORT under Expo Go on Android, in a way that reaches React
 * Native's uncaught-error overlay even from inside a try/catch — so that module has to be
 * skipped entirely rather than caught. @sentry/react-native does not behave that way: it
 * detects Expo Go itself (utils/environment.ts:isExpoGo) and degrades, logging "Offline
 * caching, native errors features are not available in Expo Go" while still capturing JS
 * errors. Verified by reading the installed SDK rather than assumed, because getting this
 * wrong is what took the whole app down last time.
 *
 * The practical consequence is worth stating plainly: under Expo Go this reports JS errors
 * only. NATIVE crashes need a development or EAS build, and this project has never set one up.
 */

/** EXPO_PUBLIC_* is inlined into the bundle at build time, which is how the DSN reaches the
 * client. That is correct and intended for Sentry: a DSN is write-only — it can submit events
 * to this one project and cannot read anything — which is why client DSNs ship inside every
 * web and mobile app that uses them. It is not a secret in the sense the API keys are. */
const DSN = process.env.EXPO_PUBLIC_SENTRY_DSN ?? "";

let initialised = false;

export function initErrorReporting(): boolean {
  if (initialised) return true;
  // A missing DSN is a supported state — a fresh clone has no mobile/.env — so this returns
  // quietly rather than warning on every app start.
  if (!DSN) return false;

  try {
    Sentry.init({
      dsn: DSN,
      // Separates real user crashes from ones produced while developing, so the issue stream
      // stays readable. Both are still sent: a dev-only filter would mean the wiring could
      // silently rot and nobody would find out until a production crash went missing.
      environment: __DEV__ ? "development" : "production",
      // Off by default. Performance tracing multiplies event volume, and nothing here needs it
      // yet — the question this answers is "did the app crash", not "was it slow".
      tracesSampleRate: 0,
      // Never send the DSN-bearing request body or headers of our own API calls.
      sendDefaultPii: false,
    });
    Sentry.setTag("component", "mobile");
    const version = Constants.expoConfig?.version;
    if (version) Sentry.setTag("app_version", version);
    initialised = true;
    return true;
  } catch {
    // A reporter that can stop the app starting is worse than the blindness it fixes — the
    // same rule the backend's init_sentry follows.
    return false;
  }
}

/** Report a handled error explicitly. Unhandled ones are captured automatically once
 * initErrorReporting has run. */
export function reportError(error: unknown, context?: Record<string, unknown>): void {
  if (!initialised) return;
  Sentry.captureException(error, context ? { extra: context } : undefined);
}

export function isErrorReportingEnabled(): boolean {
  return initialised;
}
