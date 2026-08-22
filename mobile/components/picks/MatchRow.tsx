import { router } from "expo-router";
import { Pressable, Text, View } from "react-native";

import { formatOdds, type OddsFormat } from "@/lib/oddsFormat";
import { evaluatePickCorrectness, pickHeadline } from "@/lib/pickFormat";
import { ONE_LINE, RADIUS, RESULT_DISC, TRACK_HEIGHT, TYPE, useTheme } from "@/lib/theme";
import type { DriverRow, FixtureSummary } from "@/lib/api/types";

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

  const inPlay = fixture.status === "live";

  const statusLabel = postponed
    ? "Postponed"
    : settled
      ? "Full-time"
      : inPlay
        ? // A live row previously showed its KICK-OFF TIME, which made it indistinguishable
          // from one that had not started — the visible half of the same complaint that put
          // in-play matches under "Upcoming". The minute is shown where a sport reports one;
          // basketball and tennis ingest no clock, so they get the plain label rather than a
          // fabricated minute.
          live?.match_minute != null
          ? `${live.match_minute}'`
          : "Live"
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
      : inPlay
        ? colors.fail // same red as the Live tab, so "in progress" reads identically in both
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
          {/* The same red dot the Live tab uses, so an in-progress match is recognisable
              without reading the label. */}
          {inPlay && (
            <View
              style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: colors.fail }}
            />
          )}
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

/** The three factor rows (spec §3.2): 112px label, a proportional track, a direction word.
 *
 * DIRECTION AND RELATIVE WEIGHT, NEVER A PERCENTAGE. A figure like "48%" sitting inches from
 * the pick's own probability reads as part of it, and these contributions come from a different
 * model and cannot sum to it. The bar carries the magnitude; the word carries the sign.
 *
 * Bars are scaled against the LARGEST row rather than against the sum, so the leading factor
 * always fills the track. Scaling by share instead would make a pick with several balanced
 * drivers render as three stubs, which reads as "weak evidence" when it means the opposite.
 */
function FactorRows({ rows }: { rows: DriverRow[] }) {
  const { colors } = useTheme();
  const largest = Math.max(...rows.map((row) => Math.abs(row.contribution)), 1e-9);

  return (
    <View style={{ gap: 9 }}>
      {rows.map((row) => {
        const supports = row.contribution >= 0;
        return (
          <View key={row.label} style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
            <Text {...ONE_LINE} style={[TYPE.caption, { color: colors.textSub, width: 112 }]}>
              {row.label}
            </Text>
            <View
              style={{
                flex: 1,
                height: TRACK_HEIGHT.factor,
                borderRadius: RADIUS.track,
                backgroundColor: colors.mutedBg,
                overflow: "hidden",
              }}
            >
              <View
                style={{
                  width: `${Math.max(6, (Math.abs(row.contribution) / largest) * 100)}%`,
                  height: "100%",
                  borderRadius: RADIUS.track,
                  backgroundColor: supports ? colors.accent : colors.textFaint,
                }}
              />
            </View>
            <Text
              style={[
                TYPE.caption,
                { color: supports ? colors.text : colors.textFaint, width: 52, textAlign: "right" },
              ]}
            >
              {supports ? "For" : "Against"}
            </Text>
          </View>
        );
      })}
    </View>
  );
}

/** The expanded panel (spec §3.2).
 *
 * The factor rows are exact TreeSHAP contributions computed when the prediction was made. For
 * football they come from a model that never saw a bookmaker's price, which is deliberate — a
 * panel built on the serving model would keep reporting that the biggest reason for a pick is
 * the odds, which is both true and useless. The consequence is carried in the wording rather
 * than hidden: the rows describe what the DATA says and do not sum to the probability shown.
 *
 * When the server sends no drivers the panel says so plainly instead of inventing weights —
 * see BestPick.drivers for the several ordinary reasons that happens.
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
  const drivers = fixture.best_pick?.drivers ?? null;
  const isBlind = fixture.best_pick?.drivers_are_market_blind ?? false;

  return (
    <View style={{ paddingHorizontal: 16, paddingBottom: 15 }}>
      <View style={{ backgroundColor: colors.surfaceAlt, borderRadius: RADIUS.button, padding: 14 }}>
        {/* NOT "why the model called it", and the difference is load-bearing. For football
            these contributions decompose a market-blind model rather than the one that produced
            the number on the card, so claiming they explain that number would be false. */}
        <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint, marginBottom: 10 }]}>
          {drivers && drivers.length > 0
            ? isBlind
              ? "What the form data says"
              : "What drove this call"
            : "About this call"}
        </Text>

        {drivers && drivers.length > 0 ? (
          <FactorRows rows={drivers} />
        ) : (
          <Text style={[TYPE.body, { color: colors.textSub, marginBottom: 4 }]}>
            {/* Deliberately vague about WHICH reason: a user does not need to know whether this
                is an older prediction or a suppressed panel, only that we are not going to make
                something up. */}
            No driver breakdown for this pick.
          </Text>
        )}

        {asOf && (
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "space-between",
              marginTop: 12,
            }}
          >
            <Text style={[TYPE.eyebrowSmall, { color: colors.textFaint }]}>Called</Text>
            <Text style={[TYPE.caption, { color: colors.textSub }]}>{formatAsOf(asOf)}</Text>
          </View>
        )}

        {/* THE ROUTE TO THE FIXTURE SCREEN, and it is not optional.
         *
         * Head-to-head history, every other market's prediction, and match stats all live on
         * that screen. Before this feed expanded rows inline, tapping a card went there; the
         * expansion replaced that navigation and briefly left the whole thing unreachable from
         * the feed.
         *
         * A link rather than folding the content in here: the H2H panel alone is a win/draw/win
         * record plus five stat rows, and it costs up to six live API calls per view. That
         * belongs on a screen someone deliberately opens, not one that fires whenever a row is
         * tapped open. */}
        <Pressable
          onPress={() => router.push(`/fixture/${fixture.id}`)}
          accessibilityRole="link"
          style={{
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "space-between",
            marginTop: 12,
            paddingTop: 12,
            borderTopWidth: 1,
            borderTopColor: colors.border,
          }}
        >
          <View>
            <Text style={[TYPE.pick, { color: colors.accent }]}>Full details</Text>
            <Text style={[TYPE.caption, { color: colors.textFaint, fontWeight: "400" }]}>
              Head-to-head, other markets, match stats
            </Text>
          </View>
          <View
            style={{
              width: 8,
              height: 8,
              borderRightWidth: 1.8,
              borderTopWidth: 1.8,
              borderColor: colors.accent,
              transform: [{ rotate: "45deg" }],
            }}
          />
        </Pressable>

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
