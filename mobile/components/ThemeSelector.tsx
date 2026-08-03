import { Pressable, Text, View } from "react-native";

import { useThemeStore, type ThemePreference } from "@/store/themeStore";

const OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

/** Segmented Light/Dark/System control.
 *
 * A segmented control rather than the Switch used by the push/biometric rows: those are
 * genuinely binary, whereas "System" is a real third state and not the absence of a choice.
 * Collapsing it into an on/off toggle would make following the OS unreachable once the user
 * had picked either explicit theme. */
export function ThemeSelector() {
  const preference = useThemeStore((s) => s.preference);
  const setPreference = useThemeStore((s) => s.setPreference);

  return (
    <View>
      <Text className="mb-2 text-gray-700 dark:text-gray-300">Appearance</Text>
      <View className="flex-row rounded-2xl bg-gray-100 p-1 dark:bg-gray-800">
        {OPTIONS.map((option) => {
          const active = preference === option.value;
          return (
            <Pressable
              key={option.value}
              onPress={() => setPreference(option.value)}
              accessibilityRole="radio"
              accessibilityState={{ selected: active }}
              accessibilityLabel={`${option.label} appearance`}
              className={`flex-1 items-center rounded-xl py-2.5 active:opacity-70 ${
                active ? "bg-white shadow-sm dark:bg-gray-600" : ""
              }`}
            >
              <Text
                className={
                  active
                    ? "font-semibold text-gray-900 dark:text-white"
                    : "text-gray-500 dark:text-gray-400"
                }
              >
                {option.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}
