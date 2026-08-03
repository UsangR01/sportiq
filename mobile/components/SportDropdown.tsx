import { DropdownMenu, type DropdownOption } from "@/components/DropdownMenu";
import type { SportResponse } from "@/lib/api/types";

/** Sentinel for the "All" row. Sports are keyed by slug and null means no filter, but the
 * shared menu deals in plain strings, so the two are mapped at this boundary. */
const ALL = "__all__";

interface Props {
  sports: SportResponse[];
  selected: string | null;
  onSelect: (slug: string | null) => void;
}

/** Sport selector, replacing the horizontal chip scroller.
 *
 * With three sports the chips already spent a full row showing what one line can, and a
 * dropdown scales as sports are added without that row becoming scrollable (which also
 * sidesteps the flexGrow footgun that made the old chip ScrollView stretch — see CLAUDE.md).
 *
 * Unlike the status filter, this has an explicit "All" row rather than tap-to-unselect: a
 * sport is a positive choice a user makes and returns from, so the way back should be
 * visible in the menu rather than discovered. */
export function SportDropdown({ sports, selected, onSelect }: Props) {
  const options: DropdownOption[] = [
    { value: ALL, label: "All" },
    ...sports.map((sport) => ({ value: sport.slug, label: sport.name })),
  ];
  const current = sports.find((sport) => sport.slug === selected);

  return (
    <DropdownMenu
      triggerLabel={current ? current.name : "All"}
      options={options}
      selected={selected ?? ALL}
      onSelect={(value) => onSelect(value === ALL ? null : value)}
      accessibilityLabel={`Sport: ${current ? current.name : "All"}`}
    />
  );
}
