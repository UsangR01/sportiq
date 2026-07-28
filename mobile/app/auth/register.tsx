import { router } from "expo-router";
import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { ApiError } from "@/lib/api/client";
import { useAuthStore } from "@/store/authStore";
import { usePreferencesStore } from "@/store/preferencesStore";

export default function RegisterScreen() {
  const register = useAuthStore((s) => s.register);
  const guestSessionId = usePreferencesStore((s) => s.guestSessionId);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = email.length > 0 && password.length >= 8;

  async function onSubmit() {
    setError(null);
    setSubmitting(true);
    try {
      // Migrates this device's guest filter state (sport/min-odds/odds-format) into the new
      // account, per TDD §2.1's guest-session design.
      await register(email.trim(), password, guestSessionId);
      router.replace("/(tabs)/profile");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <View className="flex-1 bg-white px-6 pt-8 dark:bg-black">
      <Text className="mb-6 text-2xl font-bold text-gray-900 dark:text-gray-100">Sign Up</Text>

      <TextInput
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        placeholder="Email"
        placeholderTextColor="#9ca3af"
        value={email}
        onChangeText={setEmail}
        className="mb-3 rounded-lg border border-gray-300 px-4 py-3 text-gray-900 dark:border-gray-700 dark:text-gray-100"
      />
      <TextInput
        autoCapitalize="none"
        autoComplete="password-new"
        secureTextEntry
        placeholder="Password (min. 8 characters)"
        placeholderTextColor="#9ca3af"
        value={password}
        onChangeText={setPassword}
        className="mb-4 rounded-lg border border-gray-300 px-4 py-3 text-gray-900 dark:border-gray-700 dark:text-gray-100"
      />

      {error && <Text className="mb-4 text-sm text-red-500">{error}</Text>}

      <Pressable
        disabled={submitting || !canSubmit}
        onPress={onSubmit}
        className={`rounded-lg bg-blue-600 py-3 ${!canSubmit ? "opacity-50" : ""}`}
      >
        <Text className="text-center font-semibold text-white">
          {submitting ? "Creating account…" : "Sign Up"}
        </Text>
      </Pressable>

      <Pressable className="mt-4" onPress={() => router.replace("/auth/login")}>
        <Text className="text-center text-sm text-blue-600">
          Already have an account? Log in
        </Text>
      </Pressable>
    </View>
  );
}
