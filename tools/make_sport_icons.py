"""Draw the three sport marks for the Picks tab row, in their real colours.

WHY DRAWN RATHER THAN DOWNLOADED. The repo is public and these ship inside the app, so an icon
of uncertain licence is a real liability for three glyphs. Drawing them also means the geometry
is the actual thing rather than an approximation of it.

WHY PNG RATHER THAN SVG. react-native-svg is a NATIVE module: adding it forces a new EAS build
and a reinstall for everyone holding the current APK, which is far too much for three icons.
PNGs ship inside an ordinary over-the-air update.

COLOUR, NOT TINTED MONOCHROME -- and that costs something worth stating. A tinted white
silhouette could be recoloured at render time, so ONE file served selected/unselected in both
themes. Real colours cannot: a football is white and black whatever the theme. So selection is
carried by the label weight and the underline, which already carry it, and by dimming the icon's
opacity rather than its hue.

EVERY BALL GETS A DARK OUTLINE, and that is load-bearing rather than decorative: the app's light
theme ground is #f5f6f8, and a white football drawn without one is very nearly invisible on it.
The outline is what lets a single asset work on both grounds.

Supersampled 8x and downscaled -- Pillow's primitives are not anti-aliased, and a jagged 18px
ball would look worse than the flat shapes these replace.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "mobile" / "assets" / "sports"
SIZE = 96
SS = 8
S = SIZE * SS

# Real ball colours, kept as named constants so a future tweak is one edit rather than a hunt.
INK = (26, 26, 26, 255)  # panel black / outline, softer than pure black at 18px
WHITE = (247, 247, 247, 255)
SPALDING = (214, 106, 42, 255)  # the orange a Spalding actually is, not a saturated #FF7F00
TENNIS_GREEN = (206, 231, 63, 255)  # optic yellow-green
SEAM_WHITE = (252, 252, 250, 255)


def canvas():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def finish(img, name):
    img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT / name)
    print(f"  wrote {name}")


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def ball_base(fill, outline_width):
    """A filled disc with a dark rim. Returns the image, its draw handle and the inset used."""
    img = canvas()
    d = ImageDraw.Draw(img)
    pad = outline_width / 2 + 2 * SS
    d.ellipse([pad, pad, S - pad, S - pad], fill=fill, outline=INK, width=int(outline_width))
    return img, d, pad


def clip_to_ball(layer, pad):
    """Keep only what falls inside the ball.

    Pillow's arc() and polygon() are clipped to nothing, so a seam drawn with an off-centre
    bounding box spills outside the rim and the mark stops reading as a ball -- which is exactly
    what the first basketball did. Masking is the fix; nudging the geometry is not, because the
    bulge that makes a seam look like a seam is the same bulge that escapes the circle.
    """
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([pad, pad, S - pad, S - pad], fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(layer, (0, 0), mask)
    return out


def rim(d, stroke, pad):
    """Redraw the outline OVER the seams.

    Seams are drawn to the ball's edge on purpose -- that is where they go -- but a seam painted
    across the rim leaves the outline visibly broken, which is what made the first tennis ball
    read as a lens rather than a ball. Painting the rim last closes it again without shortening
    any seam.
    """
    d.ellipse([pad, pad, S - pad, S - pad], outline=INK, width=int(stroke))


def football():
    """White ball, black centre pentagon, and the five partial panels around the rim."""
    stroke = 4 * SS
    img, d, pad = ball_base(WHITE, stroke)
    cx = cy = S / 2
    panels = canvas()
    pd = ImageDraw.Draw(panels)

    # Centre panel. -90 puts a vertex at the top, which is how a football is always drawn.
    pent_r = S * 0.20
    centre = [polar(cx, cy, pent_r, -90 + i * 72) for i in range(5)]
    pd.polygon(centre, fill=INK)

    # Seams from each vertex out toward the rim. NO rim wedges: the first version drew a black
    # panel where each seam lands, those fused with the outline, and the whole mark read as a
    # COG rather than a ball. A centre panel with five seams is what a football icon is.
    for i in range(5):
        ang = -90 + i * 72
        x0, y0 = polar(cx, cy, pent_r, ang)
        x1, y1 = polar(cx, cy, S * 0.42, ang)
        pd.line([x0, y0, x1, y1], fill=INK, width=int(stroke * 0.85))

    img.alpha_composite(clip_to_ball(panels, pad))
    rim(d, stroke, pad)
    finish(img, "football.png")


def basketball():
    """Spalding orange, with the two straight seams and the two bowed ones."""
    stroke = 4 * SS
    img, d, pad = ball_base(SPALDING, stroke)
    seams = canvas()
    sd = ImageDraw.Draw(seams)
    w = int(stroke * 0.85)
    sd.line([pad, S / 2, S - pad, S / 2], fill=INK, width=w)
    sd.line([S / 2, pad, S / 2, S - pad], fill=INK, width=w)
    bulge = S * 0.52
    sd.arc([-bulge, pad, S * 0.42, S - pad], 280, 80, fill=INK, width=w)
    sd.arc([S * 0.58, pad, S + bulge, S - pad], 100, 260, fill=INK, width=w)
    img.alpha_composite(clip_to_ball(seams, pad))
    rim(d, stroke, pad)
    finish(img, "basketball.png")


def tennis():
    """Optic green, with the white S-curve seam."""
    stroke = 4 * SS
    img, d, pad = ball_base(TENNIS_GREEN, stroke)
    seams = canvas()
    sd = ImageDraw.Draw(seams)
    # Arcs pulled INWARD and widened. The first attempt hugged the rim, so the rim redraw above
    # covered almost all of the seam and the ball read as a plain green circle -- the seam has to
    # sit inside the ball to be seen at 18px, which is also where it sits on a real one.
    w = int(stroke * 0.95)
    sd.arc([-S * 0.16, pad, S * 0.48, S - pad], 300, 60, fill=SEAM_WHITE, width=w)
    sd.arc([S * 0.52, pad, S * 1.16, S - pad], 120, 240, fill=SEAM_WHITE, width=w)
    img.alpha_composite(clip_to_ball(seams, pad))
    rim(d, stroke, pad)
    finish(img, "tennis.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"drawing into {OUT}")
    football()
    basketball()
    tennis()
