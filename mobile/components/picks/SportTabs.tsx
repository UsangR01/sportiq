import { useEffect, useRef } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import { SCREEN, TYPE, useTheme } from "@/lib/theme";

/** One-tap sport switching, in its own full-width row under the header.
 *
 * WHY NOT IN THE HEADER ROW: at 390px the wordmark and the filter button leave room for about
 * two tabs before the row has to scroll, and a scrolling row inside a fixed header reads as
 * broken rather than as scrollable.
 *
 * NO "ALL" TAB, and no fixture counts. A count does not help anyone choose a sport -- it turns
 * navigation into a stats readout, and the summary strip directly below already reports the
 * day's totals. "All" is reached by tapping the selected tab again.
 *
 * THIS DRIVES THE SAME STATE AS THE FILTER SHEET, which is the whole point: the sheet keeps
 * multi-select (Football + Tennis together), while a tab tap is the one-sport shortcut. The
 * store holds `sports: string[]`, so a tab writes a single-element array and the sheet can still
 * hold several. They cannot disagree because there is one value.
 *
 * AND IT MUST NOT LIGHT THE FILTER DOT. The selected sport is visible on this row, so showing
 * it a second time as "a filter is active" would train the dot to mean nothing --
 * hasActiveFilters ignores `sports` for exactly that reason.
 */

export interface SportTab {
  slug: string;
  label: string;
}

export const SPORT_TABS: SportTab[] = [
  { slug: "football", label: "Football" },
  { slug: "nba", label: "Basketball" },
  { slug: "tennis", label: "Tennis" },
];

const GLYPH = 16;
const FADE_WIDTH = 26;

export function SportTabs({
  selected,
  onSelect,
}: {
  /** The currently-shown sports. A tab reads as selected when it is the ONLY one. */
  selected: string[];
  onSelect: (slug: string) => void;
}) {
  const { colors } = useTheme();
  const scroller = useRef<ScrollView>(null);
  const offset = useRef(0);
  const overflowing = useRef(false);
  const contentWidth = useRef(0);
  const viewportWidth = useRef(0);

  const activeSlug = selected.length === 1 ? selected[0] : null;

  // Keep the selected tab fully visible, past the side padding and the edge fade -- and keep
  // the row where the user left it otherwise. setting scrollLeft directly rather than calling
  // scrollTo with animation on every render, which fights a user mid-swipe.
  useEffect(() => {
    if (!activeSlug || !scroller.current) return;
    const index = SPORT_TABS.findIndex((t) => t.slug === activeSlug);
    if (index < 0) return;
    const approximateTabWidth = 76;
    const left = index * approximateTabWidth;
    const right = left + approximateTabWidth + FADE_WIDTH;
    let next = offset.current;
    if (left < offset.current) next = Math.max(0, left - SCREEN.padding);
    else if (right > offset.current + viewportWidth.current) {
      next = right - viewportWidth.current;
    }
    if (next !== offset.current) {
      offset.current = next;
      scroller.current.scrollTo({ x: next, animated: true });
    }
  }, [activeSlug]);

  return (
    <View style={{ borderBottomWidth: 1, borderBottomColor: colors.border }}>
      <ScrollView
        ref={scroller}
        horizontal
        showsHorizontalScrollIndicator={false}
        // grow-0: RN bakes flexGrow:1 into ScrollView's base style, and two grow-1 siblings in a
        // column split the leftover space -- which is what once stretched the sport chips to
        // half the screen on an empty feed.
        style={{ flexGrow: 0, marginHorizontal: -SCREEN.padding }}
        contentContainerStyle={{ paddingHorizontal: SCREEN.padding, gap: 4 }}
        onScroll={(e) => {
          offset.current = e.nativeEvent.contentOffset.x;
        }}
        scrollEventThrottle={16}
        onLayout={(e) => {
          viewportWidth.current = e.nativeEvent.layout.width;
          overflowing.current = contentWidth.current > viewportWidth.current;
        }}
        onContentSizeChange={(w) => {
          contentWidth.current = w;
          overflowing.current = w > viewportWidth.current;
        }}
      >
        {SPORT_TABS.map((tab) => {
          const isActive = tab.slug === activeSlug;
          return (
            <Pressable
              key={tab.slug}
              onPress={() => onSelect(tab.slug)}
              accessibilityRole="tab"
              accessibilityState={{ selected: isActive }}
              style={{ minWidth: 68, paddingTop: 4, paddingHorizontal: 10, alignItems: "center" }}
            >
              <SportGlyph
                slug={tab.slug}
                // textSub, NOT textFaint, when unselected: at 12px textFaint fails 4.5:1, and
                // the glyph takes the same colour as its label so the pair reads as one thing.
                color={isActive ? colors.text : colors.textSub}
              />
              <Text
                numberOfLines={1}
                style={[
                  TYPE.sportTab,
                  {
                    marginTop: 3,
                    fontWeight: isActive ? "800" : "600",
                    color: isActive ? colors.text : colors.textSub,
                  },
                ]}
              >
                {tab.label}
              </Text>
              {/* Always rendered, transparent when unselected, so selecting a tab never changes
                  the row's height. */}
              <View
                style={{
                  height: 2,
                  alignSelf: "stretch",
                  marginTop: 4,
                  borderRadius: 1,
                  backgroundColor: isActive ? colors.accent : "transparent",
                }}
              />
            </Pressable>
          );
        })}
      </ScrollView>
    </View>
  );
}

/** Sport marks drawn from plain Views, for the same reason the tab-bar glyphs are: a platform
 * symbol set resolves to three different shapes across iOS, Android and web, so the row would
 * not look like the design on more than one platform at a time. */
function SportGlyph({ slug, color }: { slug: string; color: string }) {
  if (slug === "nba") return <BasketballGlyph color={color} />;
  if (slug === "tennis") return <TennisGlyph color={color} />;
  return <FootballGlyph color={color} />;
}

function Ring({ color, children }: { color: string; children?: React.ReactNode }) {
  return (
    <View
      style={{
        width: GLYPH,
        height: GLYPH,
        borderRadius: GLYPH / 2,
        borderWidth: 1.6,
        borderColor: color,
        alignItems: "center",
        justifyContent: "center",
        overflow: "hidden",
      }}
    >
      {children}
    </View>
  );
}

/** A ball with a panel at its centre. A true pentagon needs clip-path, which RN does not have,
 * so this is a small rotated square -- at 16px the two are indistinguishable, and inventing a
 * dependency to draw six pixels would not be. */
function FootballGlyph({ color }: { color: string }) {
  return (
    <Ring color={color}>
      <View
        style={{
          width: 6,
          height: 6,
          backgroundColor: color,
          transform: [{ rotate: "45deg" }],
        }}
      />
    </Ring>
  );
}

function BasketballGlyph({ color }: { color: string }) {
  return (
    <Ring color={color}>
      <View style={{ position: "absolute", width: GLYPH, height: 1.4, backgroundColor: color }} />
      <View style={{ position: "absolute", width: 1.4, height: GLYPH, backgroundColor: color }} />
    </Ring>
  );
}

/** The seam: two arcs clipped inside the ball. Drawn as oversized circles whose borders show
 * only where they cross the ring, which `overflow: hidden` on Ring takes care of. */
function TennisGlyph({ color }: { color: string }) {
  const arc = {
    position: "absolute" as const,
    width: 12,
    height: 12,
    borderRadius: 6,
    borderWidth: 1.4,
    borderColor: color,
  };
  return (
    <Ring color={color}>
      <View style={[arc, { left: -7 }]} />
      <View style={[arc, { right: -7 }]} />
    </Ring>
  );
}
