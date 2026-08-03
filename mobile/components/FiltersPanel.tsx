import Slider from "@react-native-community/slider";
import * as Haptics from "expo-haptics";
import { useRef, useState } from "react";
import { Platform, Pressable, Text, View } from "react-native";

/** Probability/odds filters, collapsed to a one-line summary by default.
 *
 * Both sliders previously sat permanently above the feed, costing roughly a third of the
 * first screen before a single pick was visible. They're adjusted rarely but their VALUES
 * matter constantly — hence a summary line that always states the active thresholds, with
 * the controls themselves a tap away. */
interface Props {
  minProbability: number;
  minProbabilityFloor: number;
  onMinProbabilityChange: (value: number) => void;
  minOdds: number;
  onMinOddsChange: (value: number) => void;
}

export function FiltersPanel({
  minProbability,
  minProbabilityFloor,
  onMinProbabilityChange,
  minOdds,
  onMinOddsChange,
}: Props) {
  const [open, setOpen] = useState(false);
  // @react-native-community/slider's Android SeekBar fires onSlidingComplete once on mount
  // with no real touch, reporting minimumValue — which silently overwrote a fresh guest's
  // saved min_odds. Only commit a completion preceded by a genuine onSlidingStart.
  const startedProbability = useRef(false);
  const startedOdds = useRef(false);

  function commit(value: number, onChange: (v: number) => void) {
    onChange(Math.round(value * 100) / 100);
    if (Platform.OS !== "web") Haptics.selectionAsync();
  }

  return (
    <View className="mx-4 overflow-hidden rounded-2xl bg-gray-100 dark:bg-gray-800">
      <Pressable
        onPress={() => setOpen((o) => !o)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        className="flex-row items-center justify-between px-4 py-3 active:opacity-70"
      >
        <Text className="text-gray-700 dark:text-gray-300">
          Filters{" "}
          <Text className="text-gray-400 dark:text-gray-500">
            · {Math.round(minProbability * 100)}%+ · {minOdds.toFixed(2)}+ odds
          </Text>
        </Text>
        <Text className="text-xs text-gray-500 dark:text-gray-400">{open ? "▲" : "▼"}</Text>
      </Pressable>

      {open && (
        <View className="px-4 pb-3">
          <View className="flex-row items-center justify-between">
            <Text className="text-gray-500 dark:text-gray-400">Minimum probability</Text>
            <Text className="font-bold text-gray-900 dark:text-white">
              {Math.round(minProbability * 100)}%
            </Text>
          </View>
          <Slider
            minimumValue={minProbabilityFloor}
            maximumValue={0.95}
            step={0.01}
            value={minProbability}
            onSlidingStart={() => (startedProbability.current = true)}
            onSlidingComplete={(v) => {
              if (!startedProbability.current) return;
              startedProbability.current = false;
              commit(v, onMinProbabilityChange);
            }}
            minimumTrackTintColor="#2563eb"
          />

          <View className="mt-1 flex-row items-center justify-between">
            <Text className="text-gray-500 dark:text-gray-400">Minimum odds</Text>
            <Text className="font-bold text-gray-900 dark:text-white">{minOdds.toFixed(2)}</Text>
          </View>
          <Slider
            minimumValue={1.01}
            maximumValue={20}
            step={0.01}
            value={minOdds}
            onSlidingStart={() => (startedOdds.current = true)}
            onSlidingComplete={(v) => {
              if (!startedOdds.current) return;
              startedOdds.current = false;
              commit(v, onMinOddsChange);
            }}
            minimumTrackTintColor="#2563eb"
          />
        </View>
      )}
    </View>
  );
}
