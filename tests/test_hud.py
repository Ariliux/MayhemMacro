"""HUD parsers on tricky OCR strings, and the real Windows OCR on real frames.

The frame tests run Windows OCR for real (it only reads pictures from disk).
They skip when the fixtures aren't there (they are never committed) or the PC
has no OCR language.
"""
import logging
import os
import threading

import pytest
from PIL import Image

from mayhem import hud
from mayhem.hud import Banner, parse_banner, parse_storage, parse_timer
from mayhem.ocr import Ocr, OcrLine, OcrWord

FRAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "frames")
CLIENT_BOX = (0, 23, 1920, 1032)        # the Roblox client inside the full-screen frames


# ------------------------------------------------------------ parse_banner ---
@pytest.mark.parametrize("text, block, den, variant", [
    ("Resonance has spawned! (1 / 100,000)", "Resonance", 100000, "normal"),
    ("Chromatic Vexen has spawned! (1 / 2,500,000)", "Vexen", 2500000, "chromatic"),
    ("Inverted Relic has spawned! (1/1,000,000)", "Relic", 1000000, "inverted"),
    ("Resonance has spawned! (l / 100.000)", "Resonance", 100000, "normal"),
    ("Resonance has spawned! (I/100,000)", "Resonance", 100000, "normal"),
    ("Resonance has spawned! (1 / 1OO,OOO)", "Resonance", 100000, "normal"),
    ("Resonance has spawned! (1/100000)", "Resonance", 100000, "normal"),
    ("Resonance has spawned! (1 / 100 000)", "Resonance", 100000, "normal"),
    ("e) Resonance has spawned! (1 / 100,000)", "Resonance", 100000, "normal"),
    ("e Resonance has spawned! (1 / 100,000)", "Resonance", 100000, "normal"),
    ("|| ' Resonance has spawned! (1 / 100,000)", "Resonance", 100000, "normal"),
    ("O Resonance has spawned! (1 / 100,000)", "Resonance", 100000, "normal"),
    ("Resonance has spawnedl (1 / 100,000)", "Resonance", 100000, "normal"),
    ("Resonance has spawned1(1/100,000)", "Resonance", 100000, "normal"),
    ("A Chromatic Vexen has spawned! (1 / 2,500,000)", "Vexen", 2500000, "chromatic"),
    ("SNAKEBLOCK has spawned! (1 / 505,000)", "SNAKEBLOCK", 505000, "normal"),
    ("Luminescent Lantern has spawned! (1 / 50,000)", "Luminescent Lantern", 50000, "normal"),
    ("Vexen (Chromatic) has spawned! (1 / 2,500,000)", "Vexen", 2500000, "chromatic"),
    ("[Inverted] Relic has spawned! (1 / 1,000,000)", "Relic", 1000000, "inverted"),
    ("lnverted Relic has spawned! (1 / 1,000,000)", "Relic", 1000000, "inverted"),
    ("CHROMATIC VEXEN has spawned! (1 / 2,500,000)", "VEXEN", 2500000, "chromatic"),
    ("Resonance has spawned!", "Resonance", None, "normal"),
    ("Resonance has spawned! (1 /", "Resonance", None, "normal"),
])
def test_parse_banner_spawns(text, block, den, variant):
    b = parse_banner(text)
    assert b == Banner(text.strip(), block, den, variant)


@pytest.mark.parametrize("text", [
    "Necromancer has activated Ritual and generated a Epic block!",
    "",
    "   ",
    "has spawned! (1 / 100,000)",
    "Map changed to Dreamland",
    "Collect 1500 blocks!",
])
def test_parse_banner_not_a_spawn(text):
    assert parse_banner(text) is None


def test_parse_banner_lone_variant_word_is_the_name():
    # A block really called 'Chromatic' must not end up with an empty name.
    b = parse_banner("Chromatic has spawned! (1 / 10,000)")
    assert (b.block, b.variant, b.denominator) == ("Chromatic", "normal", 10000)


# -------------------------------------------------------------- parse_timer ---
@pytest.mark.parametrize("lines, secs", [
    (["08:31"], 511),
    (["e)", "08:19"], 499),               # the globe icon reads as 'e)'
    (["06:52 3"], 412),
    (["1:23"], 83),
    (["01:23"], 83),
    (["08 : 31"], 511),
    (["O8:31"], 511),                     # letter O for zero
    (["1.07M 1.1K 215/350 2,550% 08:31"], 511),
    (["2,550%", "07:33"], 453),
    (["10:00"], 600),
    (["00:00"], 0),
])
def test_parse_timer(lines, secs):
    assert parse_timer(lines) == secs


@pytest.mark.parametrize("lines", [
    [],
    ["2,550%"],
    ["e)"],
    ["215/350"],
    ["1.07M"],
    ["07.33"],                            # ':' misread: not trusted on its own
    ["57:33"],                            # longer than the vote timer ever runs
    ["08:61"],
    ["123:45"],
    ["08:315"],
])
def test_parse_timer_rejects(lines):
    assert parse_timer(lines) is None


# ------------------------------------------------------------ parse_storage ---
@pytest.mark.parametrize("lines, want", [
    (["215/350"], (215, 350)),
    (["216 / 350"], (216, 350)),
    (["e)", "229/350"], (229, 350)),
    (["1.07M 1.1K 215/350 2,550% 08:31"], (215, 350)),
    (["2l6/350"], (216, 350)),
    (["350/350"], (350, 350)),
    (["0/350"], (0, 350)),
    (["1,200/2,000"], (1200, 2000)),
])
def test_parse_storage(lines, want):
    assert parse_storage(lines) == want


@pytest.mark.parametrize("lines", [[], ["2,550%"], ["08:31"], ["350"], ["2161350"], ["5/0"]])
def test_parse_storage_rejects(lines):
    assert parse_storage(lines) is None


# --------------------------------------------------------------- parse_gems ---
@pytest.mark.parametrize("lines, want", [
    (["1.1K"], 1100),
    (["12.86K"], 12860),
    (["950"], 950),
    (["0"], 0),
    (["1.07M"], 1_070_000),
    (["2.5B"], 2_500_000_000),
    (["1.1k"], 1100),
    (["1.1 K"], 1100),
    (["1.1K O"], 1100),                   # the green + button reads as 'O'
    (["1.1KO"], 1100),                    # ...or glued on
    (["e)", "1.5K"], 1500),
    (["l.5K"], 1500),                     # letter l for one
    (["1.O5K"], 1050),                    # letter O for zero
    (["1,1K"], 1100),                     # a comma for the point
    (["9.82K"], 9820),
    (["1,234"], 1234),
    (["15K"], 15000),
    (["999.99K"], 999_990),
])
def test_parse_gems(lines, want):
    got = hud.parse_gems(lines)
    assert got == want and type(got) is int


@pytest.mark.parametrize("lines", [
    [], [""], ["O"], ["e)"], ["1.1"],      # a point without K/M/B: the K got lost
    ["08:31"], ["215/350"], ["2,550%"],
])
def test_parse_gems_rejects(lines):
    assert hud.parse_gems(lines) is None


@pytest.mark.parametrize("text, want", [
    ("Not enough gems!", True), ("NOT ENOUGH GEMS", True), ("notenough", True),
    ("N0t en0ugh gems", True), ("You need more gems", False), ("Roll", False), ("", False)])
def test_says_not_enough(text, want):
    assert hud.says_not_enough([text]) is want


class BoxOcr:
    """A whole-client read: each (text, x, y) is one line with one word box."""

    def __init__(self, lines):
        self.lines = lines

    def read(self, img, scale=2.0):
        return [OcrLine(t, (OcrWord(t, x, y, 120.0, 24.0),)) for t, x, y in self.lines]


def test_not_enough_on_screen_but_not_in_the_chat():
    img = Image.new("RGB", (1920, 1009))
    in_chat = ("Player1: not enough gems lol", 40.0, 200.0)       # top left: the chat
    on_screen = ("Not enough gems!", 900.0, 500.0)
    assert hud.not_enough_shown(BoxOcr([in_chat]), img) is False
    assert hud.not_enough_shown(BoxOcr([in_chat, on_screen]), img) is True
    assert hud.not_enough_shown(BoxOcr([("Roll", 900.0, 500.0)]), img) is False


def test_read_gems_needs_two_reads_to_agree():
    ocr = FakeOcr(["1.1K", "11K", "1.1K"])
    assert hud.read_gems(ocr, Image.new("RGB", (1920, 1009))) == 1100
    assert hud.read_gems(FakeOcr(["1.1K"]), Image.new("RGB", (1920, 1009))) is None


# ------------------------------------------------------------- crop/regions ---
def test_crop_uses_client_fractions():
    img = Image.new("RGB", (200, 100))
    assert hud.crop(img, (0.25, 0.5, 0.75, 1.0)).size == (100, 50)
    assert hud.crop(img, (0.0, 0.0, 1.0, 1.0)).size == (200, 100)


def test_regions_are_sane_fractions():
    assert {"top", "banner", "timer", "storage"} <= set(hud.REGIONS)
    for name, (x0, y0, x1, y1) in hud.REGIONS.items():
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0, name
    # The pills sit inside the HUD row they are cut from.
    top = hud.REGIONS["top"]
    for name in ("timer", "storage"):
        r = hud.REGIONS[name]
        assert top[0] <= r[0] and r[2] <= top[2] and top[1] <= r[1] and r[3] <= top[3]


class FakeOcr:
    """Answers read() from a list, one answer per call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def read(self, img, scale=2.0):
        self.calls += 1
        text = self.answers.pop(0) if self.answers else ""
        return [OcrLine(t, ()) for t in text.split("|") if t]


def test_read_top_needs_two_reads_to_agree():
    # Timer: a misread first, then two agreeing reads. Storage: agrees at once.
    ocr = FakeOcr(["01:33", "07:33", "07:33", "229/350", "229/350"])
    top = hud.read_top(ocr, Image.new("RGB", (1920, 1009)))
    assert (top.timer, top.storage) == (453, (229, 350))
    assert "01:33" in top.lines and "07:33" in top.lines


def test_read_top_gives_none_rather_than_a_guess():
    ocr = FakeOcr(["08:31"])          # one read, then nothing ever again
    top = hud.read_top(ocr, Image.new("RGB", (1920, 1009)))
    assert top.timer is None and top.storage is None


def test_read_banner_joins_a_wrapped_banner():
    ocr = FakeOcr(["Inverted Relic has spawned!|(1 / 6,000,000)"])
    lines, b = hud.read_banner(ocr, Image.new("RGB", (1920, 1009)))
    assert lines == ["Inverted Relic has spawned!", "(1 / 6,000,000)"]
    assert (b.block, b.denominator, b.variant) == ("Relic", 6000000, "inverted")


def test_a_wrapped_banner_is_checked_against_the_table_too():
    # '(1 / 250' lost its ',000' on the wrapped line: Lucid is 1 in 250,000.
    ocr = FakeOcr(["Lucid has spawned!|(1 / 250)"])
    _, b = hud.read_banner(ocr, Image.new("RGB", (1920, 1009)))
    assert (b.block, b.denominator) == ("Lucid", 250_000)


class BandOcr:
    """The banner strip's read at 2x gives `band` (OcrLines with boxes); a
    zoomed re-read of a small line (scale 5) gives `zoom`."""

    def __init__(self, band, zoom=""):
        self.band, self.zoom = band, zoom
        self.scales = []

    def read(self, img, scale=2.0):
        self.scales.append(scale)
        if scale == 5.0:
            return [OcrLine(self.zoom, ())] if self.zoom else []
        return self.band


def _ocr_line(text, y, h):
    """One banner line in the strip, its letters `h` px tall."""
    words, x = [], 300.0
    for t in text.split():
        words.append(OcrWord(t, x, y, 12.0 * len(t), h))
        x += 12.0 * len(t) + 8
    return OcrLine(text, tuple(words))


FULL_H = 28.0          # full-size banner letters in a 1009 px client
TINY_H = 11.0          # a banner shrinking away


def test_a_zoomed_small_read_is_not_undone_by_the_joined_lines():
    # A worker picked the block up at once: the spawn line is tiny, the
    # pickup line full size. The 5x zoom reads it right; the run-together
    # 2x text must not replace that read.
    band = [_ocr_line("Sporedelic has spawned! (1 / 100,000)", 40, TINY_H),
            _ocr_line("Void King just picked up a Inverted Sporedelic block!", 80, FULL_H)]
    ocr = BandOcr(band, zoom="Inverted Sporedelic has spawned! (1 / 1,000,000)")
    _, b = hud.read_banner(ocr, Image.new("RGB", (1920, 1009)))
    assert (b.block, b.variant, b.denominator, b.small) == \
        ("Sporedelic", "inverted", 1_000_000, True)


def test_a_small_misread_number_stays_snapped_next_to_another_banner():
    band = [_ocr_line("Lucid has spawned! (1 / 250)", 40, TINY_H),
            _ocr_line("Necromancer has activated Ritual!", 80, FULL_H)]
    _, b = hud.read_banner(BandOcr(band), Image.new("RGB", (1920, 1009)))
    assert (b.block, b.denominator, b.small) == ("Lucid", 250_000, True)


def test_a_zoomed_read_is_checked_against_the_table():
    band = [_ocr_line("Lucid has spawned! (1 / 250)", 40, TINY_H)]
    _, b = hud.read_banner(BandOcr(band, zoom="Lucid has spawned! (1 / 250)"),
                           Image.new("RGB", (1920, 1009)))
    assert (b.block, b.denominator, b.small) == ("Lucid", 250_000, True)


# ---------------------------------------------------------------------- ocr ---
def test_ocr_never_raises_and_reports_why_when_off():
    o = Ocr()
    o._tried, o._engine, o._error = True, None, "no language"
    assert o.available is False
    assert o.error == "no language"
    assert o.read(Image.new("RGB", (50, 20))) == []


def test_ocr_failure_returns_empty_and_logs_once(caplog):
    o = Ocr()
    o._tried, o._engine = True, object()          # an 'engine' that can't recognise
    with caplog.at_level(logging.WARNING, logger="mayhem.ocr"):
        assert o.read(Image.new("RGB", (50, 20))) == []
        assert o.read(Image.new("RGB", (50, 20))) == []
    assert len([r for r in caplog.records if r.name == "mayhem.ocr"]) == 1


def test_ocr_close_is_safe_and_reading_still_works():
    o = Ocr()
    o.close()                                      # nothing open yet: fine
    blank = Image.new("RGB", (200, 60), "white")
    assert o.read(blank) == []
    o.close()
    o.close()
    assert o.read(blank) == []                     # a new loop is made as needed


# ------------------------------------------------- real OCR on real frames ---
_OCR = Ocr()
needs_frames = pytest.mark.skipif(
    not os.path.isfile(os.path.join(FRAMES, "215059.jpg")) or not _OCR.available,
    reason="fixture frames or Windows OCR not available")


def _client(name: str) -> Image.Image:
    with Image.open(os.path.join(FRAMES, name + ".jpg")) as im:
        return im.convert("RGB").crop(CLIENT_BOX)


FRAME_TRUTH = {
    # frame: (timer secs, storage, banner (block, denominator, variant) or None)
    "215059": (511, (215, 350), ("Resonance", 100000, "normal")),
    "215112": (499, (216, 350), None),
    "215124": (487, (216, 350), None),
    "215157": (453, (229, 350), None),
    "215239": (412, (227, 350), None),
}


@needs_frames
@pytest.mark.parametrize("frame", sorted(FRAME_TRUTH))
def test_frames_top_hud(frame):
    timer, storage, _ = FRAME_TRUTH[frame]
    top = hud.read_top(_OCR, _client(frame))
    assert (top.timer, top.storage) == (timer, storage), top.lines


@needs_frames
@pytest.mark.parametrize("frame", sorted(FRAME_TRUTH))
def test_frames_banner(frame):
    want = FRAME_TRUTH[frame][2]
    lines, b = hud.read_banner(_OCR, _client(frame))
    if want is None:
        assert b is None, lines
    else:
        assert b is not None and (b.block, b.denominator, b.variant) == want, lines
        assert b.text == "Resonance has spawned! (1 / 100,000)"
    if frame == "215112":            # not a spawn, but the text is kept for the log
        assert any("Necromancer has activated Ritual" in t for t in lines)


@needs_frames
def test_frames_boxes_are_in_crop_pixels():
    band = hud.crop(_client("215059"), hud.REGIONS["banner"])
    for scale in (1.5, 2.0, 3.0):
        lines = _OCR.read(band, scale)
        words = [w for ln in lines for w in ln.words]
        res = next(w for w in words if w.text == "Resonance")
        # Measured: banner text at client y 842-872; the band starts at 72%
        # of the client (y 726) so stacked banners fit.
        top = 842 - round(hud.REGIONS["banner"][1] * 1009)
        assert top - 12 <= res.y <= top + 12 and 20 <= res.h <= 40, (scale, res)
        left = 687 - round(hud.REGIONS["banner"][0] * 1920)
        assert left - 15 <= res.x <= left + 15, (scale, res)


@needs_frames
def test_frames_ocr_from_several_threads():
    """The run loop and the UI can read at the same time: one lock, same answers."""
    band = hud.crop(_client("215059"), hud.REGIONS["banner"])
    want = [ln.text for ln in _OCR.read(band)]
    got: list[list[str]] = []

    def worker():
        for _ in range(3):
            got.append([ln.text for ln in _OCR.read(band)])

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert got == [want] * 9


@needs_frames
@pytest.mark.parametrize("region", ["timer", "storage", "banner"])
def test_frames_regions_have_margin(region, monkeypatch):
    """Every edge of every region can move 1% either way and still read."""
    base = hud.REGIONS[region]
    frames = {k: _client(k) for k in ("215059", "215112", "215157", "215239")}
    for edge in range(4):
        for delta in (-0.01, 0.01):
            moved = list(base)
            moved[edge] += delta
            monkeypatch.setitem(hud.REGIONS, region, tuple(moved))
            for frame, img in frames.items():
                timer, storage, banner = FRAME_TRUTH[frame]
                if region == "banner":
                    _, b = hud.read_banner(_OCR, img)
                    got = None if b is None else (b.block, b.denominator, b.variant)
                    assert got == banner, (frame, moved)
                else:
                    top = hud.read_top(_OCR, img)
                    got = top.timer if region == "timer" else top.storage
                    assert got == (timer if region == "timer" else storage), (frame, moved, top.lines)


# The gem counter on the same frames, checked by eye: 1.1K, then 1.5K three
# times (one with a red glow behind the pill), then 1.6K.
GEM_TRUTH = {"215059": 1100, "215112": 1500, "215124": 1500, "215157": 1500, "215239": 1600}


@needs_frames
def test_frame_215059_gems_read_1_1k():
    client = _client("215059")
    lines = [ln.text for ln in _OCR.read(hud.crop(client, hud.REGIONS["gems"]), 4.0)]
    assert hud.parse_gems(lines) == 1100, lines
    assert hud.read_gems(_OCR, client) == 1100


@needs_frames
@pytest.mark.parametrize("frame", sorted(GEM_TRUTH))
def test_frames_gems(frame):
    client = _client(frame)
    assert hud.read_gems(_OCR, client) == GEM_TRUTH[frame]
    assert hud.not_enough_shown(_OCR, client) is False


@needs_frames
def test_frames_gems_region_has_margin(monkeypatch):
    """Every edge of the gem pill's region can move 1.2% either way and
    still read: never the diamond's outline or the + button as a digit."""
    base = hud.REGIONS["gems"]
    frames = {k: _client(k) for k in GEM_TRUTH}
    for edge in range(4):
        for delta in (-0.012, -0.01, 0.01, 0.012):
            moved = list(base)
            moved[edge] += delta
            monkeypatch.setitem(hud.REGIONS, "gems", tuple(moved))
            for frame, img in frames.items():
                assert hud.read_gems(_OCR, img) == GEM_TRUTH[frame], (frame, moved)


def test_chat_finds_survive_wrapping_and_lost_words():
    # Real OCR of a 1080p chat (2026-10-18): each find wrapped over lines,
    # "with" dropped and "chance" mangled around the colored words.
    lines = ["RedAriliux has collected a Resonmce", "a 1 / 100,000", "�mce!",
             "(GLOBAL] Unknom: wwin08 has collected a Inverted Arboric", "a 1 / 1,000,000,000"]
    got = [(f.who, f.variant, f.block, f.denominator) for f in hud.parse_collects(lines)]
    assert got == [("RedAriliux", "normal", "Resonance", 100_000),
                   ("wwin08", "inverted", "Arboric", 1_000_000_000)]
    assert hud.same_player("RedAriliux", "RealAriliux")


def test_chat_is_read_color_blind():
    from PIL import Image
    img = Image.new("RGB", (4, 1))
    img.putpixel((1, 0), (255, 0, 200))      # pink letter
    img.putpixel((2, 0), (40, 255, 60))      # green letter
    out = hud.chat_image(img)
    assert out.getpixel((0, 0))[0] > 200     # dark background -> white
    assert out.getpixel((1, 0))[0] < 50 and out.getpixel((2, 0))[0] < 50   # any bright color -> black


@pytest.mark.parametrize("line, want", [
    ("Player1 has collected a Primorddk• with a 1 / 111,111,111 chance!", ("normal", "Primordial", 111_111_111)),
    ("Player1 has collected a Voi&inpc with a 1 / 1,000,000 chance!", ("normal", "Voidrinyx", 1_000_000)),
    ("Player1 has collected a Erup€on wit' a 1 / 100,000 chance!", ("normal", "Eruption", 100_000)),
    ("Player1 has collected a Resonmce wi&t a 1 / 100,000 chance!", ("normal", "Resonance", 100_000)),
    ("Player1 has collected a Doredis wit' a 1 / 3,500,000 chance!", ("normal", "Borealis", 3_500_000)),
    ("Player1 has collected a Tempcrid with a 1 / 500,000 chance!", ("normal", "Temporal", 500_000)),
    ("Player1 has collected a SNAKEDECK with a 1 / 505,000 chance!", ("normal", "SNAKEBLOCK", 505_000)),
    ("Player1 has collected a Inverted Thermos with a chance!", None),
])
def test_garbled_chat_names_snap_to_the_blocks_of_their_rarity(line, want):
    got = [(f.variant, f.block, f.denominator) for f in hud.parse_collects([line])]
    assert got == ([want] if want else [])


def test_a_bare_variant_word_is_not_a_find():
    assert hud.parse_collects(["Player1 has collected a Inverted with a 1 / 1,000,000 chance!"]) == []


@pytest.mark.parametrize("text, variant", [
    ("Invgted Evergreen has spawned! (1 / 1,000,000)", "inverted"),
    ("Inveryed Evergreen has spawned! (1 / 1,000,000)", "inverted"),
    ("gyomatic Evergreen has spawned! (1 / 2,500,000)", "chromatic"),
    ("lhverted Lucid has spawned!", "inverted"),
])
def test_misread_variant_words_in_banners(text, variant):
    b = hud._checked(hud.parse_banner(text))
    assert (b.block, b.variant) == (b.block if b.block != "Evergreen" else "Evergreen", variant)
    assert b.block in ("Evergreen", "Lucid")


def test_a_pickup_with_no_block_name_is_skipped():
    assert hud.parse_pickup("You just picked up a Chromatic block!") is None
    assert hud.parse_pickup("You just picked up a Inverted block!") is None


@pytest.mark.parametrize("text, want", [
    ("Inverted a Legendar Diamond has spawned! (1 / 100,000)", ("Diamond", "inverted", 1_000_000)),
    ("A Legendary Ignityx has spawned! (1 / 10,000,000)", ("Ignityx", "normal", 10_000_000)),
    ("Mythical Chromatic Relic has spawned!", ("Relic", "chromatic", None)),   # no number on screen
])
def test_rarity_words_in_banners_are_dropped(text, want):
    b = hud._checked(hud.parse_banner(text))
    assert (b.block, b.variant, b.denominator) == want
