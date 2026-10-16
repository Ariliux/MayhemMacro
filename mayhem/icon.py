"""MayhemMacro's icon: a glowing amber block on a dark rounded tile.

Used for the app icon (tools/make_icon.py writes mayhemmacro.ico from it)
and the logo inside the app. Small sizes drop the sparkles and panels."""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP, LEFT, RIGHT = ((255, 214, 140), (255, 196, 92)), ((247, 170, 40), (222, 132, 14)), ((186, 112, 12), (128, 72, 6))


def _grad(size, c0, c1):
    """A vertical gradient strip, resized to fill any polygon mask."""
    g = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        g.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(c0, c1)))
    return g.resize(size)


def _face(canvas, pts, colours):
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    ys = [p[1] for p in pts]
    fill = _grad((canvas.width, int(max(ys) - min(ys)) + 1), *colours)
    layer = Image.new("RGB", canvas.size)
    layer.paste(fill, (0, int(min(ys))))
    canvas.paste(layer, (0, 0), mask)


def _sparkle(draw, cx, cy, r, alpha):
    """A four-point star."""
    w = r * 0.22
    pts = [(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
           (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)]
    draw.polygon(pts, fill=(255, 244, 214, alpha))


def render(px: int, detail: bool = True) -> Image.Image:
    ss = 4
    n = px * ss
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # tile: rounded square, navy gradient
    tile = Image.new("L", (n, n), 0)
    pad = int(n * 0.04)
    ImageDraw.Draw(tile).rounded_rectangle([pad, pad, n - pad, n - pad], radius=int(n * 0.23), fill=255)
    bg = _grad((n, n), (30, 40, 58), (9, 13, 21)).convert("RGBA")
    img.paste(bg, (0, 0), tile)

    # aura behind the block
    glow = Image.new("L", (n, n), 0)
    gr = n * (0.36 if detail else 0.40)
    ImageDraw.Draw(glow).ellipse([n / 2 - gr, n * 0.53 - gr, n / 2 + gr, n * 0.53 + gr], fill=200)
    glow = glow.filter(ImageFilter.GaussianBlur(n * 0.09))
    glow = Image.composite(glow, Image.new("L", (n, n), 0), tile)
    img = Image.composite(Image.new("RGBA", (n, n), (245, 165, 36, 255)), img,
                          glow.point(lambda v: int(v * (0.55 if detail else 0.4))))

    # the block (isometric cube)
    cube = Image.new("RGB", (n, n))
    cmask = Image.new("L", (n, n), 0)
    s = n * (0.25 if detail else 0.30)
    cx, cy = n / 2, n * 0.54
    h = s * 0.58
    top = [(cx, cy - s), (cx + s, cy - s + h), (cx, cy - s + 2 * h), (cx - s, cy - s + h)]
    left = [(cx - s, cy - s + h), (cx, cy - s + 2 * h), (cx, cy + s + 0.16 * s), (cx - s, cy + s - h + 0.16 * s)]
    right = [(cx, cy - s + 2 * h), (cx + s, cy - s + h), (cx + s, cy + s - h + 0.16 * s), (cx, cy + s + 0.16 * s)]
    for pts, col in ((top, TOP), (left, LEFT), (right, RIGHT)):
        _face(cube, pts, col)
        ImageDraw.Draw(cmask).polygon(pts, fill=255)
    if detail:
        # an inset panel on each face, like the game's patterned blocks
        d = ImageDraw.Draw(cube)
        for pts, col in ((top, (255, 232, 182)), (left, (255, 190, 82)), (right, (156, 92, 8))):
            mx = sum(p[0] for p in pts) / 4
            my = sum(p[1] for p in pts) / 4
            inner = [(mx + (x - mx) * 0.52, my + (y - my) * 0.52) for x, y in pts]
            d.polygon(inner, outline=col, width=max(1, int(n * 0.012)))
    # bright top edges
    d = ImageDraw.Draw(cube)
    lw = max(1, int(n * (0.010 if detail else 0.016)))
    d.line([top[3], top[0], top[1]], fill=(255, 246, 222), width=lw, joint="curve")
    d.line([top[3], top[2], top[1]], fill=(255, 236, 190), width=lw, joint="curve")
    d.line([top[2], left[2]], fill=(255, 222, 160), width=lw)
    img.paste(cube, (0, 0), cmask)

    if detail:
        sp = ImageDraw.Draw(img)
        _sparkle(sp, n * 0.73, n * 0.25, n * 0.075, 255)
        _sparkle(sp, n * 0.27, n * 0.30, n * 0.040, 210)
        _sparkle(sp, n * 0.78, n * 0.62, n * 0.032, 170)

    # soft rim on the tile
    rim = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle([pad, pad, n - pad, n - pad], radius=int(n * 0.23),
                                          outline=(255, 255, 255, 38), width=max(1, int(n * 0.008)))
    img = Image.alpha_composite(img, rim)
    return img.resize((px, px), Image.LANCZOS)
