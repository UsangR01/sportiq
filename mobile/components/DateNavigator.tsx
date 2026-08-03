import { Modal, Pressable, Text, View } from "react-native";
import { useState } from "react";

// Symmetric with the backend's real ingestion window — app/workers/ingest_fixtures.py backs
// this many days forward AND back (FEATURE_LOOKAHEAD_DAYS/FIXTURE_HISTORY_DAYS, both 7).
// Days outside it are rendered disabled rather than hidden: an empty feed reads as a bug,
// whereas a greyed-out date communicates that we genuinely hold no data there.
const DAYS_EACH_DIRECTION = 7;

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

export function startOfDay(date: Date): Date {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function addDays(date: Date, days: number): Date {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d;
}

function withinWindow(date: Date, today: Date): boolean {
  const diff = Math.round((startOfDay(date).getTime() - today.getTime()) / 86_400_000);
  return Math.abs(diff) <= DAYS_EACH_DIRECTION;
}

function headerLabel(date: Date, today: Date): string {
  if (isSameDay(date, today)) return "Today";
  if (isSameDay(date, addDays(today, -1))) return "Yesterday";
  if (isSameDay(date, addDays(today, 1))) return "Tomorrow";
  return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

/** Monday-first grid for a month, padded with nulls so weekday columns line up. */
function monthGrid(year: number, month: number): (Date | null)[] {
  const first = new Date(year, month, 1);
  const lead = (first.getDay() + 6) % 7; // JS weeks start Sunday; this shifts to Monday
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells: (Date | null)[] = Array(lead).fill(null);
  for (let day = 1; day <= daysInMonth; day++) cells.push(new Date(year, month, day));
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

interface Props {
  selected: Date;
  onSelect: (date: Date) => void;
}

export function DateNavigator({ selected, onSelect }: Props) {
  const today = startOfDay(new Date());
  const [open, setOpen] = useState(false);
  const [visibleMonth, setVisibleMonth] = useState(() => new Date(selected));

  const prev = addDays(selected, -1);
  const next = addDays(selected, 1);

  function openCalendar() {
    setVisibleMonth(new Date(selected)); // always reopen on the selected day's month
    setOpen(true);
  }

  function choose(date: Date) {
    onSelect(startOfDay(date));
    setOpen(false);
  }

  const cells = monthGrid(visibleMonth.getFullYear(), visibleMonth.getMonth());

  return (
    <>
      <View className="flex-1 flex-row items-center gap-2">
        <Arrow
          label="Previous day"
          glyph="‹"
          disabled={!withinWindow(prev, today)}
          onPress={() => onSelect(prev)}
        />
        <Pressable
          onPress={openCalendar}
          accessibilityRole="button"
          accessibilityLabel={`Date: ${headerLabel(selected, today)}. Opens calendar.`}
          className="flex-1 flex-row items-center justify-center gap-1 rounded-2xl bg-gray-100 px-4 py-3 active:opacity-70 dark:bg-gray-800"
        >
          <Text className="font-semibold text-gray-900 dark:text-white" numberOfLines={1}>
            {headerLabel(selected, today)}
          </Text>
          <Text className="text-xs text-gray-500 dark:text-gray-400">{open ? "▲" : "▼"}</Text>
        </Pressable>
        <Arrow
          label="Next day"
          glyph="›"
          disabled={!withinWindow(next, today)}
          onPress={() => onSelect(next)}
        />
      </View>

      <Modal visible={open} transparent animationType="fade" onRequestClose={() => setOpen(false)}>
        <Pressable className="flex-1 bg-black/30" onPress={() => setOpen(false)}>
          {/* Inner press must not bubble to the dismissing backdrop. */}
          <Pressable
            onPress={(e) => e.stopPropagation()}
            className="mx-6 mt-32 rounded-3xl bg-white p-4 shadow-xl dark:bg-gray-800"
          >
            <View className="mb-3 flex-row items-center justify-between">
              <Arrow
                label="Previous month"
                glyph="‹"
                onPress={() =>
                  setVisibleMonth(
                    new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() - 1, 1),
                  )
                }
              />
              <Text className="text-base font-semibold text-gray-900 dark:text-white">
                {visibleMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" })}
              </Text>
              <Arrow
                label="Next month"
                glyph="›"
                onPress={() =>
                  setVisibleMonth(
                    new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 1),
                  )
                }
              />
            </View>

            <View className="flex-row">
              {WEEKDAYS.map((day) => (
                <Text
                  key={day}
                  className="flex-1 py-1 text-center text-xs text-gray-400 dark:text-gray-500"
                >
                  {day}
                </Text>
              ))}
            </View>

            {Array.from({ length: cells.length / 7 }, (_, week) => (
              <View key={week} className="flex-row">
                {cells.slice(week * 7, week * 7 + 7).map((date, i) => (
                  <DayCell
                    key={date ? date.toISOString() : `pad-${week}-${i}`}
                    date={date}
                    today={today}
                    selected={selected}
                    onPress={choose}
                  />
                ))}
              </View>
            ))}

            <Pressable
              onPress={() => choose(today)}
              accessibilityRole="button"
              className="mt-3 items-center rounded-2xl bg-blue-600 py-4 active:opacity-80"
            >
              <Text className="font-bold tracking-wide text-white">TODAY</Text>
            </Pressable>
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
}

function DayCell({
  date,
  today,
  selected,
  onPress,
}: {
  date: Date | null;
  today: Date;
  selected: Date;
  onPress: (date: Date) => void;
}) {
  if (!date) return <View className="flex-1 py-2" />;
  const usable = withinWindow(date, today);
  const isSelected = isSameDay(date, selected);
  return (
    <Pressable
      disabled={!usable}
      onPress={() => onPress(date)}
      accessibilityRole="button"
      accessibilityState={{ selected: isSelected, disabled: !usable }}
      className="flex-1 items-center py-1"
    >
      <View
        className={`h-9 w-9 items-center justify-center rounded-full ${
          isSelected ? "bg-blue-600" : ""
        }`}
      >
        <Text
          className={
            isSelected
              ? "font-bold text-white"
              : usable
                ? "font-semibold text-gray-900 dark:text-gray-100"
                : "text-gray-300 dark:text-gray-600"
          }
        >
          {date.getDate()}
        </Text>
      </View>
    </Pressable>
  );
}

function Arrow({
  label,
  glyph,
  disabled,
  onPress,
}: {
  label: string;
  glyph: string;
  disabled?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      disabled={disabled}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: !!disabled }}
      className={`h-11 w-11 items-center justify-center rounded-2xl bg-gray-100 active:opacity-70 dark:bg-gray-800 ${
        disabled ? "opacity-40" : ""
      }`}
    >
      <Text className="text-lg text-gray-900 dark:text-white">{glyph}</Text>
    </Pressable>
  );
}
