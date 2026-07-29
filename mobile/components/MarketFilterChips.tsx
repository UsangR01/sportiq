import { Pressable, ScrollView, Text } from "react-native";

import type { PickMarket } from "@/lib/api/types";

export const GOALS_LINES = [1.5, 2.5, 3.5] as const;
export const CORNERS_LINES = [9.5] as const;

const MARKETS: { market: PickMarket; label: string }[] = [
  { market: "all", label: "All" },
  { market: "h2h", label: "1X2" },
  { market: "double_chance", label: "Double Chance" },
  { market: "goals_total", label: "Goals O/U" },
  { market: "corners_total", label: "Corners O/U" },
];

interface Props {
  selected: PickMarket;
  onSelect: (market: PickMarket) => void;
}

export function MarketFilterChips({ selected, onSelect }: Props) {
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      // Same flexGrow footgun as SportFilterChips — see that component's own note.
      className="mb-2 grow-0"
      contentContainerClassName="items-center gap-2 px-4"
    >
      {MARKETS.map(({ market, label }) => (
        <Chip key={market} label={label} active={selected === market} onPress={() => onSelect(market)} />
      ))}
    </ScrollView>
  );
}

export function LineFilterChips({
  lines,
  selected,
  onSelect,
}: {
  lines: readonly number[];
  selected: number;
  onSelect: (line: number) => void;
}) {
  if (lines.length <= 1) return null;
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      className="mb-2 grow-0"
      contentContainerClassName="items-center gap-2 px-4"
    >
      {lines.map((line) => (
        <Chip
          key={line}
          label={line.toString()}
          active={selected === line}
          onPress={() => onSelect(line)}
        />
      ))}
    </ScrollView>
  );
}

function Chip({ label, active, onPress }: { label: string; active: boolean; onPress: () => void }) {
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
