"""Rasterise the SportPIQ logo into every icon asset Expo needs.

Rendered through a real browser (Playwright driving the installed Edge) rather than an offline
rasteriser, because the logo is TYPE, set in Space Grotesk from Google Fonts. librsvg/resvg
would silently fall back to Arial and the result would look subtly wrong rather than fail.
document.fonts.ready is awaited before every screenshot for the same reason.

Each variant differs in ways that are NOT cosmetic:

  icon.png                    FULL BLEED, no rounded corners, NO transparency. iOS applies its
                              own mask; baking a radius leaves transparent corners, and iOS
                              renders transparency as black.
  android-icon-foreground.png Adaptive icons are cropped to a circle/squircle by the launcher,
                              so content must sit inside the central ~66% safe zone. The full
                              1024 lockup would lose its outer letters.
  android-icon-background.png The gradient plate, full bleed, behind that foreground.
  android-icon-monochrome.png Android 13+ themed icons: a single-colour silhouette on
                              transparency. Also the correct source for the notification icon,
                              which Android renders as a white mask regardless of colour.
  favicon.png                 The full lockup is unreadable at 64px, so this is the PIQ mark.
  splash-icon.png             Transparent, so the splash background colour shows through
                              instead of a dark square sitting on white.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(r"C:\Users\User\IdeaProjects\SportIQ")
OUT = REPO / "mobile" / "assets" / "images"

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@800&display=swap"'
    ' rel="stylesheet">'
)

DEFS = """
  <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" stop-color="#0A1022"/>
    <stop offset="100%" stop-color="#131C36"/>
  </linearGradient>
  <linearGradient id="accentGrad" x1="0%" y1="0%" x2="100%" y2="0%">
    <stop offset="0%" stop-color="#00F5A0"/>
    <stop offset="100%" stop-color="#00C2FF"/>
  </linearGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
    <stop offset="0%" stop-color="#00F5A0" stop-opacity="0.18"/>
    <stop offset="100%" stop-color="#00F5A0" stop-opacity="0"/>
  </radialGradient>
  <filter id="textGlow" x="-30%" y="-30%" width="160%" height="160%">
    <feGaussianBlur stdDeviation="6" result="blur"/>
    <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
"""

# The two-line lockup. Baselines are the author's own; the block's optical centre sits at
# y=527, which is what every scale transform below pivots around.
LOCKUP_CENTRE_Y = 527


def lockup(sport_fill="#B7C6DE", piq_fill="url(#accentGrad)", glow=True):
    filt = ' filter="url(#textGlow)"' if glow else ""
    return f"""
  <text x="512" y="475" text-anchor="middle" font-family="'Space Grotesk', Arial, sans-serif"
        font-weight="800" font-size="260" letter-spacing="8" fill="{sport_fill}">sport</text>
  <text x="512" y="734" text-anchor="middle" font-family="'Space Grotesk', Arial, sans-serif"
        font-weight="800" font-size="260" letter-spacing="14" fill="{piq_fill}"{filt}>PIQ</text>
"""


def scaled(inner, factor):
    return (
        f'<g transform="translate(512,{LOCKUP_CENTRE_Y}) scale({factor}) '
        f'translate(-512,-{LOCKUP_CENTRE_Y})">{inner}</g>'
    )


def svg(body, size):
    """The SVG's own width/height MUST equal the screenshot viewport.

    They did not, and it cost the favicon its text: `size` defaulted to 1024 while the viewport
    was 256, so the capture was the top-left QUADRANT of a 1024 canvas -- the plate rendered,
    and the mark at y=620 fell outside it. The default is removed so the two cannot drift."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 1024 1024" '
        f'xmlns="http://www.w3.org/2000/svg"><defs>{DEFS}</defs>{body}</svg>'
    )


PLATE = '<rect x="0" y="0" width="1024" height="1024" fill="url(#bgGrad)"/>'
GLOW = '<ellipse cx="512" cy="512" rx="340" ry="260" fill="url(#glow)"/>'

# 0.66 rather than a tighter fit: the launcher mask is a circle of ~660px diameter, and the
# lockup is WIDE, so its outer letters sit near the chord rather than the diameter.
SAFE_ZONE = 0.66

PIQ_MARK = (
    '<text x="512" y="655" text-anchor="middle" '
    'font-family="\'Space Grotesk\', Arial, sans-serif" font-weight="800" '
    'font-size="420" letter-spacing="14" fill="url(#accentGrad)" '
    'filter="url(#textGlow)">PIQ</text>'
)

# (filename, body markup WITHOUT the svg wrapper, size, transparent). The body is wrapped at
# render time with the SAME size as the viewport -- see svg().
VARIANTS = [
    ("icon.png", PLATE + GLOW + lockup(), 1024, False),
    ("android-icon-background.png", PLATE + GLOW, 1024, False),
    ("android-icon-foreground.png", scaled(lockup(), SAFE_ZONE), 1024, True),
    (
        "android-icon-monochrome.png",
        scaled(lockup(sport_fill="#FFFFFF", piq_fill="#FFFFFF", glow=False), SAFE_ZONE),
        1024,
        True,
    ),
    ("splash-icon.png", scaled(lockup(), 0.86), 1024, True),
    ("favicon.png", PLATE + GLOW + PIQ_MARK, 256, False),
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge")
        for name, body, size, transparent in VARIANTS:
            page = await browser.new_page(viewport={"width": size, "height": size})
            await page.set_content(
                f"<!doctype html><html><head>{FONT_LINK}"
                "<style>html,body{margin:0;padding:0;background:transparent}"
                "svg{display:block}</style></head><body>" + svg(body, size) + "</body></html>",
                wait_until="networkidle",
            )
            await page.evaluate("document.fonts.ready")
            # Confirms the real face loaded rather than the Arial fallback -- the whole reason
            # a browser is doing this job.
            loaded = await page.evaluate(
                "document.fonts.check('800 260px \"Space Grotesk\"')"
            )
            target = OUT / name
            await page.screenshot(path=str(target), omit_background=transparent)
            await page.close()
            print(f"  {name:32} {size}x{size}  space_grotesk={loaded}  "
                  f"{target.stat().st_size:,} bytes")
        await browser.close()


asyncio.run(main())
