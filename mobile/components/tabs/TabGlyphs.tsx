/** The five bottom-tab glyphs (design spec §2).
 *
 * Drawn from plain Views rather than an icon font or SymbolView, because the spec gives exact
 * geometry (a 22px ring with a 10px inner circle; bars at 9/15/21; a 14px square rotated 45°)
 * and a platform symbol set will not honour those — SymbolView also resolves to three different
 * shapes across iOS, Android and web, so the tab bar would not look like the design on any two
 * platforms at once.
 *
 * Each glyph occupies a 22×22 box so the row stays optically even, and takes `color` from the
 * caller: `accent` when selected, `textFaint` otherwise. Nothing here reads the theme itself —
 * the tab bar owns that decision.
 */

import { View } from "react-native";
import type { ColorValue } from "react-native";

const BOX = 22;

interface GlyphProps {
  /** ColorValue, not string: React Navigation may hand a platform colour object rather than a
   * hex string, and casting that away would be a lie that only surfaces at runtime. */
  color: ColorValue;
}

function Box({ children }: { children: React.ReactNode }) {
  return (
    <View style={{ width: BOX, height: BOX, alignItems: "center", justifyContent: "center" }}>
      {children}
    </View>
  );
}

/** Picks — concentric rings: a 22px ring with a filled 10px core. Reads as a target. */
export function PicksGlyph({ color }: GlyphProps) {
  return (
    <Box>
      <View
        style={{
          width: 22,
          height: 22,
          borderRadius: 11,
          borderWidth: 2,
          borderColor: color,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <View style={{ width: 10, height: 10, borderRadius: 5, backgroundColor: color }} />
      </View>
    </Box>
  );
}

/** Live — three ascending bars (9 / 15 / 21), reading as a rising signal. */
export function LiveGlyph({ color }: GlyphProps) {
  return (
    <Box>
      <View style={{ flexDirection: "row", alignItems: "flex-end", gap: 3 }}>
        {[9, 15, 21].map((height) => (
          <View
            key={height}
            style={{ width: 3, height, borderRadius: 1.5, backgroundColor: color }}
          />
        ))}
      </View>
    </Box>
  );
}

/** Top calls — a filled 14px square rotated 45°, i.e. a diamond. Deliberately the only solid
 * shape in the row: it marks the premium surface without needing a badge. */
export function PremiumGlyph({ color }: GlyphProps) {
  return (
    <Box>
      <View
        style={{
          width: 14,
          height: 14,
          borderRadius: 3,
          backgroundColor: color,
          transform: [{ rotate: "45deg" }],
        }}
      />
    </Box>
  );
}

/** Saved — a bookmark outline with a notched base.
 *
 * DRAWN AS AN OPEN RECTANGLE PLUS TWO ANGLED LEGS, not as a wedge punched out of the shape.
 * The first attempt overlaid a rotated square in the bar's background colour; it rendered as a
 * plain rectangle, because React Native applies `translateY` along the ALREADY-ROTATED axis, so
 * the wedge travelled diagonally out of the clip region instead of down.
 *
 * The rewrite is also less fragile: punching a hole meant the glyph had to be told which colour
 * it was sitting on, so any surface change elsewhere could leave a coloured wedge across it.
 * This version only ever paints its own strokes.
 */
export function SavedGlyph({ color }: GlyphProps) {
  const STROKE = 2;
  const WIDTH = 14;
  const BODY_HEIGHT = 12;
  // Each leg spans half the width and meets its twin at the centre, forming the V.
  const LEG_LENGTH = 8;

  return (
    <Box>
      <View style={{ width: WIDTH, height: 18, alignItems: "center" }}>
        {/* Body: open at the bottom, so the legs complete the outline rather than crossing it. */}
        <View
          style={{
            width: WIDTH,
            height: BODY_HEIGHT,
            borderWidth: STROKE,
            borderBottomWidth: 0,
            borderColor: color,
            borderTopLeftRadius: 2,
            borderTopRightRadius: 2,
          }}
        />
        {[-1, 1].map((direction) => (
          <View
            key={direction}
            style={{
              position: "absolute",
              top: BODY_HEIGHT - STROKE,
              left: direction === -1 ? 0 : undefined,
              right: direction === 1 ? 0 : undefined,
              width: LEG_LENGTH,
              height: STROKE,
              backgroundColor: color,
              borderRadius: STROKE / 2,
              // ~41° gives a notch about 5px deep across a 14px span, which reads as a
              // bookmark rather than an envelope.
              transform: [{ rotate: `${direction * 41}deg` }],
            }}
          />
        ))}
      </View>
    </Box>
  );
}

/** Profile — head and shoulders: a 9px circle over a 16×8 shape rounded at the top only. */
export function ProfileGlyph({ color }: GlyphProps) {
  return (
    <Box>
      <View style={{ alignItems: "center", gap: 2 }}>
        <View style={{ width: 9, height: 9, borderRadius: 4.5, backgroundColor: color }} />
        <View
          style={{
            width: 16,
            height: 8,
            backgroundColor: color,
            borderTopLeftRadius: 8,
            borderTopRightRadius: 8,
          }}
        />
      </View>
    </Box>
  );
}
