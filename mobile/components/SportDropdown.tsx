import {
  DropdownMenu,
  FILTER_TRIGGER_WIDTH,
  type DropdownOption,
} from "@/components/DropdownMenu";
import type { SportResponse } from "@/lib/api/types";

/** Sentinel for the "All" row. Sports are keyed by slug and null means no filter, but the
 * shared menu deals in plain strings, so the two are mapped at this boundary. */
const ALL = "__all__";

/** A league row is encoded as "sportSlug/leagueSlug" so one flat menu can carry both levels.
 * The separator cannot appear in a slug (they are seeded lowercase alphanumeric + underscore),
 * so splitting on it is unambiguous. */
const LEVEL_SEPARATOR = "/";

export interface SportSelection {
  sport: string | null;
  league: string | null;
}

interface Props {
  sports: SportResponse[];
  selected: SportSelection;
  onSelect: (selection: SportSelection) => void;
}

function encode(sport: string, league?: string): string {
  return league ? `${sport}${LEVEL_SEPARATOR}${league}` : sport;
}

function decode(value: string): SportSelection {
  if (value === ALL) return { sport: null, league: null };
  const [sport, league] = value.split(LEVEL_SEPARATOR);
  return { sport, league: league ?? null };
}

/** Sport selector, replacing the horizontal chip scroller.
 *
 * With three sports the chips already spent a full row showing what one line can, and a
 * dropdown scales as sports are added without that row becoming scrollable (which also
 * sidesteps the flexGrow footgun that made the old chip ScrollView stretch — see CLAUDE.md).
 *
 * Unlike the status filter, this has an explicit "All" row rather than tap-to-unselect: a
 * sport is a positive choice a user makes and returns from, so the way back should be
 * visible in the menu rather than discovered.
 *
 * TWO LEVELS, where the data supports it. Basketball is NBA and WNBA under one Sport row, and
 * tennis is ATP and WTA — competitions a user thinks of as separate and picks between, even
 * though each pair shares a model. The backend decides which sports get expanded
 * (app/sports/router.py:LEAGUE_PICKER_MAX) and sends `leagues` only for those, so football's
 * 18 never appear here and nothing has to be special-cased per sport in this file. */
export function SportDropdown({ sports, selected, onSelect }: Props) {
  const options: DropdownOption[] = [{ value: ALL, label: "All" }];
  for (const sport of sports) {
    options.push({ value: encode(sport.slug), label: sport.name });
    for (const league of sport.leagues ?? []) {
      // Indented so the hierarchy reads at a glance without a nested menu, which on a phone
      // costs an extra tap and a second surface to dismiss.
      options.push({
        value: encode(sport.slug, league.slug),
        label: `   ${league.name}`,
      });
    }
  }

  const currentValue = selected.sport
    ? encode(selected.sport, selected.league ?? undefined)
    : ALL;
  const currentSport = sports.find((sport) => sport.slug === selected.sport);
  const currentLeague = currentSport?.leagues?.find(
    (league) => league.slug === selected.league,
  );
  // The trigger shows the LEAGUE when one is picked: "WNBA" is what the user chose and what
  // they are looking at, and "NBA Basketball" would actively contradict the feed below it.
  const triggerLabel = currentLeague?.name ?? currentSport?.name ?? "All";

  return (
    <DropdownMenu
      triggerLabel={triggerLabel}
      options={options}
      selected={currentValue}
      onSelect={(value) => onSelect(decode(value))}
      accessibilityLabel={`Sport: ${triggerLabel}`}
      triggerClassName={FILTER_TRIGGER_WIDTH}
    />
  );
}
