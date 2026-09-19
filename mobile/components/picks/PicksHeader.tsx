import { Pressable, Text, View } from "react-native";

import { CONTROL, GAP, ONE_LINE, RADIUS, SCREEN, TYPE, useTheme, useScreenInsets } from "@/lib/theme";

/** The fixed, non-scrolling Picks header (design spec §3.1 row 1).
 *
 * Rows 2–4 (date stepper, summary strip, segmented control) are composed by the screen and
 * passed as `children`, so this file owns only the identity row and the header's frame. That
 * keeps the row that never changes separate from the three that depend on the day's data.
 *
 * WORDMARK LEFT, ONE CONTROL RIGHT. Two things were removed rather than moved, and both are
 * still reachable — checked before deleting, because an orphaned screen is a silent regression:
 *
 *   - the theme toggle → Profile already owns it (a three-state preference, which a two-state
 *     header toggle could never express properly anyway)
 *   - the hub button → Profile already links to How it works
 *
 * That leaves a single control, so it can sit flush right and carry the filter state.
 */
export function PicksHeader({
  isPremium,
  onOpenFilters,
  filtersActive,
  children,
}: {
  isPremium: boolean;
  onOpenFilters: () => void;
  /** Any filter off its default — drives the accent dot. */
  filtersActive: boolean;
  children?: React.ReactNode;
}) {
  const { colors } = useTheme();
  const insets = useScreenInsets();

  return (
    <View
      style={{
        paddingTop: insets.top,
        paddingHorizontal: SCREEN.padding,
        paddingBottom: 12,
        backgroundColor: colors.bg,
        gap: GAP.headerControl,
      }}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
        <Text {...ONE_LINE} style={[TYPE.wordmark, { color: colors.text, flex: 1 }]}>
          SportPIQ
        </Text>

        {/* Only when subscribed — an always-visible PRO pill would advertise a state the user
            is not in, which reads as a nag rather than a status. */}
        {isPremium && (
          <View
            style={{
              paddingHorizontal: 7,
              paddingVertical: 3,
              borderRadius: RADIUS.badge,
              backgroundColor: colors.accentSoft,
            }}
          >
            <Text style={[TYPE.eyebrowSmall, { color: colors.accent }]}>PRO</Text>
          </View>
        )}

        <FilterButton
          onPress={onOpenFilters}
          active={filtersActive}
          // Overflow is what makes the dot land on the CORNER rather than inside the button, so
          // it must not be clipped. RN does not clip by default, but a stray overflow:"hidden"
          // on a future wrapper would silently swallow it.
        />
      </View>

      {children}
    </View>
  );
}

/** The one control in this row: three stacked bars of descending width.
 *
 * A FILTER GLYPH, NOT A HAMBURGER, and the descending widths are the whole point — this button
 * opens filters, and three equal bars is the universal sign for "navigation menu". Promising
 * navigation and delivering a filter sheet is a worse failure than an unfamiliar glyph.
 *
 * THE DOT IS THE REASON THIS IS WORTH DOING. A card can vanish from the feed because a slider
 * sits where the user left it days ago, and with the controls behind a sheet there was nothing
 * on screen saying so — which is exactly the "how come this wasn't on the card?" report. The dot
 * makes a non-default filter visible without opening anything. It rings itself in `bg` so it
 * stays legible where it overlaps the button's own fill.
 */
function FilterButton({ onPress, active }: { onPress: () => void; active: boolean }) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={active ? "Filters, some changed from default" : "Filters"}
      style={{
        width: CONTROL.iconButton,
        height: CONTROL.iconButton,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: RADIUS.button,
        backgroundColor: colors.surfaceAlt,
      }}
    >
      <View style={{ gap: 3.5 }}>
        {[15, 9, 13].map((width, i) => (
          <View
            key={i}
            style={{ width, height: 1.8, borderRadius: 1, backgroundColor: colors.text }}
          />
        ))}
      </View>
      {active && (
        <View
          style={{
            position: "absolute",
            top: -2,
            right: -2,
            width: 8,
            height: 8,
            borderRadius: 4,
            borderWidth: 1.5,
            borderColor: colors.bg,
            backgroundColor: colors.accent,
          }}
        />
      )}
    </Pressable>
  );
}
