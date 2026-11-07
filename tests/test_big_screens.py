"""Bigger 16:9 screens: the real fixture frames blown up to 2560x1440 and
3840x2160 must read and click exactly like they do on 1920x1080. A Roblox
picture taller than 1080 is shrunk to 1080 before it's read (engine.VIEW_H),
and every click is scaled back up. All fakes - nothing is clicked for real."""
import os

import pytest
from PIL import Image

from mayhem import engine as engine_mod, hud
from test_engine import CLIENT_RECT, FRAMES, Rig, real_ocr  # noqa: F401  (fixture)

SCALES = {"1440p": 4 / 3, "4K": 2.0}


def big(name: str, s: float) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """A fixture frame and its Roblox client rect, as on a screen s times bigger."""
    path = os.path.join(FRAMES, name)
    if not os.path.isfile(path):
        pytest.skip(f"{name} is missing")
    with Image.open(path) as img:
        frame = img.convert("RGB")
    frame = frame.resize((round(frame.width * s), round(frame.height * s)), Image.LANCZOS)
    rect = tuple(round(v * s) for v in CLIENT_RECT)
    return frame, rect


def rig_on(real_ocr, name: str, s: float, **settings) -> Rig:
    r = Rig(**settings)
    r.window.frame, r.window.rect = big(name, s)
    r.deps.ocr = real_ocr
    return r.start()


# ---- the scale itself ---------------------------------------------------------
def test_1080p_is_never_resized():
    assert engine_mod.view_scale((0, 23, 1920, 1009)) == 1.0        # windowed
    assert engine_mod.view_scale((0, 0, 1920, 1080)) == 1.0         # fullscreen
    assert engine_mod.view_scale((0, 0, 1280, 720)) == 1.0          # smaller: left alone
    assert engine_mod.view_scale(None) == 1.0
    assert engine_mod.Engine._real((0, 23, 1920, 1009), 700, 400) == (700, 423)


def test_bigger_pictures_scale_to_1080_and_clicks_scale_back():
    assert engine_mod.view_scale((0, 0, 3840, 2160)) == 2.0
    assert engine_mod.view_scale((0, 0, 2560, 1440)) == pytest.approx(4 / 3)
    assert engine_mod.Engine._real((0, 0, 3840, 2160), 960, 540) == (1920, 1080)
    assert engine_mod.Engine._real((100, 46, 2560, 1440), 300, 300) == (500, 446)


def test_the_picture_the_engine_reads_is_1080_tall():
    r = Rig().start()
    r.window.rect = (0, 0, 3840, 2160)
    r.window.frame = Image.new("RGB", (3840, 2160), (40, 90, 30))
    assert r.engine._see(r.window.rect).size == (1920, 1080)
    r.window.rect, r.window.frame = (0, 23, 1920, 1009), Image.new("RGB", (1920, 1080))
    assert r.engine._see(r.window.rect).size == (1920, 1009)


# ---- real frames, real OCR ------------------------------------------------------
@pytest.mark.parametrize("label", SCALES)
def test_banner_timer_and_storage_read_the_same(real_ocr, label):
    r = rig_on(real_ocr, "215059.jpg", SCALES[label])
    r.tick()
    [ev] = r.of("rare")
    assert (ev["block"], ev["denominator"], ev["variant"]) == ("Resonance", 100000, "normal")
    assert ev["picture"].height == 1080          # the shrunk picture is what's saved
    hud_ev = r.of("hud")[-1]
    assert hud_ev["timer"] == 511 and hud_ev["storage"] == [215, 350]


@pytest.mark.parametrize("label", SCALES)
def test_a_second_frame_reads_the_same(real_ocr, label):
    r = rig_on(real_ocr, "215112.jpg", SCALES[label])
    r.tick()
    hud_ev = r.of("hud")[-1]
    assert hud_ev["timer"] == 499 and hud_ev["storage"] == [216, 350]


@pytest.mark.parametrize("label", SCALES)
@pytest.mark.parametrize("name", ["vote_214329.jpg", "vote_215354.jpg", "vote_220420.jpg"])
def test_the_vote_click_lands_on_the_green_button(real_ocr, label, name):
    r = rig_on(real_ocr, name, SCALES[label], auto_vote=True)
    r.engine._start_vote(r.clock.t)
    r.tick(6)
    clicks = r.inputs.clicks()
    assert clicks, r.logs("warn")
    _, x, y = clicks[0]
    s = SCALES[label]
    box = r.window.frame.crop((int(x - 60 * s), int(y - 15 * s), int(x + 60 * s), int(y + 15 * s)))
    px = list(box.getdata())
    green = sum(1 for cr, cg, cb in px if cg > cr + 40 and cg > cb + 40)
    assert green / len(px) > 0.3, (x, y, green / len(px))


@pytest.mark.parametrize("label", SCALES)
def test_the_pinned_arrow_click_is_scaled_to_the_real_screen(label):
    path = os.path.join(FRAMES, "pinned_closed_arctic.jpg")
    if not os.path.isfile(path):
        pytest.skip("pinned_closed_arctic.jpg is missing")
    s = SCALES[label]
    with Image.open(path) as img:
        pic = img.convert("RGB")
    assert hud.find_pinned_arrow(pic) is not None             # found on 1080p
    r = Rig().start()
    r.window.rect = (0, 0, round(pic.width * s), round(pic.height * s))
    r.window.frame = pic.resize(r.window.rect[2:], Image.LANCZOS)
    r.engine._open_pinned(r.engine._look(r.clock.t))
    [(_, x, y)] = r.inputs.clicks()[:1]
    assert abs(x - 1887 * s) <= 4 * s and abs(y - 385 * s) <= 4 * s, (x, y)
