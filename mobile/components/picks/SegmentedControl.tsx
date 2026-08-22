import { Pressable, Text, View } from "react-native";

import { RADIUS, TYPE, useTheme } from "@/lib/theme";

/** All / Upcoming / Finished (design spec §3.1 row 4).
 *
 * The active segment LIFTS to `surface` with the card shadow while the track stays
 * `surfaceAlt`, so the selection reads as raised rather than merely tinted — the same
 * treatment the platform segmented controls use, and the reason it is legible at a glance in
 * both themes without relying on colour alone.
 */
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
}: {
  options: readonly T[];
  value: T;
  onChange: (value: T) => void;
}) {
  const { colors, elevation } = useTheme();

  return (
    <View
      style={{
        flexDirection: "row",
        backgroundColor: colors.surfaceAlt,
        borderRadius: RADIUS.button,
        padding: 3,
      }}
    >
      {options.map((option) => {
        const selected = option === value;
        return (
          <Pressable
            key={option}
            onPress={() => onChange(option)}
            accessibilityRole="tab"
            accessibilityState={{ selected }}
            style={{
              flex: 1,
              alignItems: "center",
              justifyContent: "center",
              paddingVertical: 7,
              borderRadius: RADIUS.icon,
              backgroundColor: selected ? colors.surface : "transparent",
              ...(selected ? elevation.card : null),
            }}
          >
            <Text
              numberOfLines={1}
              style={[
                TYPE.caption,
                { color: selected ? colors.text : colors.textSub, fontWeight: selected ? "700" : "600" },
              ]}
            >
              {option}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}
