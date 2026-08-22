import { useState } from "react";
import { Modal, Pressable, ScrollView, Text, View } from "react-native";

import { ONE_LINE, RADIUS, SCREEN, TYPE, useTheme } from "@/lib/theme";

export interface CountryOption {
  /** Display name, e.g. "Scotland". */
  name: string;
  /** Short code for the badge, e.g. "SCO". */
  code: string;
  /** How many of the day's matches sit in this country. */
  count: number;
}

/** The three-column summary strip (design spec §3.1 row 3).
 *
 * The third column is a CONTROL, not a statistic — it opens the country picker. That is easy
 * to miss, so it carries the accent colour and a chevron while the other two do not.
 */
export function SummaryStrip({
  callCount,
  leagueCount,
  country,
  countries,
  onSelectCountry,
}: {
  callCount: number;
  leagueCount: number;
  country: string | null;
  countries: CountryOption[];
  onSelectCountry: (country: string | null) => void;
}) {
  const { colors, elevation } = useTheme();
  const [open, setOpen] = useState(false);

  return (
    <>
      <View
        style={{
          flexDirection: "row",
          backgroundColor: colors.surface,
          borderRadius: RADIUS.control,
          paddingVertical: 13,
          paddingHorizontal: 4,
          ...elevation.card,
        }}
      >
        <Column label="Calls today" value={String(callCount)} />
        <Column label="Leagues" value={String(leagueCount)} divided />
        <Column
          label="Country"
          value={country ?? "All"}
          divided
          interactive
          onPress={() => setOpen(true)}
        />
      </View>

      <CountryPicker
        visible={open}
        onClose={() => setOpen(false)}
        selected={country}
        countries={countries}
        onSelect={(next) => {
          onSelectCountry(next);
          setOpen(false);
        }}
      />
    </>
  );
}

function Column({
  label,
  value,
  divided,
  interactive,
  onPress,
}: {
  label: string;
  value: string;
  divided?: boolean;
  interactive?: boolean;
  onPress?: () => void;
}) {
  const { colors } = useTheme();
  const body = (
    <View style={{ alignItems: "center", paddingHorizontal: 6 }}>
      {/* Fixed height so all three eyebrows sit on one baseline even when one wraps to two
          words — otherwise the values below them land at different heights. */}
      <View style={{ height: 24, justifyContent: "center" }}>
        <Text {...ONE_LINE} style={[TYPE.eyebrow, { color: colors.textFaint }]}>
          {label}
        </Text>
      </View>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 3 }}>
        <Text
          {...ONE_LINE}
          style={[
            TYPE.summaryValue,
            // Accent signals "this one does something"; the selected country also reads as a
            // filter that is currently ON.
            { color: interactive && value !== "All" ? colors.accent : colors.text },
          ]}
        >
          {value}
        </Text>
        {interactive && <Chevron color={value !== "All" ? colors.accent : colors.textFaint} />}
      </View>
    </View>
  );

  return (
    <View
      style={{
        flex: 1,
        borderLeftWidth: divided ? 1 : 0,
        // The first divider is transparent rather than absent so all three columns keep the
        // same inner width and the values stay evenly spaced.
        borderLeftColor: divided ? colors.border : "transparent",
      }}
    >
      {interactive ? (
        <Pressable onPress={onPress} accessibilityRole="button">
          {body}
        </Pressable>
      ) : (
        body
      )}
    </View>
  );
}

function Chevron({ color }: { color: string }) {
  return (
    <View
      style={{
        width: 6,
        height: 6,
        borderRightWidth: 1.6,
        borderBottomWidth: 1.6,
        borderColor: color,
        transform: [{ rotate: "45deg" }, { translateY: -1 }],
      }}
    />
  );
}

/** The country popover.
 *
 * A Modal rather than an absolutely-positioned View: the strip sits inside a fixed header, and
 * an absolute child would be clipped by it on Android. The list is built from the DAY'S OWN
 * data by the caller, never hardcoded, and the selection cuts across sports (§3.1).
 */
function CountryPicker({
  visible,
  onClose,
  selected,
  countries,
  onSelect,
}: {
  visible: boolean;
  onClose: () => void;
  selected: string | null;
  countries: CountryOption[];
  onSelect: (country: string | null) => void;
}) {
  const { colors, elevation } = useTheme();

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable style={{ flex: 1 }} onPress={onClose}>
        <View
          style={{
            position: "absolute",
            top: 210,
            right: SCREEN.padding,
            minWidth: 210,
            maxHeight: 360,
            backgroundColor: colors.surface,
            borderRadius: RADIUS.control,
            overflow: "hidden",
            ...elevation.dropdown,
          }}
        >
          <ScrollView>
            <CountryRow
              label="All countries"
              selected={selected === null}
              onPress={() => onSelect(null)}
              first
            />
            {countries.map((entry) => (
              <CountryRow
                key={entry.name}
                label={entry.name}
                code={entry.code}
                count={entry.count}
                selected={selected === entry.name}
                onPress={() => onSelect(entry.name)}
              />
            ))}
          </ScrollView>
        </View>
      </Pressable>
    </Modal>
  );
}

function CountryRow({
  label,
  code,
  count,
  selected,
  onPress,
  first,
}: {
  label: string;
  code?: string;
  count?: number;
  selected: boolean;
  onPress: () => void;
  first?: boolean;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected }}
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: 8,
        paddingHorizontal: 12,
        paddingVertical: 11,
        borderTopWidth: first ? 0 : 1,
        borderTopColor: colors.border,
        backgroundColor: selected ? colors.accentSoft : "transparent",
      }}
    >
      {code ? (
        <View
          style={{
            minWidth: 26,
            alignItems: "center",
            paddingHorizontal: 5,
            paddingVertical: 3,
            borderRadius: RADIUS.badge,
            backgroundColor: colors.surfaceAlt,
          }}
        >
          {/* A letter code, not a flag — the ONE place the design prefers it, since a code
              beside the full country name is clearer at this size than a 20px image. */}
          <Text style={[TYPE.eyebrowSmall, { color: colors.textSub }]}>{code}</Text>
        </View>
      ) : null}
      <Text
        {...ONE_LINE}
        style={[TYPE.team, { flex: 1, color: selected ? colors.accent : colors.text }]}
      >
        {label}
      </Text>
      {count != null && (
        <Text style={[TYPE.caption, { color: colors.textFaint }]}>{count}</Text>
      )}
    </Pressable>
  );
}
