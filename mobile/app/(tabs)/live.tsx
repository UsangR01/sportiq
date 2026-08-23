import { useQuery } from "@tanstack/react-query";
import { router } from "expo-router";
import { FlatList, Pressable, Text, View } from "react-native";

import { listFixtures } from "@/lib/api/fixtures";
import type { FixtureSummary } from "@/lib/api/types";
import { getPreferences } from "@/lib/api/users";
import { formatOdds, toOddsFormat, type OddsFormat } from "@/lib/oddsFormat";
import { pickHeadline } from "@/lib/pickFormat";
import { GAP, ONE_LINE, RADIUS, SCREEN, TRACK_HEIGHT, TYPE, useTheme, useScreenInsets } from "@/lib/theme";
import { useAuthStore } from "@/store/authStore";

/** Football's regulation time, used only to draw the elapsed-time track.
 *
 * Deliberately not applied to other sports: basketball's live state carries no clock at all
 * (measured: match_minute populated on 0 of 263 rows) and tennis has no minute either, so
 * anything but football gets no progress track rather than a fabricated one. */
const FOOTBALL_FULL_TIME = 90;

/** Live matches (design spec §4).
 *
 * The `ON TRACK` / `AT RISK` verdict comes from the server (§4.1), which runs the SAME rules
 * that decide whether to push an alert — so the badge and the notification cannot disagree.
 *
 * WHO GETS A VERDICT AT ALL IS A DATA QUESTION, not a product one. Football can be judged (it
 * reports a real minute on every row); tennis only at completed-set granularity; basketball not
 * at all until its period is ingested; and corners never, because counts are written once at
 * settlement so no in-play value exists. Every one of those renders NO tag and a neutral bar
 * rather than a reassuring one.
 */
export default function LiveScreen() {
  const { colors } = useTheme();
  const insets = useScreenInsets();
  const accessToken = useAuthStore((state) => state.accessToken);

  const liveQuery = useQuery({
    queryKey: ["fixtures", "live"],
    queryFn: () => listFixtures({ status: "live", limit: 100 }),
    refetchInterval: 60 * 1000,
  });

  const preferencesQuery = useQuery({
    queryKey: ["preferences"],
    queryFn: getPreferences,
    enabled: !!accessToken,
  });
  const oddsFormat = toOddsFormat(preferencesQuery.data?.odds_format);

  const matches = liveQuery.data ?? [];

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <View
        style={{
          paddingTop: insets.top,
          paddingHorizontal: SCREEN.padding,
          paddingBottom: 12,
          flexDirection: "row",
          alignItems: "center",
          gap: 8,
        }}
      >
        <Text {...ONE_LINE} style={[TYPE.wordmark, { color: colors.text }]}>
          SportPIQ
        </Text>
        <View style={{ width: 7, height: 7, borderRadius: 3.5, backgroundColor: colors.fail }} />
        <View style={{ flex: 1 }} />
        {matches.length > 0 && (
          <Text style={[TYPE.caption, { color: colors.textFaint }]}>
            {matches.length} {matches.length === 1 ? "match" : "matches"}
          </Text>
        )}
      </View>

      <FlatList
        data={matches}
        keyExtractor={(item) => item.id}
        contentContainerStyle={{ paddingHorizontal: SCREEN.padding, paddingBottom: 24 }}
        renderItem={({ item }) => <LiveCard fixture={item} oddsFormat={oddsFormat} />}
        ListEmptyComponent={
          <View style={{ alignItems: "center", marginTop: 48, paddingHorizontal: 24 }}>
            {liveQuery.isLoading ? (
              <Text style={[TYPE.body, { color: colors.textFaint }]}>Loading…</Text>
            ) : liveQuery.isError ? (
              <Text style={[TYPE.body, { color: colors.fail }]}>
                Couldn&apos;t reach the SportPIQ API.
              </Text>
            ) : (
              <>
                <Text style={[TYPE.pick, { fontSize: 15, color: colors.text, marginBottom: 6 }]}>
                  Nothing is live right now
                </Text>
                <Text style={[TYPE.body, { color: colors.textSub, textAlign: "center" }]}>
                  Matches appear here while they are being played.
                </Text>
              </>
            )}
          </View>
        }
      />
    </View>
  );
}

function LiveCard({ fixture, oddsFormat }: { fixture: FixtureSummary; oddsFormat: OddsFormat }) {
  const { colors, elevation } = useTheme();
  const live = fixture.live_state;
  const pick = fixture.best_pick;

  const minute = live?.match_minute ?? null;
  const progress =
    fixture.sport_slug === "football" && minute != null
      ? Math.min(1, minute / FOOTBALL_FULL_TIME)
      : null;

  return (
    <Pressable
      onPress={() => router.push(`/fixture/${fixture.id}`)}
      accessibilityRole="link"
      style={{
        backgroundColor: colors.surface,
        borderRadius: RADIUS.card,
        borderWidth: 1,
        borderColor: colors.border,
        padding: 16,
        marginBottom: GAP.card,
        gap: 13,
        ...elevation.card,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
        <View style={{ width: 7, height: 7, borderRadius: 3.5, backgroundColor: colors.fail }} />
        <Text style={[TYPE.eyebrowSmall, { color: colors.fail }]}>
          {/* The period is the honest fallback where no clock exists — basketball and tennis
              carry no minute, so a fabricated one would be worse than a coarse label. */}
          {minute != null ? `${minute}'` : (live?.period ?? "Live")}
        </Text>
        <View style={{ flex: 1 }} />
        <Text {...ONE_LINE} style={[TYPE.eyebrowSmall, { color: colors.textFaint }]}>
          {fixture.league_name}
        </Text>
      </View>

      <View style={{ gap: 2 }}>
        <ScoreLine name={fixture.home_team} score={live?.home_score} />
        <ScoreLine name={fixture.away_team} score={live?.away_score} />
      </View>

      {progress != null && (
        <View
          style={{
            height: TRACK_HEIGHT.live,
            borderRadius: RADIUS.trackThin,
            backgroundColor: colors.mutedBg,
            overflow: "hidden",
          }}
        >
          <View
            style={{
              width: `${progress * 100}%`,
              height: "100%",
              borderRadius: RADIUS.trackThin,
              // THE BAR CARRIES THE VERDICT, not just the clock. Its LENGTH is elapsed time;
              // its COLOUR is how the pick is doing, so a glance down a list of live cards
              // reads as green-good / red-trouble without parsing a single tag.
              //
              // Neutral when there is no verdict, which is the important case: corners have no
              // in-play data and basketball reports no clock, so a green bar there would claim
              // a fact we do not have. Grey says "not applicable" rather than "fine".
              backgroundColor: trackColour(pick?.live_status, colors),
            }}
          />
        </View>
      )}

      {pick && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
          <Text {...ONE_LINE} style={[TYPE.pick, { flex: 1, fontSize: 12.5, color: colors.text }]}>
            {pickHeadline(pick)} · {Math.round(pick.probability * 100)}%
            {pick.odds != null ? ` · ${formatOdds(pick.odds, oddsFormat)}` : ""}
          </Text>
          <StatusTag status={pick.live_status} />
        </View>
      )}
    </Pressable>
  );
}

/** The progress track's colour: the pick's verdict, or neutral when there is none. */
function trackColour(status: string | null | undefined, colors: ReturnType<typeof useTheme>["colors"]) {
  if (status === "on_track") return colors.success;
  if (status === "at_risk" || status === "lost") return colors.fail;
  return colors.textFaint;
}

/** `ON TRACK` / `AT RISK` (design spec §4, §4.1).
 *
 * RENDERS NOTHING when the server sends no status, and that is the whole point. Corners are
 * written once at settlement so no in-play count exists, and basketball reports no clock — an
 * absent tag reads as "not applicable", while a grey one would read as "we checked and it is
 * fine". `lost` shows as AT RISK rather than a third label: by then the pick is gone, and a
 * card announcing a loss to someone watching the match adds nothing.
 */
function StatusTag({ status }: { status: string | null | undefined }) {
  const { colors } = useTheme();
  if (status !== "on_track" && status !== "at_risk" && status !== "lost") return null;

  const good = status === "on_track";
  return (
    <View
      style={{
        paddingHorizontal: 7,
        paddingVertical: 4,
        borderRadius: RADIUS.badge,
        backgroundColor: good ? colors.successSoft : colors.failSoft,
      }}
    >
      <Text style={[TYPE.eyebrowSmall, { color: good ? colors.success : colors.fail }]}>
        {good ? "ON TRACK" : "AT RISK"}
      </Text>
    </View>
  );
}

function ScoreLine({ name, score }: { name: string; score?: number }) {
  const { colors } = useTheme();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
      <Text {...ONE_LINE} style={[TYPE.team, { flex: 1, color: colors.text }]}>
        {name}
      </Text>
      {score != null && <Text style={[TYPE.score, { color: colors.text }]}>{score}</Text>}
    </View>
  );
}
