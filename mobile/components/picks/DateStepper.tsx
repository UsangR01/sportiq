import { useMemo, useState } from "react";
import { Modal, Pressable, Text, View } from "react-native";

import { CONTROL, ONE_LINE, RADIUS, SCREEN, TYPE, useTheme } from "@/lib/theme";

/** Midnight local, so two dates compare by DAY rather than by instant. Every date decision on
 * this screen is "which day am I looking at", and an unnormalised Date carries a time that
 * makes today !== today. */
export function startOfDay(date: Date): Date {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy;
}

function isSameDay(a: Date, b: Date): boolean {
  return startOfDay(a).getTime() === startOfDay(b).getTime();
}

function addDays(date: Date, days: number): Date {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + days);
  return copy;
}

/** ‹ · Today | Thu, Jul 31 · › (design spec §3.1 row 2). */
export function DateStepper({
  selected,
  onSelect,
  /** Days that actually have fixtures, so the calendar can dot them. Local ISO (YYYY-MM-DD). */
  daysWithFixtures = [],
}: {
  selected: Date;
  onSelect: (date: Date) => void;
  daysWithFixtures?: string[];
}) {
  const { colors } = useTheme();
  const [open, setOpen] = useState(false);
  const today = isSameDay(selected, new Date());

  return (
    <>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Arrow direction="left" onPress={() => onSelect(addDays(selected, -1))} />
        <Pressable
          onPress={() => setOpen(true)}
          accessibilityRole="button"
          style={{
            flex: 1,
            height: CONTROL.dateArrow,
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            backgroundColor: colors.surface,
            borderWidth: 1,
            borderColor: colors.border,
            borderRadius: RADIUS.control,
          }}
        >
          <Text {...ONE_LINE} style={[TYPE.pick, { color: colors.text }]}>
            {today
              ? "Today"
              : selected.toLocaleDateString(undefined, {
                  weekday: "short",
                  month: "short",
                  day: "numeric",
                })}
          </Text>
          <Caret color={colors.textSub} />
        </Pressable>
        <Arrow direction="right" onPress={() => onSelect(addDays(selected, 1))} />
      </View>

      <CalendarPopover
        visible={open}
        onClose={() => setOpen(false)}
        selected={selected}
        daysWithFixtures={daysWithFixtures}
        onSelect={(date) => {
          onSelect(date);
          setOpen(false);
        }}
      />
    </>
  );
}

function Arrow({ direction, onPress }: { direction: "left" | "right"; onPress: () => void }) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={direction === "left" ? "Previous day" : "Next day"}
      style={{
        width: CONTROL.dateArrow,
        height: CONTROL.dateArrow,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: colors.surface,
        borderWidth: 1,
        borderColor: colors.border,
        borderRadius: RADIUS.control,
      }}
    >
      <View
        style={{
          width: 8,
          height: 8,
          borderLeftWidth: 1.8,
          borderBottomWidth: 1.8,
          borderColor: colors.textSub,
          transform: [{ rotate: direction === "left" ? "45deg" : "-135deg" }],
          // The chevron's visual centre sits off its bounding box's centre; nudge it back so
          // the glyph looks centred in the button rather than measuring as centred.
          marginLeft: direction === "left" ? 3 : -3,
        }}
      />
    </Pressable>
  );
}

function Caret({ color }: { color: string }) {
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

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

/** Local YYYY-MM-DD. Deliberately not toISOString(), which converts to UTC first and lands on
 * the wrong day for anyone east or west of it near midnight. */
function localKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function CalendarPopover({
  visible,
  onClose,
  selected,
  daysWithFixtures,
  onSelect,
}: {
  visible: boolean;
  onClose: () => void;
  selected: Date;
  daysWithFixtures: string[];
  onSelect: (date: Date) => void;
}) {
  const { colors, elevation } = useTheme();
  const [month, setMonth] = useState(() => new Date(selected.getFullYear(), selected.getMonth(), 1));
  const withFixtures = useMemo(() => new Set(daysWithFixtures), [daysWithFixtures]);

  const cells = useMemo(() => {
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    // Monday-first, so shift Sunday (0) to the end of the week rather than the start.
    const leading = (first.getDay() + 6) % 7;
    const daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    const out: (Date | null)[] = Array(leading).fill(null);
    for (let day = 1; day <= daysInMonth; day += 1) {
      out.push(new Date(month.getFullYear(), month.getMonth(), day));
    }
    return out;
  }, [month]);

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable style={{ flex: 1 }} onPress={onClose}>
        <View
          style={{
            position: "absolute",
            top: 150,
            left: SCREEN.padding,
            right: SCREEN.padding,
            backgroundColor: colors.surface,
            borderRadius: 16,
            padding: 14,
            ...elevation.dropdown,
          }}
        >
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <MonthArrow
              direction="left"
              onPress={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
            />
            <Text style={[TYPE.pick, { color: colors.text }]}>
              {month.toLocaleDateString(undefined, { month: "long", year: "numeric" })}
            </Text>
            <MonthArrow
              direction="right"
              onPress={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
            />
          </View>

          <View style={{ flexDirection: "row", marginBottom: 4 }}>
            {WEEKDAYS.map((day) => (
              <Text
                key={day}
                style={[
                  TYPE.caption,
                  { flex: 1, textAlign: "center", color: colors.textFaint, fontWeight: "700" },
                ]}
              >
                {day}
              </Text>
            ))}
          </View>

          <View style={{ flexDirection: "row", flexWrap: "wrap" }}>
            {cells.map((date, index) => {
              if (!date) return <View key={`pad-${index}`} style={{ width: "14.28%", height: 40 }} />;
              const isSelected = isSameDay(date, selected);
              const hasFixtures = withFixtures.has(localKey(date));
              return (
                <Pressable
                  key={localKey(date)}
                  onPress={() => onSelect(date)}
                  accessibilityRole="button"
                  style={{ width: "14.28%", height: 40, alignItems: "center", justifyContent: "center" }}
                >
                  <View
                    style={{
                      width: 34,
                      height: 34,
                      alignItems: "center",
                      justifyContent: "center",
                      borderRadius: RADIUS.chipTight,
                      backgroundColor: isSelected ? colors.accent : "transparent",
                    }}
                  >
                    <Text
                      style={[
                        TYPE.caption,
                        {
                          color: isSelected
                            ? "#ffffff"
                            : hasFixtures
                              ? colors.text
                              : colors.textFaint,
                          fontWeight: isSelected ? "800" : hasFixtures ? "700" : "500",
                        },
                      ]}
                    >
                      {date.getDate()}
                    </Text>
                    {/* A day with fixtures is worth marking even when it is not selected —
                        otherwise finding one means stepping through empty days one at a time. */}
                    {hasFixtures && !isSelected && (
                      <View
                        style={{
                          position: "absolute",
                          bottom: 3,
                          width: 3,
                          height: 3,
                          borderRadius: 1.5,
                          backgroundColor: colors.accent,
                        }}
                      />
                    )}
                  </View>
                </Pressable>
              );
            })}
          </View>

          <Pressable
            onPress={() => onSelect(new Date())}
            accessibilityRole="button"
            style={{
              marginTop: 10,
              paddingVertical: 11,
              alignItems: "center",
              borderRadius: RADIUS.button,
              backgroundColor: colors.accent,
            }}
          >
            <Text style={[TYPE.eyebrow, { color: "#ffffff" }]}>Today</Text>
          </Pressable>
        </View>
      </Pressable>
    </Modal>
  );
}

function MonthArrow({ direction, onPress }: { direction: "left" | "right"; onPress: () => void }) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={direction === "left" ? "Previous month" : "Next month"}
      style={{
        width: CONTROL.calendarArrow,
        height: CONTROL.calendarArrow,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <View
        style={{
          width: 7,
          height: 7,
          borderLeftWidth: 1.6,
          borderBottomWidth: 1.6,
          borderColor: colors.textSub,
          transform: [{ rotate: direction === "left" ? "45deg" : "-135deg" }],
          marginLeft: direction === "left" ? 2 : -2,
        }}
      />
    </Pressable>
  );
}
