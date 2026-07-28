import { useQuery } from "@tanstack/react-query";
import { Link } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, Switch, Text, View } from "react-native";

import {
  disableBiometricLogin,
  enableBiometricLogin,
  isBiometricHardwareAvailable,
  isBiometricLoginEnabled,
} from "@/lib/biometricAuth";
import {
  isPushNotificationsEnabled,
  PushRegistrationError,
  registerForPushNotificationsAsync,
  setPushNotificationsEnabledFlag,
} from "@/lib/notifications";
import { getPreferences, updatePushToken } from "@/lib/api/users";
import { useAuthStore } from "@/store/authStore";

export default function ProfileScreen() {
  const email = useAuthStore((s) => s.email);
  const accessToken = useAuthStore((s) => s.accessToken);
  const refreshToken = useAuthStore((s) => s.refreshToken);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const isGuest = accessToken === null;

  const preferencesQuery = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: !isGuest,
  });

  const [pushEnabled, setPushEnabled] = useState(false);
  const [pushBusy, setPushBusy] = useState(false);
  const [pushError, setPushError] = useState<string | null>(null);

  const [biometricAvailable, setBiometricAvailable] = useState(false);
  const [biometricEnabled, setBiometricEnabled] = useState(false);
  const [biometricBusy, setBiometricBusy] = useState(false);
  const [biometricError, setBiometricError] = useState<string | null>(null);

  useEffect(() => {
    if (isGuest) return;
    isPushNotificationsEnabled().then(setPushEnabled);
    isBiometricHardwareAvailable().then(setBiometricAvailable);
    isBiometricLoginEnabled().then(setBiometricEnabled);
  }, [isGuest]);

  async function onTogglePush(next: boolean) {
    setPushError(null);
    setPushBusy(true);
    try {
      if (next) {
        const token = await registerForPushNotificationsAsync();
        await updatePushToken(token);
        await setPushNotificationsEnabledFlag(true);
        setPushEnabled(true);
      } else {
        await updatePushToken(null);
        await setPushNotificationsEnabledFlag(false);
        setPushEnabled(false);
      }
    } catch (e) {
      setPushError(
        e instanceof PushRegistrationError ? e.message : "Couldn't update push notifications."
      );
    } finally {
      setPushBusy(false);
    }
  }

  async function onToggleBiometric(next: boolean) {
    setBiometricError(null);
    setBiometricBusy(true);
    try {
      if (next) {
        if (!refreshToken || !email) throw new Error("Log in again before enabling this.");
        await enableBiometricLogin(refreshToken, email);
        setBiometricEnabled(true);
      } else {
        await disableBiometricLogin();
        setBiometricEnabled(false);
      }
    } catch (e) {
      setBiometricError(e instanceof Error ? e.message : "Couldn't update biometric login.");
    } finally {
      setBiometricBusy(false);
    }
  }

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

      <View className="mb-6">
        <ToggleRow
          label="Push notifications"
          value={pushEnabled}
          busy={pushBusy}
          onValueChange={onTogglePush}
        />
        {pushError && <Text className="mt-1 text-xs text-red-500">{pushError}</Text>}

        {biometricAvailable && (
          <>
            <ToggleRow
              label="Biometric login"
              value={biometricEnabled}
              busy={biometricBusy}
              onValueChange={onToggleBiometric}
            />
            {biometricError && (
              <Text className="mt-1 text-xs text-red-500">{biometricError}</Text>
            )}
          </>
        )}
      </View>

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

function ToggleRow({
  label,
  value,
  busy,
  onValueChange,
}: {
  label: string;
  value: boolean;
  busy: boolean;
  onValueChange: (next: boolean) => void;
}) {
  return (
    <View className="flex-row items-center justify-between border-b border-gray-100 py-2 dark:border-gray-800">
      <Text className="text-gray-700 dark:text-gray-300">{label}</Text>
      <Switch value={value} disabled={busy} onValueChange={onValueChange} />
    </View>
  );
}
