"""Start-of-run setup: find and click the pinned recipe's arrow, centre the
cursor. Frames: the user's real screenshot with the panel open (2026-10-14),
and a closed-panel frame made by mirroring that real arrow to the right edge."""
import os

import pytest
from PIL import Image, ImageOps

from conftest import FIXTURES
from mayhem import hud
from test_engine import DC, ENTERED, JOIN, LOADED, NEW_LOG, Rig

FRAMES = os.path.join(FIXTURES, "frames")
OPEN = os.path.join(FRAMES, "pinned_open_fullscreen.png")
NO_PANEL = os.path.join(FRAMES, "vote_214329.jpg")


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("fixture frames are local only")


def closed_frame() -> Image.Image:
    """A 1920x1080 screen whose right edge has the ◀ button (the real ▶,
    mirrored), as when the pinned recipe is closed."""
    button = Image.open(OPEN).convert("RGB").crop((1552, 360, 1598, 406))
    frame = Image.open(NO_PANEL).convert("RGB")
    frame.paste(ImageOps.mirror(button), (1855, 370))
    return frame


def test_finds_the_real_arrow_and_not_the_letters():
    _need(OPEN)
    x, y = hud.find_pinned_arrow(Image.open(OPEN))
    assert abs(x - 1575) <= 3 and abs(y - 382) <= 3


def test_finds_the_closed_arrow_at_the_right_edge():
    _need(OPEN, NO_PANEL)
    client = closed_frame().crop((0, 23, 1920, 1032))
    x, y = hud.find_pinned_arrow(client)
    assert abs(x - 1878) <= 4 and abs(y - (393 - 23)) <= 4


def test_no_arrow_on_a_vote_screen():
    _need(NO_PANEL)
    assert hud.find_pinned_arrow(Image.open(NO_PANEL).convert("RGB").crop((0, 23, 1920, 1032))) is None


def _rig(**kw):
    r = Rig(open_pinned=True, center_cursor=True, start_jump=True, **kw)
    r.window.screen_size = lambda: (1920, 1080)
    return r


def test_setup_clicks_the_arrow_then_centres_the_cursor():
    _need(OPEN, NO_PANEL)
    r = _rig()
    r.window.frame = closed_frame()
    r.ocr.script["pinned"] = lambda: ["Pinned Recipe"] if r.inputs.clicks() else []
    r.start()
    r.tick()
    [(_, x, y)] = r.inputs.clicks()
    assert abs(x - 1878) <= 4 and abs(y - 393) <= 4          # screen pixels
    assert ("move", 960, 540) in r.inputs.calls
    assert "Opened the pinned recipe." in r.logs("info")
    order = [c[0] for c in r.inputs.calls]                  # arrow, then middle, then jump
    assert order.index("click") < order.index("move") < order.index("space")
    r.tick(5)
    assert len(r.inputs.clicks()) == 1                         # once per run


def test_setup_leaves_an_open_panel_alone():
    r = _rig()
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.start()
    r.tick()
    assert r.inputs.clicks() == []
    assert "The pinned recipe is already open." in r.logs("info")


def test_setup_runs_again_after_a_rejoin():
    r = _rig()
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.start()
    r.tick()
    moves = [c for c in r.inputs.calls if c[0] == "move"]
    r.tail.feed(JOIN(), ENTERED, LOADED)
    r.tick(2)
    assert len([c for c in r.inputs.calls if c[0] == "move"]) == len(moves) + 1


def test_setup_after_a_join_you_made_waits_until_you_are_away():
    # You hopped servers (or pressed Reconnect) and are playing: a 1.5 s
    # pause must not get you a cursor warp and a jump.
    r = _rig()
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.start()
    r.tick()
    calls = len(r.inputs.calls)
    r.tail.feed(JOIN(), ENTERED, LOADED)
    r.clock.touch()
    for _ in range(20):                       # playing, with pauses of a few seconds
        r.tick(6)
        r.clock.touch()
    assert len(r.inputs.calls) == calls
    r.run(61)                                 # then you walk away
    assert ("move", 960, 540) in r.inputs.calls[calls:]


def test_setup_after_the_macros_own_rejoin_uses_the_5_s_rule():
    r = _rig()
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.start()
    r.tick()
    calls = len(r.inputs.calls)
    r.tail.feed(DC(277))
    r.tick()                                  # away an hour: it rejoins at once
    r.clock.touch()                           # ...and you come back while it loads
    r.tail.switch(NEW_LOG.format(1))
    r.tail.feed(JOIN(), ENTERED, LOADED)
    r.tick()
    assert r.engine._rj is None
    r.tick(8)                                 # 4.5 s since you touched the PC
    assert len(r.inputs.calls) == calls
    r.tick(2)                                 # 5.5 s
    assert ("move", 960, 540) in r.inputs.calls[calls:]


def test_no_setup_on_a_disconnected_session():
    r = _rig(auto_rejoin=False)
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.tail.feed(DC(277))
    r.start()
    r.run(5)
    assert r.inputs.calls == []


def test_an_open_panel_is_left_alone_even_when_its_header_isnt_read():
    # No text reading (no Windows OCR language) or a misread header: the ▶
    # arrow itself says the panel is open, and clicking it would close it.
    _need(OPEN)
    r = _rig()
    with Image.open(OPEN) as im:
        r.window.frame = im.convert("RGB")            # the whole picture is the client
    r.window.rect = (0, 0) + r.window.frame.size
    r.ocr.script["pinned"] = ["Plnned Reclpe"]
    r.start()
    r.tick()
    assert r.inputs.clicks() == []
    assert "The pinned recipe is already open." in r.logs("info")
    assert not any("didn't open" in t for t in r.logs("warn"))


def test_setup_waits_while_you_are_still_pressing_keys():
    r = _rig()
    r.ocr.script["pinned"] = ["Pinned Recipe"]
    r.start()
    r.clock.touch()                                            # the F1 press
    r.tick()
    assert [c for c in r.inputs.calls if c[0] == "move"] == []


def test_a_pickup_banner_fills_in_the_variant_of_a_shrunken_spawn_read():
    r = Rig()
    r.ocr.script["banner"] = lambda: (
        ["Luminescent has spawned! (1 / 1,000,000)"] if r.engine.deps.now() < r.t0 + 1
        else ["Void King just picked up a Inverted Luminescent block!"])
    r.start()
    r.t0 = r.engine.deps.now()
    import mayhem.hud as H
    real = H.read_banner

    def small(ocr, client):
        lines, b = real(ocr, client)
        return lines, (H.replace(b, small=True) if b else None)
    H.read_banner = small
    try:
        r.run(5)
    finally:
        H.read_banner = real
    [ev] = r.of("rare")
    assert (ev["block"], ev["variant"], ev["denominator"]) == ("Luminescent", "inverted", 1_000_000)


def test_finds_the_closed_arrow_in_arctic_snow():
    """The real closed-panel picture the macro saved on 2026-10-14 (Arctic:
    bright snow and sky all around the dark button)."""
    path = os.path.join(FRAMES, "pinned_closed_arctic.jpg")
    _need(path)
    x, y = hud.find_pinned_arrow(Image.open(path).convert("RGB"))
    assert abs(x - 1887) <= 3 and abs(y - 385) <= 3
