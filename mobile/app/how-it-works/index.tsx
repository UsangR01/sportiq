import { ScrollView, Text, View } from "react-native";

function Section({ title, children }: { title: string; children: string }) {
  return (
    <View className="mb-6">
      <Text className="mb-1 text-base font-semibold text-gray-900 dark:text-gray-100">
        {title}
      </Text>
      <Text className="text-sm leading-5 text-gray-600 dark:text-gray-400">{children}</Text>
    </View>
  );
}

export default function HowItWorksScreen() {
  return (
    <ScrollView className="flex-1 bg-white dark:bg-black" contentContainerClassName="p-4">
      <Section title="How predictions are made">
        Each sport has its own model: NBA uses an XGBoost classifier trained on rest days,
        recent form, head-to-head record, and — since key players missing a game matters —
        how many of a team's season-ranked Top 5 players are confirmed available. Football
        will use a two-stage expected-goals model once it has real data access.
      </Section>
      <Section title="Expected value">
        A pick's EV is (model probability × (odds − 1)) − (1 − model probability). Positive EV
        means the model thinks the odds on offer are better than they should be — it's not a
        guarantee, just a statistical edge over many bets.
      </Section>
      <Section title="Confidence tiers">
        Temporarily hidden. We checked the High/Medium/Low labels against real settled results
        and they did not hold up — picks labelled High were no more accurate than Medium ones,
        so showing the label would have pointed you at the wrong games. We would rather show
        nothing than something we have measured as misleading. It will come back if and when it
        earns its place.
      </Section>
      <Section title="What isn't live yet">
        Player injury data (RotoWire/BallDontLie) isn't connected yet, so key-player
        availability defaults to "healthy" for every team until that's wired up. Football
        predictions aren't live yet either — the current free-tier API access doesn't reach
        this season's fixtures.
      </Section>
    </ScrollView>
  );
}
