"""Draw the three sport marks as monochrome PNGs for the Picks tab row.

WHY DRAWN RATHER THAN DOWNLOADED. The repo is public and these ship inside the app, so an icon
of uncertain licence is a real liability for three glyphs. Drawing them also means the geometry
is the actual thing -- a football with a genuine pentagon and radiating seams rather than a
rotated square standing in for one, which is what prompted this.

WHY PNG RATHER THAN SVG. react-native-svg is not installed and is a NATIVE module: adding it
forces a new EAS build and a reinstall for everyone holding the current APK, which is far too
much for three icons. PNGs ship inside an ordinary over-the-air update.

WHITE ON TRANSPARENT, because React Native's Image `tintColor` recolours a monochrome image at
render time. That is what lets one asset serve selected and unselected states in both themes --
the glyphs they replace took `currentColor`, and losing that would have meant four files each.

Supersampled 8x and downscaled: Pillow's draw primitives are not anti-aliased, and a 16px ball
with jagged edges would look worse than the shapes it replaces.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(r"C:\Users\User\IdeaProjects\SportIQ\mobile\assets\sports")
SIZE = 96  # rendered at 96 so a 16pt glyph stays crisp at 3x device density
SS = 8  # supersample factor
S = SIZE * SS
WHITE = (255, 255, 255, 255)


def canvas():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def finish(img: Image.Image, name: str):
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / name)
    print(f"  wrote {name}")


def ring(draw, stroke):
    pad = stroke / 2 + 2 * SS
    draw.ellipse([pad, pad, S - pad, S - pad], outline=WHITE, width=int(stroke))


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def football():
    """Ball outline, a true centre pentagon, and seams running out to the edge."""
    img = canvas()
    d = ImageDraw.Draw(img)
    stroke = 5 * SS
    ring(d, stroke)
    cx = cy = S / 2
    pent_r = S * 0.19
    # -90 puts a vertex at the top, which is how a football panel is always drawn.
    pts = [polar(cx, cy, pent_r, -90 + i * 72) for i in range(5)]
    d.polygon(pts, fill=WHITE)
    # Seams from each pentagon vertex outward to the ball edge.
    for i in range(5):
        x, y = polar(cx, cy, pent_r, -90 + i * 72)
        ex, ey = polar(cx, cy, S * 0.40, -90 + i * 72)
        d.line([x, y, ex, ey], fill=WHITE, width=int(stroke * 0.8))
    finish(img, "football.png")


def clipped_to_ball(seams: Image.Image, stroke) -> Image.Image:
    """Keep only the part of a seam layer that falls inside the ball.

    Pillow's arc() is not clipped to anything, so a bowed seam drawn with an off-centre bounding
    box spills OUTSIDE the outline and the mark stops reading as a ball -- which is exactly what
    the first basketball did. Masking is the fix; nudging the arc geometry is not, because the
    bulge that makes a seam look like a seam is the same bulge that escapes the circle.
    """
    pad = stroke / 2 + 2 * SS
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([pad, pad, S - pad, S - pad], fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(seams, (0, 0), mask)
    return out


def basketball():
    """Ball outline, the two straight seams, and the two curved ones -- seams clipped inside."""
    img = canvas()
    d = ImageDraw.Draw(img)
    stroke = 5 * SS
    ring(d, stroke)
    pad = stroke / 2 + 2 * SS

    seams = canvas()
    sd = ImageDraw.Draw(seams)
    sd.line([pad, S / 2, S - pad, S / 2], fill=WHITE, width=int(stroke * 0.75))
    sd.line([S / 2, pad, S / 2, S - pad], fill=WHITE, width=int(stroke * 0.75))
    bulge = S * 0.52
    sd.arc([-bulge, pad, S * 0.42, S - pad], 280, 80, fill=WHITE, width=int(stroke * 0.75))
    sd.arc([S * 0.58, pad, S + bulge, S - pad], 100, 260, fill=WHITE, width=int(stroke * 0.75))
    img.alpha_composite(clipped_to_ball(seams, stroke))
    finish(img, "basketball.png")


def tennis():
    """Ball outline plus the single S-curve seam, drawn as two opposing arcs."""
    img = canvas()
    d = ImageDraw.Draw(img)
    stroke = 5 * SS
    ring(d, stroke)
    pad = stroke / 2 + 2 * SS
    # Two arcs bowing in from each side, which is what the seam actually looks like head-on.
    seams = canvas()
    sd = ImageDraw.Draw(seams)
    sd.arc([-S * 0.34, pad, S * 0.30, S - pad], 300, 60, fill=WHITE, width=int(stroke * 0.85))
    sd.arc([S * 0.70, pad, S * 1.34, S - pad], 120, 240, fill=WHITE, width=int(stroke * 0.85))
    img.alpha_composite(clipped_to_ball(seams, stroke))
    finish(img, "tennis.png")


print(f"drawing into {OUT}")
football()
basketball()
tennis()
