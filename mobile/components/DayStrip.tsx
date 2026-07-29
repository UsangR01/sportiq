import { Pressable, ScrollView, Text, View } from "react-native";

// Symmetric with the backend's real ingestion window — app/workers/ingest_fixtures.py backs
// this many days forward AND back (FEATURE_LOOKAHEAD_DAYS/FIXTURE_HISTORY_DAYS, both 7).
// Picking a day further out than this would just show an empty list, since nothing is
// ingested that far out in either direction.
const DAYS_EACH_DIRECTION = 7;

export type DaySelection = "live" | { date: Date };

function startOfDay(date: Date): Date {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function buildDays(): Date[] {
  const today = startOfDay(new Date());
  const days: Date[] = [];
  for (let offset = -DAYS_EACH_DIRECTION; offset <= DAYS_EACH_DIRECTION; offset++) {
    const d = new Date(today);
    d.setDate(d.getDate() + offset);
    days.push(d);
  }
  return days;
}

function dayLabel(date: Date, today: Date): string {
  if (isSameDay(date, today)) return "TODAY";
  const weekday = date.toLocaleDateString(undefined, { weekday: "short" }).toUpperCase();
  return `${weekday} ${date.getDate()}`;
}

interface Props {
  selected: DaySelection;
  onSelect: (selection: DaySelection) => void;
}

export function DayStrip({ selected, onSelect }: Props) {
  const today = startOfDay(new Date());
  const days = buildDays();
  const isLiveSelected = selected === "live";

  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      className="mb-2 grow-0"
      contentContainerClassName="items-center gap-2 px-4"
    >
      <Chip label="LIVE" active={isLiveSelected} onPress={() => onSelect("live")} />
      {days.map((date) => {
        const active =
          !isLiveSelected && typeof selected === "object" && isSameDay(selected.date, date);
        return (
          <Chip
            key={date.toISOString()}
            label={dayLabel(date, today)}
            active={active}
            onPress={() => onSelect({ date })}
          />
        );
      })}
    </ScrollView>
  );
}

function Chip({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      className={`rounded-full px-3 py-1.5 ${active ? "bg-blue-600" : "bg-gray-200 dark:bg-gray-700"}`}
    >
      <Text className={active ? "font-medium text-white" : "text-gray-800 dark:text-gray-200"}>
        {label}
      </Text>
    </Pressable>
  );
}
