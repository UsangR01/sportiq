import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { ScrollView, Text, View } from "react-native";

import { getTeamSchedule } from "@/lib/api/fixtures";
import type { TeamFixtureRow } from "@/lib/api/types";
import { FORM_RUNS } from "@/lib/theme";

/** A club's recent results and next fixtures, reached by tapping a team name or a standings row.
 *
 * ACROSS EVERY COMPETITION, and that is the reason this comes from the provider rather than
 * from our own fixtures table. We ingest league matches only, and had accumulated about six
 * weeks of them when this shipped — measured at the time, ZERO teams in any league had ten
 * completed fixtures stored and EPL's median was three. A locally-built schedule would have
 * shown three rows under a "last 10" heading and omitted every cup tie and European night,
 * which are exactly the matches that make a run of form legible.
 */
export default function TeamScheduleScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const query = useQuery({
    queryKey: ["team-schedule", id],
    queryFn: () => getTeamSchedule(id),
    enabled: !!id,
  });

  if (query.isLoading) {
    return (
      <View className="flex-1 items-center justify-center bg-white dark:bg-black">
        <Text className="text-gray-400">Loading…</Text>
      </View>
    );
  }
  if (query.isError || !query.data) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
        <Text className="text-center text-gray-400">
          No schedule available for this team.
        </Text>
      </View>
    );
  }

  const { team_name, rows } = query.data;
  const played = rows.filter((row) => row.result != null);
  const upcoming = rows.filter((row) => row.result == null);
  const record = {
    W: played.filter((row) => row.result === "W").length,
    D: played.filter((row) => row.result === "D").length,
    L: played.filter((row) => row.result === "L").length,
  };

  return (
    <ScrollView className="flex-1 bg-white dark:bg-black" contentContainerClassName="p-4">
      <Text className="text-2xl font-bold text-gray-900 dark:text-gray-100">{team_name}</Text>
      <Text className="mb-4 text-xs text-gray-400">Form and schedule, all competitions</Text>

      {played.length > 0 && (
        <View className="mb-5">
          <View className="mb-2 flex-row items-center justify-between">
            <Text className="text-xs font-semibold uppercase tracking-wide text-gray-400">
              Form · last {played.length}
            </Text>
            <Text className="text-xs font-semibold text-gray-500 dark:text-gray-400">
              {record.W}W · {record.D}D · {record.L}L
            </Text>
          </View>
          {/* Oldest first, so the run reads left to right the way a form string does — the
              opposite order to the list below, which leads with the most recent match. */}
          <View className="flex-row flex-wrap gap-1.5">
            {[...played].reverse().map((row) => (
              <FormPip key={row.fixture_external_id} result={row.result!} />
            ))}
          </View>
        </View>
      )}

      {upcoming.length > 0 && (
        <Section title="Next up">
          {upcoming.map((row) => (
            <FixtureRow key={row.fixture_external_id} row={row} />
          ))}
        </Section>
      )}

      {played.length > 0 && (
        <Section title={`Last ${played.length}, most recent first`}>
          {played.map((row) => (
            <FixtureRow key={row.fixture_external_id} row={row} />
          ))}
        </Section>
      )}

      {rows.length === 0 && (
        <Text className="mt-8 text-center text-sm text-gray-400">
          No fixtures found for this team.
        </Text>
      )}
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View className="mb-5">
      <Text className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
        {title}
      </Text>
      <View className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-800">
        {children}
      </View>
    </View>
  );
}

function FixtureRow({ row }: { row: TeamFixtureRow }) {
  const when = new Date(row.kickoff_utc);
  const date = Number.isNaN(when.getTime())
    ? ""
    : when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "2-digit" });

  return (
    <View className="flex-row items-center border-b border-gray-100 px-3 py-2.5 last:border-b-0 dark:border-gray-800">
      <View className="flex-1 pr-3">
        <Text numberOfLines={1} className="text-sm text-gray-900 dark:text-gray-100">
          {/* "vs" and "at" rather than H/A: it reads as a sentence and needs no legend. */}
          {row.at_home ? "vs" : "at"} {row.opponent}
        </Text>
        <Text numberOfLines={1} className="text-xs text-gray-400">
          {date} · {row.competition}
        </Text>
      </View>
      {row.result == null ? (
        // Never a 0-0 placeholder for a match that has not happened.
        <Text className="text-xs text-gray-400">
          {Number.isNaN(when.getTime())
            ? "To play"
            : when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
        </Text>
      ) : (
        <>
          <Text className="mr-3 text-sm font-bold tabular-nums text-gray-900 dark:text-gray-100">
            {row.team_score} – {row.opponent_score}
          </Text>
          <FormPip result={row.result} />
        </>
      )}
    </View>
  );
}

/** Letter AND colour, never colour alone — the same rule the fixture screen's form chips
 * follow, using the same contrast-checked fills (see lib/theme/tokens.ts). */
function FormPip({ result }: { result: "W" | "D" | "L" }) {
  const fill = result === "W" ? FORM_RUNS.W : result === "L" ? FORM_RUNS.L : FORM_RUNS.D;
  return (
    <View
      style={{ backgroundColor: fill }}
      className="h-6 w-6 items-center justify-center rounded-full"
    >
      <Text className="text-xs font-bold text-white">{result}</Text>
    </View>
  );
}
