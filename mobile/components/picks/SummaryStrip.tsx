import { useState } from "react";
import { Modal, Pressable, ScrollView, Text, View } from "react-native";

import { CountryFlag } from "@/lib/countryFlags";
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
        {/* This column is always an IMAGE: the selected country's flag, or the globe for no
            filter. Country names vary wildly in length — "USA" against "Czech-Republic" — so a
            name either truncates or drags the three columns to different widths, and a flag is
            recognised faster than a word at this size. Using the globe rather than the word
            "All" also keeps the column the same shape whether the filter is set or cleared. */}
        <Column
          label="Country"
          value={country ?? "All"}
          flag={country}
          showFlag
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
  flag,
  showFlag,
  divided,
  interactive,
  onPress,
}: {
  label: string;
  value: string;
  /** When set, the column shows this country's flag in place of the value text. */
  flag?: string | null;
  /** Render an image even when `flag` is null — the globe, meaning "every country". */
  showFlag?: boolean;
  divided?: boolean;
  interactive?: boolean;
  onPress?: () => void;
}) {
  const { colors } = useTheme();
  const active = interactive && value !== "All";
  const body = (
    <View style={{ alignItems: "center", paddingHorizontal: 6 }}>
      {/* Fixed height so all three eyebrows sit on one baseline even when one wraps to two
          words — otherwise the values below them land at different heights. */}
      <View style={{ height: 24, justifyContent: "center" }}>
        <Text {...ONE_LINE} style={[TYPE.eyebrow, { color: colors.textFaint }]}>
          {label}
        </Text>
      </View>
      {/* Fixed height on the value row too: a 22px flag and a 20px numeral have different
          natural heights, and without this the three columns' baselines drift apart the moment
          a country is selected. */}
      <View
        style={{
          height: 25,
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: 3,
        }}
      >
        {showFlag ? (
          // A country shows its flag; "no filter" shows the globe CountryFlag already falls
          // back to. Keeping both as images means the column never changes shape when the
          // filter is set or cleared, which a word-then-flag swap would do.
          <CountryFlag country={flag ?? null} size={22} />
        ) : (
          <Text
            {...ONE_LINE}
            style={[
              TYPE.summaryValue,
              // Accent signals "this one does something"; a selected country also reads as a
              // filter that is currently ON.
              { color: active ? colors.accent : colors.text },
            ]}
          >
            {value}
          </Text>
        )}
        {interactive && <Chevron color={active ? colors.accent : colors.textFaint} />}
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
              // No country name to map, so CountryFlag renders its globe — the same mark the
              // strip shows when the filter is off, so the two read as the same state.
              globe
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
  globe,
}: {
  label: string;
  code?: string;
  count?: number;
  selected: boolean;
  onPress: () => void;
  first?: boolean;
  globe?: boolean;
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
      {/* Flags here too, matching the strip above. The full country name sits beside it, so the
          flag is a fast visual key rather than the only identifier — which is what makes it
          safe to prefer over a letter code even for countries whose flags look alike. */}
      {globe ? (
        <CountryFlag country={null} size={20} />
      ) : code ? (
        <CountryFlag country={label} size={20} />
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
