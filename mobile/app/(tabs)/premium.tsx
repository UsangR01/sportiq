import { Text, View } from "react-native";

import { SCREEN, TYPE, useTheme } from "@/lib/theme";

/** Top calls — PLACEHOLDER.
 *
 * The real screen (design spec §5) is Phase 5: it needs de-vigged market probability exposed by
 * the API, and it should be built against the MARKET-BLIND model probability rather than the
 * serving one — the serving model consumes market prices as an input feature, so a gap measured
 * against it is partly circular by construction. See §9.6.1 and §11.
 *
 * A placeholder rather than a hidden tab: the tab bar's five-slot layout is part of the design
 * being built, and hiding one would make every spacing decision in Phase 0 provisional.
 */
export default function PremiumScreen() {
  const { colors } = useTheme();
  return (
    <View
      style={{
        flex: 1,
        backgroundColor: colors.bg,
        paddingHorizontal: SCREEN.padding,
        paddingTop: SCREEN.paddingTop,
      }}
    >
      <Text style={[TYPE.wordmarkCompact, { color: colors.text }]}>SportPIQ</Text>
      <Text style={[TYPE.eyebrow, { color: colors.accent, marginTop: 6 }]}>Premium</Text>
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
        <Text style={[TYPE.pick, { color: colors.text, marginBottom: 6 }]}>Top calls</Text>
        <Text style={[TYPE.body, { color: colors.textSub, textAlign: "center" }]}>
          Where the model and the odds disagree.{"\n"}Coming soon.
        </Text>
      </View>
    </View>
  );
}
