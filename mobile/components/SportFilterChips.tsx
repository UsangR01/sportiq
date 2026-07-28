import { Pressable, ScrollView, Text } from "react-native";

import type { SportResponse } from "@/lib/api/types";

interface Props {
  sports: SportResponse[];
  selected: string | null;
  onSelect: (slug: string | null) => void;
}

export function SportFilterChips({ sports, selected, onSelect }: Props) {
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      // ScrollView's own base style sets flexGrow: 1 — left unset, this horizontal chip
      // strip competes with an empty FlatList sibling for leftover vertical space in the
      // column layout and stretches to fill it. grow-0 pins it to its content height.
      className="mb-3 grow-0"
      contentContainerClassName="items-center gap-2 px-4"
    >
      <Chip label="All" active={selected === null} onPress={() => onSelect(null)} />
      {sports.map((sport) => (
        <Chip
          key={sport.id}
          label={sport.name}
          active={selected === sport.slug}
          onPress={() => onSelect(sport.slug)}
        />
      ))}
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
