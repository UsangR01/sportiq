import {
  DropdownMenu,
  FILTER_TRIGGER_WIDTH,
  type DropdownOption,
} from "@/components/DropdownMenu";


/** The three states worth filtering to. POSTPONED is deliberately absent: nobody browses for
 * postponed games, and it stays visible under the default (no filter) so a fixture that was
 * called off never silently vanishes from its day. */
const OPTIONS: DropdownOption[] = [
  { value: "completed", label: "Finished" },
  { value: "live", label: "Live" },
  { value: "scheduled", label: "Upcoming" },
];

// Mirrors the subset of backend/app/fixtures/models.py:FixtureStatus that GET /fixtures
// accepts as a filter. POSTPONED is excluded by design (see OPTIONS above).
export type StatusFilter = "completed" | "live" | "scheduled";

interface Props {
  selected: StatusFilter | null;
  onSelect: (status: StatusFilter | null) => void;
}

/** Status filter with no explicit "All" row — tapping the active option clears it.
 *
 * There's no "All" entry because the menu only has three items and an unselect gesture keeps
 * it that way; the trigger reads "Status" whenever nothing is applied, so the default state
 * is still legible without spending a row on it. */
export function StatusDropdown({ selected, onSelect }: Props) {
  const active = OPTIONS.find((option) => option.value === selected);

  return (
    <DropdownMenu
      triggerLabel={active ? active.label : "Status"}
      options={OPTIONS}
      selected={selected}
      // Tapping the option that's already applied clears the filter rather than re-applying
      // it — the "click again to unselect" behaviour, which is also the only way back to the
      // default without an "All" row.
      onSelect={(value) =>
        onSelect(value === selected ? null : (value as StatusFilter))
      }
      accessibilityLabel={`Status: ${active ? active.label : "all"}`}
      triggerClassName={FILTER_TRIGGER_WIDTH}
    />
  );
}
