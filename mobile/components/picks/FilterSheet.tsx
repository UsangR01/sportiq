import Slider from "@react-native-community/slider";
import { useRef } from "react";
import { Modal, Pressable, ScrollView, Text, View } from "react-native";

import { formatOdds, type OddsFormat } from "@/lib/oddsFormat";
import { CONTROL, RADIUS, SCRIM, SCREEN, TYPE, useTheme } from "@/lib/theme";

/** Sports offered in the sheet, and their sub-competitions.
 *
 * Sub-tours exist only where one Sport row genuinely covers several competitions: a tennis
 * "tour" and basketball's two leagues share a sport_slug, so without this the filter cannot
 * separate ATP from WTA or NBA from WNBA. Football's leagues are many and change often, so it
 * has none here — the country picker is the right instrument for that.
 */
const SPORTS: { label: string; slug: string | null; subs?: { label: string; slug: string }[] }[] = [
  { label: "All", slug: null },
  { label: "Football", slug: "football" },
  { label: "NBA Basketball", slug: "nba", subs: [{ label: "NBA", slug: "nba" }, { label: "WNBA", slug: "wnba" }] },
  { label: "Tennis", slug: "tennis", subs: [{ label: "ATP", slug: "atp" }, { label: "WTA", slug: "wta" }] },
];

export const MIN_PROBABILITY_FLOOR = 0.5;
export const MIN_ODDS_FLOOR = 1.01;
export const MIN_ODDS_CEILING = 3.0;

/** The filter bottom sheet (design spec §3.3). */
export function FilterSheet({
  visible,
  onClose,
  sport,
  subSport,
  onSelectSport,
  minProbability,
  onMinProbabilityChange,
  minOdds,
  onMinOddsChange,
  oddsFormat,
  matchCount,
  onReset,
}: {
  visible: boolean;
  onClose: () => void;
  sport: string | null;
  subSport: string | null;
  onSelectSport: (sport: string | null, subSport: string | null) => void;
  minProbability: number;
  onMinProbabilityChange: (value: number) => void;
  minOdds: number;
  onMinOddsChange: (value: number) => void;
  oddsFormat: OddsFormat;
  matchCount: number;
  onReset: () => void;
}) {
  const { colors, elevation } = useTheme();
  const active = SPORTS.find((entry) => entry.slug === sport);

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={{ flex: 1, backgroundColor: SCRIM }} onPress={onClose} />
      <View
        style={{
          backgroundColor: colors.surface,
          borderTopLeftRadius: RADIUS.sheetTop,
          borderTopRightRadius: RADIUS.sheetTop,
          paddingTop: 14,
          paddingHorizontal: SCREEN.padding,
          paddingBottom: 26,
          ...elevation.sheet,
        }}
      >
        <View
          style={{
            alignSelf: "center",
            width: 34,
            height: 4,
            borderRadius: 2,
            backgroundColor: colors.border,
            marginBottom: 14,
          }}
        />

        <View
          style={{
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 16,
          }}
        >
          <Text style={[TYPE.pick, { fontSize: 17, fontWeight: "800", color: colors.text }]}>
            Filters
          </Text>
          <Pressable onPress={onReset} accessibilityRole="button">
            <Text style={[TYPE.caption, { color: colors.accent, fontWeight: "700" }]}>Reset</Text>
          </Pressable>
        </View>

        <ScrollView style={{ maxHeight: 420 }} showsVerticalScrollIndicator={false}>
          <Section label="Sport">
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
              {SPORTS.map((entry) => (
                <Chip
                  key={entry.label}
                  label={entry.label}
                  selected={entry.slug === sport}
                  onPress={() => onSelectSport(entry.slug, null)}
                />
              ))}
            </View>
          </Section>

          {/* Only for sports that genuinely have sub-competitions, and indented behind a rule so
              it reads as belonging to the chip above rather than as a peer filter. */}
          {active?.subs && (
            <View
              style={{
                borderLeftWidth: 2,
                borderLeftColor: colors.mutedBg,
                paddingLeft: 12,
                marginLeft: 2,
              }}
            >
              <Section label={sport === "tennis" ? "Tour" : "League"}>
                <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                  {active.subs.map((sub) => (
                    <Chip
                      key={sub.slug}
                      label={sub.label}
                      selected={sub.slug === subSport}
                      // Tapping the selected chip clears it — otherwise a sub-tour can only be
                      // escaped by changing sport and coming back.
                      onPress={() =>
                        onSelectSport(sport, sub.slug === subSport ? null : sub.slug)
                      }
                    />
                  ))}
                </View>
              </Section>
            </View>
          )}

          <SliderRow
            label="Minimum probability"
            value={`${Math.round(minProbability * 100)}%`}
            min={MIN_PROBABILITY_FLOOR}
            max={1}
            step={0.01}
            current={minProbability}
            onChange={onMinProbabilityChange}
          />

          <SliderRow
            label="Minimum odds"
            value={formatOdds(minOdds, oddsFormat)}
            min={MIN_ODDS_FLOOR}
            max={MIN_ODDS_CEILING}
            step={0.01}
            current={minOdds}
            onChange={onMinOddsChange}
          />
        </ScrollView>

        <Pressable
          onPress={onClose}
          accessibilityRole="button"
          style={{
            marginTop: 16,
            height: CONTROL.primaryButtonHeight,
            alignItems: "center",
            justifyContent: "center",
            borderRadius: 13,
            backgroundColor: colors.accent,
          }}
        >
          <Text style={[TYPE.pick, { color: "#ffffff", fontWeight: "800" }]}>
            Show {matchCount} {matchCount === 1 ? "match" : "matches"}
          </Text>
        </Pressable>
      </View>
    </Modal>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  const { colors } = useTheme();
  return (
    <View style={{ marginBottom: 18 }}>
      <Text style={[TYPE.eyebrow, { color: colors.textFaint, marginBottom: 9 }]}>{label}</Text>
      {children}
    </View>
  );
}

function Chip({
  label,
  selected,
  onPress,
}: {
  label: string;
  selected: boolean;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected }}
      style={{
        height: CONTROL.chipHeight,
        paddingHorizontal: 14,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: RADIUS.chip,
        backgroundColor: selected ? colors.accent : colors.surface,
        borderWidth: selected ? 0 : 1,
        borderColor: colors.border,
      }}
    >
      <Text
        style={[TYPE.caption, { color: selected ? "#ffffff" : colors.text, fontWeight: "700" }]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function SliderRow({
  label,
  value,
  min,
  max,
  step,
  current,
  onChange,
}: {
  label: string;
  value: string;
  min: number;
  max: number;
  step: number;
  current: number;
  onChange: (value: number) => void;
}) {
  const { colors } = useTheme();
  // The Android SeekBar fires onSlidingComplete once on MOUNT with no touch involved, which
  // silently overwrote a fresh guest's saved minimum odds the first time the tab was opened.
  // A completion with no preceding start is therefore ignored.
  const started = useRef(false);

  return (
    <View style={{ marginBottom: 18 }}>
      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 2,
        }}
      >
        <Text style={[TYPE.eyebrow, { color: colors.textFaint }]}>{label}</Text>
        <Text style={[TYPE.pick, { fontSize: 17, fontWeight: "800", color: colors.text }]}>
          {value}
        </Text>
      </View>
      <Slider
        minimumValue={min}
        maximumValue={max}
        step={step}
        value={current}
        minimumTrackTintColor={colors.accent}
        maximumTrackTintColor={colors.mutedBg}
        thumbTintColor={colors.accent}
        onSlidingStart={() => {
          started.current = true;
        }}
        onSlidingComplete={(next) => {
          if (!started.current) return;
          started.current = false;
          onChange(next);
        }}
      />
    </View>
  );
}
