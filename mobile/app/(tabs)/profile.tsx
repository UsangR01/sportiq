import { useQuery } from "@tanstack/react-query";
import { router } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, Switch, Text, View } from "react-native";

import { getPreferences, updatePushToken } from "@/lib/api/users";
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
import { ONE_LINE, RADIUS, SCREEN, TYPE, useTheme, useScreenInsets } from "@/lib/theme";
import { useAuthStore } from "@/store/authStore";
import { useThemeStore } from "@/store/themeStore";

/** Profile (design spec §6).
 *
 * Deliberately sparse: an account row, one card of settings, and a way out. §6 rules out a
 * hit-rate card and stat tiles — accuracy belongs where it carries its own sample size and
 * confidence interval, not as a headline number on a settings screen.
 */
export default function ProfileScreen() {
  const { colors, isDark, elevation } = useTheme();
  const insets = useScreenInsets();
  const email = useAuthStore((s) => s.email);
  const accessToken = useAuthStore((s) => s.accessToken);
  const refreshToken = useAuthStore((s) => s.refreshToken);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const setThemePreference = useThemeStore((s) => s.setPreference);
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
    void (async () => {
      setPushEnabled(await isPushNotificationsEnabled());
      setBiometricAvailable(await isBiometricHardwareAvailable());
      setBiometricEnabled(await isBiometricLoginEnabled());
    })();
  }, []);

  async function onTogglePush(next: boolean) {
    setPushBusy(true);
    setPushError(null);
    try {
      if (next) {
        const token = await registerForPushNotificationsAsync();
        await updatePushToken(token);
      } else {
        // Clearing the token server-side matters as much as the local flag: without it the
        // backend keeps sending to a device that has opted out.
        await updatePushToken(null);
      }
      await setPushNotificationsEnabledFlag(next);
      setPushEnabled(next);
    } catch (error) {
      setPushError(
        error instanceof PushRegistrationError
          ? error.message
          : "Couldn't update notifications."
      );
    } finally {
      setPushBusy(false);
    }
  }

  async function onToggleBiometric(next: boolean) {
    setBiometricBusy(true);
    setBiometricError(null);
    try {
      if (next) {
        if (!refreshToken) throw new Error("Sign in again to enable this.");
        await enableBiometricLogin(refreshToken, email ?? "");
      } else {
        await disableBiometricLogin();
      }
      setBiometricEnabled(next);
    } catch (error) {
      setBiometricError(error instanceof Error ? error.message : "Couldn't update biometrics.");
    } finally {
      setBiometricBusy(false);
    }
  }

  return (
    <View
      style={{
        flex: 1,
        backgroundColor: colors.bg,
        paddingTop: insets.top,
        paddingHorizontal: SCREEN.padding,
      }}
    >
      <Text {...ONE_LINE} style={[TYPE.wordmark, { color: colors.text, marginBottom: 22 }]}>
        SportPIQ
      </Text>

      <View style={{ flexDirection: "row", alignItems: "center", gap: 14, marginBottom: 22 }}>
        <View
          style={{
            width: 56,
            height: 56,
            borderRadius: 28,
            alignItems: "center",
            justifyContent: "center",
            backgroundColor: colors.accentSoft,
          }}
        >
          <Text style={{ fontSize: 22, fontWeight: "800", color: colors.accent }}>
            {(email?.[0] ?? "G").toUpperCase()}
          </Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text
            {...ONE_LINE}
            style={[TYPE.pick, { fontSize: 17, fontWeight: "700", color: colors.text }]}
          >
            {email ?? "Guest"}
          </Text>
          <Text {...ONE_LINE} style={[TYPE.body, { color: colors.textSub }]}>
            {isGuest ? "Not signed in" : "Free member"}
          </Text>
        </View>
        {isGuest && (
          <Pressable
            onPress={() => router.push("/auth/register")}
            accessibilityRole="button"
            style={{
              paddingHorizontal: 13,
              paddingVertical: 8,
              borderRadius: RADIUS.chipTight,
              backgroundColor: colors.accentSoft,
            }}
          >
            <Text style={[TYPE.caption, { color: colors.accent, fontWeight: "700" }]}>
              Sign up
            </Text>
          </Pressable>
        )}
      </View>

      <View
        style={{
          backgroundColor: colors.surface,
          borderRadius: RADIUS.control,
          overflow: "hidden",
          ...elevation.card,
        }}
      >
        {/* A two-state toggle, though the underlying preference has three (light/dark/system).
            Flipping it sets an EXPLICIT light or dark, which is what a toggle can honestly
            express; "system" stays reachable from the header's own theme control and survives
            untouched until the user overrides it here. */}
        <SettingRow
          label="Dark mode"
          first
          control={
            <Switch
              value={isDark}
              onValueChange={(next) => setThemePreference(next ? "dark" : "light")}
            />
          }
        />

        {/* The sub-line states the interval the BACKEND actually fires at. The design draft said
            15 minutes; the implemented reminder is T-60, and shipping copy that promises a
            timing the worker does not honour is worse than a less punchy sentence. */}
        <SettingRow
          label="Kick-off alerts"
          sub={isGuest ? "Sign in to use alerts" : "60 min before saved picks"}
          error={pushError}
          control={
            <Switch
              value={pushEnabled}
              disabled={pushBusy || isGuest}
              onValueChange={onTogglePush}
            />
          }
        />

        {biometricAvailable && !isGuest && (
          <SettingRow
            label="Biometric login"
            error={biometricError}
            control={
              <Switch
                value={biometricEnabled}
                disabled={biometricBusy}
                onValueChange={onToggleBiometric}
              />
            }
          />
        )}

        <SettingRow
          label="Preferences & more"
          sub={
            preferencesQuery.data
              ? `Odds shown as ${preferencesQuery.data.odds_format}`
              : undefined
          }
          onPress={() => router.push("/how-it-works")}
          control={<Chevron color={colors.textFaint} />}
        />
      </View>

      {!isGuest && (
        <Pressable
          onPress={() => clearAuth()}
          accessibilityRole="button"
          style={{
            marginTop: 22,
            paddingVertical: 13,
            alignItems: "center",
            borderRadius: RADIUS.button,
            borderWidth: 1,
            borderColor: colors.border,
          }}
        >
          <Text style={[TYPE.pick, { color: colors.fail }]}>Log out</Text>
        </Pressable>
      )}
    </View>
  );
}

function SettingRow({
  label,
  sub,
  error,
  control,
  onPress,
  first,
}: {
  label: string;
  sub?: string;
  error?: string | null;
  control: React.ReactNode;
  onPress?: () => void;
  first?: boolean;
}) {
  const { colors } = useTheme();
  const body = (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: 12,
        paddingHorizontal: 14,
        paddingVertical: 13,
        borderTopWidth: first ? 0 : 1,
        borderTopColor: colors.border,
      }}
    >
      <View style={{ flex: 1 }}>
        <Text {...ONE_LINE} style={[TYPE.pick, { color: colors.text }]}>
          {label}
        </Text>
        {sub && (
          <Text {...ONE_LINE} style={[TYPE.caption, { color: colors.textFaint, fontWeight: "400" }]}>
            {sub}
          </Text>
        )}
        {error && <Text style={[TYPE.caption, { color: colors.fail }]}>{error}</Text>}
      </View>
      {control}
    </View>
  );
  return onPress ? (
    <Pressable onPress={onPress} accessibilityRole="button">
      {body}
    </Pressable>
  ) : (
    body
  );
}

function Chevron({ color }: { color: string }) {
  return (
    <View
      style={{
        width: 8,
        height: 8,
        borderRightWidth: 1.8,
        borderTopWidth: 1.8,
        borderColor: color,
        transform: [{ rotate: "45deg" }],
      }}
    />
  );
}
