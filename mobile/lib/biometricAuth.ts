import * as LocalAuthentication from "expo-local-authentication";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

// A separate keychainService from the plain token storage in lib/tokenStore.ts —
// SecureStore's own docs warn that requireAuthentication doesn't mix well with
// non-authenticated items under the same keychainService.
const BIOMETRIC_KEYCHAIN_SERVICE = "sportiq-biometric";
const BIOMETRIC_REFRESH_TOKEN_KEY = "sportiq_biometric_refresh_token";
const BIOMETRIC_ENABLED_FLAG_KEY = "sportiq_biometric_enabled";
// Plain (non-gated) — email isn't sensitive, and the login screen wants to show "Log in as
// x@y.com" without forcing a biometric prompt just to render a label.
const BIOMETRIC_EMAIL_KEY = "sportiq_biometric_email";

export async function isBiometricHardwareAvailable(): Promise<boolean> {
  if (Platform.OS === "web") return false;
  const [hasHardware, isEnrolled] = await Promise.all([
    LocalAuthentication.hasHardwareAsync(),
    LocalAuthentication.isEnrolledAsync(),
  ]);
  return hasHardware && isEnrolled;
}

export async function isBiometricLoginEnabled(): Promise<boolean> {
  if (Platform.OS === "web") return false;
  const flag = await SecureStore.getItemAsync(BIOMETRIC_ENABLED_FLAG_KEY);
  return flag === "true";
}

/** Writes the refresh token into the biometric-gated slot without re-prompting — used both
 * by enableBiometricLogin (right after an explicit authenticateAsync() confirmation) and
 * after every biometric login (the backend rotates refresh tokens on every /auth/refresh
 * call, so the stored one goes stale the instant it's used unless re-synced). On Android,
 * requireAuthentication demands a fresh prompt for writes too, not just reads — so this
 * still surfaces a second prompt right after login there; there's no way around that given
 * the backend's rotation policy without storing something less sensitive than the real
 * refresh token instead. */
export async function storeBiometricRefreshToken(refreshToken: string, email: string): Promise<void> {
  await SecureStore.setItemAsync(BIOMETRIC_REFRESH_TOKEN_KEY, refreshToken, {
    keychainService: BIOMETRIC_KEYCHAIN_SERVICE,
    requireAuthentication: true,
  });
  await SecureStore.setItemAsync(BIOMETRIC_ENABLED_FLAG_KEY, "true");
  await SecureStore.setItemAsync(BIOMETRIC_EMAIL_KEY, email);
}

/** Confirms the user's identity once via expo-local-authentication, then stores the
 * current refresh token behind a biometric-gated SecureStore entry (requireAuthentication)
 * for next time. */
export async function enableBiometricLogin(refreshToken: string, email: string): Promise<void> {
  const result = await LocalAuthentication.authenticateAsync({
    promptMessage: "Confirm it's you to enable biometric login",
  });
  if (!result.success) {
    throw new Error("Biometric confirmation was cancelled or failed.");
  }
  await storeBiometricRefreshToken(refreshToken, email);
}

export async function disableBiometricLogin(): Promise<void> {
  await SecureStore.deleteItemAsync(BIOMETRIC_REFRESH_TOKEN_KEY, {
    keychainService: BIOMETRIC_KEYCHAIN_SERVICE,
  });
  await SecureStore.deleteItemAsync(BIOMETRIC_ENABLED_FLAG_KEY);
  await SecureStore.deleteItemAsync(BIOMETRIC_EMAIL_KEY);
}

export async function getBiometricEmail(): Promise<string | null> {
  return SecureStore.getItemAsync(BIOMETRIC_EMAIL_KEY);
}

/** Reading a requireAuthentication entry itself triggers the native biometric prompt — no
 * separate authenticateAsync() call needed here. Returns null if the user isn't enrolled,
 * cancels, or never enabled biometric login. */
export async function getBiometricRefreshToken(): Promise<string | null> {
  return SecureStore.getItemAsync(BIOMETRIC_REFRESH_TOKEN_KEY, {
    keychainService: BIOMETRIC_KEYCHAIN_SERVICE,
    requireAuthentication: true,
  });
}
