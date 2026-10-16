"""The real vote screen (captured 2026-10-13): three cards, map name over a
green VOTE button. The click must land on the button."""
import os

import pytest
from PIL import Image

from conftest import FIXTURES
from mayhem import engine, vote
from mayhem.ocr import Ocr

CLIENT = (0, 23, 1920, 1032)
FRAMES = {"vote_214329.jpg": ["Void", "Overworld", "Dreamland"],
          "vote_215354.jpg": ["Ocean", "Cave", "Arctic"],
          "vote_220420.jpg": ["Overworld", "Arctic", "Lava"]}
VOTES = {"vote_214329.jpg": [1, 0, 0], "vote_215354.jpg": [0, 0, 0],
         "vote_220420.jpg": [0, 0, 0]}


@pytest.mark.parametrize("name, maps", FRAMES.items())
def test_real_vote_screen_click_lands_on_the_green_button(name, maps):
    path = os.path.join(FIXTURES, "frames", name)
    if not os.path.exists(path):
        pytest.skip("fixture frames are local only")
    ocr = Ocr()
    if not ocr.available:
        pytest.skip("no Windows OCR")
    img = Image.open(path).convert("RGB").crop(CLIENT)
    lines = ocr.read(img, engine.VOTE_SCALE)
    opts = vote.find_options(lines, img.height, img.width)
    assert vote.is_vote_screen(opts)
    assert [o.name for o in opts] == maps                  # left to right
    # each card's own 'N Votes' count (what tells whether a click counted)
    assert [vote.card_votes(lines, o, img.height, img.width) for o in opts] == VOTES[name]
    for opt in opts:
        x, y = vote.click_point(opt, (0, 0))
        y += round(engine.VOTE_BUTTON_DY * img.height)
        # on the button: the area around the click is mostly the green button
        # (its white VOTE letters sit right in the middle)
        box = img.crop((int(x) - 60, int(y) - 15, int(x) + 60, int(y) + 15))
        px = list(box.getdata())
        green = sum(1 for r, g, b in px if g > r + 40 and g > b + 40)   # green, even when dimmed
        assert green / len(px) > 0.3, (opt.name, green / len(px))
    # ranking rules on the real options
    assert vote.choose(opts, ["Glitch", maps[2]]).name == maps[2]
    assert vote.choose(opts, ["Glitch"]).name == maps[0]
    assert vote.choose(opts, []).name == maps[0]
