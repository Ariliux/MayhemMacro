"""Draws MayhemMacro's app icon: a glowing amber block on a dark rounded tile.

    .venv\\Scripts\\python.exe tools\\make_icon.py [preview.png]

Writes mayhemmacro.ico (16-256 px) next to mayhemmacro.py. Small sizes are
drawn separately with fewer details so they stay crisp in the taskbar.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image  # noqa: E402

from mayhem.icon import render  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    big = [render(s) for s in (256, 128, 64, 48)]
    small = [render(s, detail=False) for s in (32, 24, 16)]
    out = os.path.join(HERE, "mayhemmacro.ico")
    big[0].save(out, format="ICO", sizes=[(i.width, i.height) for i in big + small],
                append_images=big[1:] + small)
    if len(sys.argv) > 1:
        sheet = Image.new("RGBA", (256 + 128 + 64 + 48 + 32 + 16 + 70, 256), (40, 44, 52, 255))
        x = 0
        for im in [big[0], big[1], big[2], big[3], small[0], small[2]]:
            sheet.alpha_composite(im, (x, (256 - im.height) // 2))
            x += im.width + 14
        sheet.save(sys.argv[1])
    print("wrote", out)


if __name__ == "__main__":
    main()
