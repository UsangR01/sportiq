import { Pressable, Text, View } from "react-native";

import { formatOdds, type OddsFormat } from "@/lib/oddsFormat";
import { evaluatePickCorrectness, pickHeadline } from "@/lib/pickFormat";
import { ONE_LINE, RADIUS, RESULT_DISC, TRACK_HEIGHT, TYPE, useTheme } from "@/lib/theme";
import type { FixtureSummary } from "@/lib/api/types";

/** Below this share of the model's inputs, the probability is shown muted with a "limited
 * data" caption (spec §3.2). The server refuses to publish a pick under its own floor, so this
 * band is narrow — it exists so the app never recommends a pick and calls its own data limited
 * at the same time. */
const LOW_COMPLETENESS = 0.35;

/** One match, as a ROW inside its league's shared card — not a card of its own (spec §3.2).
 *
 * Three parts: a tappable summary block, the pick line, and (when expanded) a panel. A
 * postponed fixture replaces the pick line with a plain strip: the backend nulls its pick
 * outright, because showing a pre-postponement call for a match nobody is playing was a real
 * reported bug.
 */
export function MatchRow({
  fixture,
  first,
  expanded,
  onToggle,
  oddsFormat,
  isSaved,
  onToggleSaved,
  canSave,
}: {
  fixture: FixtureSummary;
  first: boolean;
  expanded: boolean;
  onToggle: () => void;
  oddsFormat: OddsFormat;
  isSaved: boolean;
  onToggleSaved: () => void;
  canSave: boolean;
}) {
  const { colors } = useTheme();
  const pick = fixture.best_pick;
  const live = fixture.live_state;
  const settled = fixture.status === "completed";
  const postponed = fixture.status === "postponed";

  const correct =
    settled && pick && live
      ? evaluatePickCorrectness(
          pick,
          live.home_score,
          live.away_score,
          live.home_corners,
          live.away_corners
        )
      : null;

  // A retirement or walkover is deliberately NOT scored: bookmakers generally void those, so a
  // green tick would imply a payout the user may never have received.
  const voided = Boolean(live?.result_type);
  const verdict = voided ? null : correct;

  const statusLabel = postponed
    ? "Postponed"
    : settled
      ? "Full-time"
      : fixture.kickoff_is_estimated
        ? "Time TBC"
        : // 2-digit hour, not "numeric": a 00:30 kick-off renders as "0:30" otherwise, which
          // reads as a truncated value rather than half past midnight. The eyebrow sits in a
          // column of times, so they should all be the same width.
          new Date(fixture.kickoff_utc).toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          });

  const statusColor = postponed
    ? colors.warn
    : settled
      ? colors.textFaint
      : colors.accent;

  const pickColor =
    verdict === true ? colors.success : verdict === false ? colors.fail : colors.accent;

  const lowInformation =
    pick?.feature_completeness != null && pick.feature_completeness < LOW_COMPLETENESS;

  return (
    <View
      style={{
        borderTopWidth: first ? 0 : 1,
        // Transparent rather than absent on the first row, so every row keeps the same height
        // and the card's internal rhythm does not shift by a pixel at the top.
        borderTopColor: first ? "transparent" : colors.border,
      }}
    >
      <Pressable
        onPress={onToggle}
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        style={{ paddingHorizontal: 16, paddingVertical: 15 }}
      >
        <View style={{ flexDirection: "row", alignItems: "center", gap: 7, marginBottom: 8 }}>
          <Text style={[TYPE.eyebrow, { color: statusColor, letterSpacing: 0.7 }]}>
            {statusLabel}
          </Text>
          {verdict != null && <ResultDisc won={verdict} />}
          {voided && (
            <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint }]}>
              {live?.result_type === "walkover" ? "W/O" : "Ret"}
            </Text>
          )}
          <View style={{ flex: 1 }} />
          <Chevron open={expanded} color={colors.textFaint} />
        </View>

        <TeamLine
          name={fixture.home_team}
          score={live?.home_score}
          winner={settled && !!live && live.home_score > live.away_score}
          showScore={Boolean(live) && !postponed}
        />
        <TeamLine
          name={fixture.away_team}
          score={live?.away_score}
          winner={settled && !!live && live.away_score > live.home_score}
          showScore={Boolean(live) && !postponed}
        />
      </Pressable>

      {postponed ? (
        <View style={{ paddingHorizontal: 16, paddingBottom: 15 }}>
          <View
            style={{
              paddingVertical: 8,
              alignItems: "center",
              borderRadius: RADIUS.chipTight,
              backgroundColor: colors.surfaceAlt,
            }}
          >
            <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, letterSpacing: 0.66 }]}>
              Rescheduled — no pick
            </Text>
          </View>
        </View>
      ) : pick ? (
        <View
          style={{
            flexDirection: "row",
            alignItems: "flex-end",
            gap: 12,
            paddingHorizontal: 16,
            paddingBottom: 15,
          }}
        >
          <View style={{ flex: 1 }}>
            <Text {...ONE_LINE} style={[TYPE.pick, { color: colors.text, marginBottom: 6 }]}>
              {pickHeadline(pick)}
            </Text>
            <View
              style={{
                height: TRACK_HEIGHT.pick,
                borderRadius: RADIUS.trackThin,
                backgroundColor: colors.mutedBg,
                overflow: "hidden",
              }}
            >
              <View
                style={{
                  width: `${Math.round(pick.probability * 100)}%`,
                  height: "100%",
                  borderRadius: RADIUS.trackThin,
                  backgroundColor: pickColor,
                }}
              />
            </View>
          </View>
          <View style={{ alignItems: "flex-end" }}>
            <Text
              style={[
                TYPE.pick,
                {
                  fontSize: 15,
                  fontWeight: "800",
                  // Muted rather than coloured when the vector was thin: the number is what the
                  // model genuinely says, it simply had little to go on.
                  color: lowInformation ? colors.textSub : pickColor,
                },
              ]}
            >
              {Math.round(pick.probability * 100)}%
            </Text>
            {pick.odds != null && (
              <Text style={[TYPE.caption, { color: colors.textFaint }]}>
                {formatOdds(pick.odds, oddsFormat)}
              </Text>
            )}
            {/* Only when it actually moved — a badge on every card would be wallpaper. */}
            {pick.previous_probability != null && (
              <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, letterSpacing: 0 }]}>
                was {Math.round(pick.previous_probability * 100)}%
              </Text>
            )}
            {lowInformation && (
              <Text style={[TYPE.eyebrowSmall, { color: colors.warn, letterSpacing: 0 }]}>
                limited data
              </Text>
            )}
          </View>
        </View>
      ) : null}

      {expanded && (
        <ExpandedPanel
          fixture={fixture}
          isSaved={isSaved}
          onToggleSaved={onToggleSaved}
          canSave={canSave}
        />
      )}
    </View>
  );
}

function TeamLine({
  name,
  score,
  winner,
  showScore,
}: {
  name: string;
  score?: number;
  winner: boolean;
  showScore: boolean;
}) {
  const { colors } = useTheme();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 1 }}>
      <Text
        {...ONE_LINE}
        style={[winner ? TYPE.teamWinner : TYPE.team, { flex: 1, color: colors.text }]}
      >
        {name}
      </Text>
      {showScore && score != null && (
        <Text style={[TYPE.score, { color: colors.text }]}>{score}</Text>
      )}
    </View>
  );
}

function ResultDisc({ won }: { won: boolean }) {
  const { colors } = useTheme();
  return (
    <View
      style={{
        width: RESULT_DISC,
        height: RESULT_DISC,
        borderRadius: RESULT_DISC / 2,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: won ? colors.success : colors.fail,
      }}
    >
      {/* The glyph is punched in the SURFACE colour rather than white, so the disc reads the
          same on a card in either theme. */}
      <Text style={{ fontSize: 9, fontWeight: "900", color: colors.surface, lineHeight: 11 }}>
        {won ? "✓" : "✕"}
      </Text>
    </View>
  );
}

function Chevron({ open, color }: { open: boolean; color: string }) {
  return (
    <View
      style={{
        width: 8,
        height: 8,
        borderRightWidth: 1.6,
        borderBottomWidth: 1.6,
        borderColor: color,
        transform: [{ rotate: open ? "225deg" : "45deg" }, { translateY: open ? 2 : -1 }],
      }}
    />
  );
}

/** The expanded panel (spec §3.2).
 *
 * The factor rows the design calls for need per-fixture attribution, which does not exist yet
 * — that is Phase 3 (market-blind TreeSHAP). Rather than fill the bars with invented weights,
 * this ships the parts that are real today: the provenance line and the save action. The
 * eyebrow says what is actually shown.
 */
function ExpandedPanel({
  fixture,
  isSaved,
  onToggleSaved,
  canSave,
}: {
  fixture: FixtureSummary;
  isSaved: boolean;
  onToggleSaved: () => void;
  canSave: boolean;
}) {
  const { colors } = useTheme();
  const asOf = fixture.best_pick?.as_of;

  return (
    <View style={{ paddingHorizontal: 16, paddingBottom: 15 }}>
      <View style={{ backgroundColor: colors.surfaceAlt, borderRadius: RADIUS.button, padding: 14 }}>
        <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, marginBottom: 10 }]}>
          About this call
        </Text>

        {asOf && (
          <View
            style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}
          >
            <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint }]}>Called</Text>
            <Text style={[TYPE.caption, { color: colors.textSub }]}>{formatAsOf(asOf)}</Text>
          </View>
        )}

        <Text style={[TYPE.body, { color: colors.textSub, marginTop: 10 }]}>
          {/* Honest about the gap rather than silent about it: a panel that explains nothing is
              better than one that explains something we cannot yet compute. */}
          Why the model called it is coming soon.
        </Text>

        {fixture.status === "scheduled" && (
          <Pressable
            onPress={onToggleSaved}
            accessibilityRole="button"
            style={{
              marginTop: 12,
              paddingVertical: 12,
              alignItems: "center",
              borderRadius: RADIUS.button,
              backgroundColor: isSaved ? colors.mutedBg : colors.accent,
            }}
          >
            <Text
              style={[
                TYPE.pick,
                { color: isSaved ? colors.textSub : "#ffffff" },
              ]}
            >
              {!canSave
                ? "Sign in to save this pick"
                : isSaved
                  ? "Remove from saved picks"
                  : "Save this pick"}
            </Text>
          </Pressable>
        )}
        {canSave && !isSaved && fixture.status === "scheduled" && (
          <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, letterSpacing: 0, marginTop: 6 }]}>
            Keeps this pick exactly as it is now, even if it changes later.
          </Text>
        )}
      </View>
    </View>
  );
}

/** Time alone for something called today, otherwise a short date — so a call that has not been
 * refreshed for days says so rather than showing a bare time that reads as recent. */
function formatAsOf(iso: string): string {
  const made = new Date(iso);
  if (Number.isNaN(made.getTime())) return "";
  const now = new Date();
  const sameDay =
    made.getFullYear() === now.getFullYear() &&
    made.getMonth() === now.getMonth() &&
    made.getDate() === now.getDate();
  return sameDay
    ? made.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : made.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
