import { router } from "expo-router";
import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { ApiError } from "@/lib/api/client";
import { useAuthStore } from "@/store/authStore";

export default function LoginScreen() {
  const login = useAuthStore((s) => s.login);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit() {
    setError(null);
    setSubmitting(true);
    try {
      await login(email.trim(), password);
      router.replace("/(tabs)/profile");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <View className="flex-1 bg-white px-6 pt-8 dark:bg-black">
      <Text className="mb-6 text-2xl font-bold text-gray-900 dark:text-gray-100">Log In</Text>

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
        autoComplete="password"
        secureTextEntry
        placeholder="Password"
        placeholderTextColor="#9ca3af"
        value={password}
        onChangeText={setPassword}
        className="mb-4 rounded-lg border border-gray-300 px-4 py-3 text-gray-900 dark:border-gray-700 dark:text-gray-100"
      />

      {error && <Text className="mb-4 text-sm text-red-500">{error}</Text>}

      <Pressable
        disabled={submitting || !email || !password}
        onPress={onSubmit}
        className={`rounded-lg bg-blue-600 py-3 ${!email || !password ? "opacity-50" : ""}`}
      >
        <Text className="text-center font-semibold text-white">
          {submitting ? "Logging in…" : "Log In"}
        </Text>
      </Pressable>

      <Pressable className="mt-4" onPress={() => router.replace("/auth/register")}>
        <Text className="text-center text-sm text-blue-600">
          No account? Sign up instead
        </Text>
      </Pressable>
    </View>
  );
}
