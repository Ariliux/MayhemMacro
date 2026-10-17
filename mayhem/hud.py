"""Reading Block Mayhem X's HUD: the vote timer, storage, gems and the spawn banner.

Everything is measured as a fraction of the Roblox CLIENT rect (the game
picture without the title bar), so it holds windowed or fullscreen. Only
1920x1080 at 100% scaling is tested; the fractions were tuned on real frames
where the client is 1920x1009 (maximised window, 23 px title bar).

Windows OCR is noisy on this font: '08:19' comes back with a stray 'e)' (the
globe icon), '216/350' as '216 / 350', ':' sometimes as '.'. The parsers below
expect that, and read_top() tries a few cleaned-up versions of each crop
before giving up on a value.
"""
import re
import difflib
from dataclasses import replace, dataclass, field
from typing import Any, Callable

from PIL import Image, ImageOps

from .ocr import Ocr, texts

# Client-rect fractions (x0, y0, x1, y1). "top" is the whole HUD row and the
# last resort; "timer" and "storage" are its two pills on their own, which OCR
# reads far more reliably than the whole row (the big 1.07M money text above
# them confuses it). Measured on the fixtures in client px: timer text x
# 1060-1110, y 102-127, globe icon right of it; storage text x 809-862, same
# rows; the money/gem boxes above end at y ~87; banner text y 842-872. Each
# crop keeps 35+ px sideways and 12+ px up/down around its text, so it still
# reads with every edge moved 1.2% either way.
from . import blocks

REGIONS = {
    "top": (0.25, 0.08, 0.75, 0.17),
    "timer": (0.5323, 0.0892, 0.6115, 0.1388),
    "storage": (0.3948, 0.0892, 0.4698, 0.1388),
    # The gem pill right of centre ('1.1K'): client x 1050-1196, y 26-92, between
    # the diamond icon (ends at x ~1053) and the green + button (starts at
    # x ~1199). Text measured at x 1085-1161, y 39-79, centred on x ~1123; it
    # still reads with every edge moved 1.2% either way.
    "gems": (0.5469, 0.0258, 0.6229, 0.0912),
    "banner": (0.15, 0.72, 0.85, 0.89),     # tall enough for 2-3 stacked banners
    "chat": (0.0, 0.04, 0.34, 0.46),        # Roblox chat, top left
    "pinned": (0.76, 0.24, 1.0, 0.50),      # the pinned recipe and its arrow, right side
}
# The vote timer counts down from 10:00. Anything past this is a misread
# ('57:33' for '07:33'), not a clock.
TIMER_MAX = 15 * 60


def crop(img: Image.Image, region: tuple[float, float, float, float]) -> Image.Image:
    """Cut a fractional region (x0, y0, x1, y1) out of `img` (the client picture)."""
    w, h = img.size
    x0, y0, x1, y1 = region
    box = (round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h))
    return img.crop(box)


# ---------------------------------------------------------------- banner ---
@dataclass(frozen=True)
class Banner:
    text: str                   # the line as read
    block: str                  # 'Resonance', 'Vexen' (variant word removed)
    denominator: int | None     # 100000 for '(1 / 100,000)', None if unreadable
    variant: str                # normal | inverted | chromatic
    small: bool = False     # read off shrunken text (a banner pushed up or popping in)


# No word break after it: the '!' often comes back glued on as 'l' or '1'.
_SPAWNED = re.compile(r"\bhas\s*spawn[e3]d", re.I)
# '(1 / 100,000)' after the spawn words. The '1' comes back as l, I, | or ! and
# sometimes not at all; the thousands separator as ',' '.' or a space.
_RARITY = re.compile(r"[\(\[{]?\s*[1lIi|!\]]?\s*[/\\]\s*(\d{1,3}(?:[,. ]?\d{3})*)(?!\d)")
_VARIANT_WORD = re.compile(r"^(?:[Il1|]nverted|chromatic)$", re.I)
_VARIANT_TAG = re.compile(r"[\(\[]\s*([Il1|]nverted|chromatic)\s*[\)\]]", re.I)
_NAME_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9'\-]*$")
NAME_WORDS_MAX = 4      # longest block names seen are two words


def _variant_of(word: str) -> str:
    return "chromatic" if word.lower().startswith("c") else "inverted"


def parse_banner(text: str) -> Banner | None:
    """'Resonance has spawned! (1 / 100,000)' -> Banner, anything else -> None.

    Tolerant of OCR noise: junk before the name ('e) Resonance ...'), '1/x'
    without spaces, 'l / 100.000'. Banners that are not spawns (the
    Necromancer 'has activated Ritual' line) give None.
    """
    text = (text or "").strip()
    m = _SPAWNED.search(text)
    if not m:
        return None
    head, tail = text[:m.start()], text[m.end():]

    variant = "normal"
    tag = _VARIANT_TAG.search(head)
    if tag:                                  # 'Vexen (Chromatic)' / '[Inverted] Relic'
        variant = _variant_of(tag.group(1))
        head = head[:tag.start()] + " " + head[tag.end():]

    # The name is the run of plain words right before 'has spawned'. Walking
    # back from there drops whatever OCR glued on in front ('e)', icons).
    words: list[str] = []
    for tok in reversed(head.split()):
        tok = tok.strip(":,;")
        if not _NAME_WORD.match(tok) or len(words) >= NAME_WORDS_MAX:
            break
        words.insert(0, tok)
    # A scrap in front ('e Resonance', 'O Resonance') is an icon, and 'A'/'The'
    # are not part of the name either.
    while len(words) > 1 and (len(words[0]) == 1 or words[0].lower() in ("an", "the")
                              or (len(words[0]) == 2 and words[0].islower())):
        words.pop(0)
    if not words:
        return None
    if len(words) > 1 and _VARIANT_WORD.match(words[0]):
        variant = _variant_of(words[0])
        words = words[1:]

    denominator = None
    # Letter O for zero only inside the rarity part, never in the name.
    r = _RARITY.search(tail.replace("O", "0").replace("o", "0"))
    if r:
        digits = re.sub(r"\D", "", r.group(1))
        if digits and int(digits) > 1:
            denominator = int(digits)
    return Banner(text, " ".join(words), denominator, variant)


# ----------------------------------------------------------- timer/storage ---
# Letters OCR gives for digits, fixed only when touching a digit or ':' so
# words like 'Lava' are left alone.
_NEAR_DIGIT = re.compile(r"(?<=[\d:/])[OoIl|]|[OoIl|](?=[\d:/])")
_DIGIT_FOR = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}
# The timer is the only 'mm:ss' on the HUD. Only a real ':' counts: also
# taking '07.33' let two misreads ('01.33' twice) agree on a wrong time, and
# the colon reads fine in at least two of read_top's tries anyway.
_CLOCK = re.compile(r"(?<![\d:.,])(\d{1,2})\s?:\s?(\d{2})(?![\d:])")
# 'n/m' with optional thousands commas; '2,550%' never fits.
_STORAGE = re.compile(r"(?<![\d,./])(\d{1,3}(?:,\d{3})*|\d+)\s*/\s*(\d{1,3}(?:,\d{3})*|\d+)(?![\d,%])")


def _digits_fixed(line: str) -> str:
    return _NEAR_DIGIT.sub(lambda m: _DIGIT_FOR[m.group(0)], line)


def parse_timer(lines: list[str]) -> int | None:
    """Seconds left on the vote timer ('08:31' -> 511), None if no clock is there."""
    for ln in lines:
        for m in _CLOCK.finditer(_digits_fixed(ln)):
            mins, secs = int(m.group(1)), int(m.group(2))
            if secs < 60 and mins * 60 + secs <= TIMER_MAX:
                return mins * 60 + secs
    return None


def parse_storage(lines: list[str]) -> tuple[int, int] | None:
    """Blocks in storage and the cap ('215/350' or '216 / 350' -> (216, 350))."""
    for ln in lines:
        for m in _STORAGE.finditer(_digits_fixed(ln)):
            n, cap = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            # A misread can glue digits together ('2161350'); a store holding
            # ten times its cap is that, not a real reading.
            if cap > 0 and n <= cap * 10:
                return n, cap
    return None


# -------------------------------------------------------------------- gems ---
# The gem counter: '950', '1.1K', '12.86K', '1.07M'. Letters for digits are
# fixed only next to a digit, '.' or ',' (so the + button's 'O' stays a
# letter), and a suffix may have junk glued after it ('1.1KO').
_GEM_NEAR = re.compile(r"(?<=[\d.,])[OoIl|]|[OoIl|](?=[\d.,])")
_GEMS = re.compile(r"(?<![\d.,:/])(\d{1,3}(?:,\d{3})+|\d+)(?:\s?[.,]\s?(\d{1,3}))?"
                   r"\s?([kKmMbB])?(?![\d.,%/:])")
_GEM_SCALE = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
GEMS_MAX = 10 ** 15


def parse_gems(lines: list[str]) -> int | None:
    """The gem count ('1.1K' -> 1100, '12.86K' -> 12860, '950' -> 950), None
    if no number is there. A decimal with no K/M/B after it ('1.1') is a
    misread (the K got lost), not a count."""
    for ln in lines:
        fixed = _GEM_NEAR.sub(lambda m: _DIGIT_FOR[m.group(0)], str(ln))
        for m in _GEMS.finditer(fixed):
            whole, frac = m.group(1).replace(",", ""), m.group(2) or ""
            suffix = (m.group(3) or "").lower()
            if frac and not suffix:
                continue
            # whole-number maths: '12.86K' is 1286 * 1000 // 100
            places = 10 ** len(frac)
            value = (int(whole) * places + int(frac or 0)) * _GEM_SCALE[suffix] // places
            if value <= GEMS_MAX:
                return value
    return None


def read_gems(ocr: Ocr, client: Image.Image) -> int | None:
    """The gem counter from the client picture: the first value two reads
    agree on, None when they never do."""
    return _agreed(ocr, crop(client, REGIONS["gems"]), {"g": parse_gems}, [])["g"]


_NOT_ENOUGH = re.compile(r"n[o0]t\s*en[o0]u?gh", re.I)


def says_not_enough(lines: list[str]) -> bool:
    """A 'Not enough gems!' kind of message."""
    return any(_NOT_ENOUGH.search(str(t)) for t in lines)


def not_enough_shown(ocr: Ocr, client: Image.Image) -> bool:
    """The game says 'not enough' somewhere on screen. The chat box is left
    out, so a player typing it doesn't count."""
    x0, y0, x1, y1 = REGIONS["chat"]
    w, h = client.size
    for ln in ocr.read(client, 1.0):
        if not says_not_enough([ln.text]):
            continue
        if ln.words:
            cx = sum(wd.cx for wd in ln.words) / len(ln.words)
            cy = sum(wd.cy for wd in ln.words) / len(ln.words)
            if x0 * w <= cx <= x1 * w and y0 * h <= cy <= y1 * h:
                continue
        return True
    return False


# ------------------------------------------------------------------ reads ---
@dataclass
class TopHud:
    timer: int | None
    storage: tuple[int, int] | None
    lines: list[str] = field(default_factory=list)


def _white_text(img: Image.Image, cut: int) -> Image.Image:
    """HUD text is white with a dark outline: keep only the near-white pixels,
    as black on white. Drops the icons and most of the world behind the pill."""
    return img.convert("L").point(lambda v: 0 if v >= cut else 255).convert("RGB")


_PREP = {
    "plain": lambda im: im,
    "gray": lambda im: im.convert("L").convert("RGB"),
    "contrast": lambda im: ImageOps.autocontrast(im.convert("L"), cutoff=1).convert("RGB"),
    "white": lambda im: _white_text(im, 200),
    "whiter": lambda im: _white_text(im, 225),
}
# Each pill is read several ways, in this order, until two reads AGREE. Swept
# on the fixtures with every region edge moved up to 1.2%: one read alone was
# wrong about 1 time in 100 ('227/350' as '2'0/050' with a bright glow behind
# the pill), two agreeing reads were right 810 times out of 810. A read is
# 3-9 ms and two are usually enough.
_TRIES = (("white", 4.0), ("plain", 4.0), ("contrast", 4.0), ("gray", 4.0),
          ("whiter", 2.0), ("plain", 3.0), ("plain", 2.0))
# The whole HUD row is big (960 px wide), so fewer, cheaper tries there.
_ROW_TRIES = (("plain", 3.0), ("white", 3.0), ("contrast", 3.0))


def _agreed(ocr: Ocr, img: Image.Image, parsers: dict[str, Callable[[list[str]], Any]],
            seen: list[str], tries: tuple[tuple[str, float], ...] = _TRIES) -> dict[str, Any]:
    """For each parser, the first value two reads of `img` agree on (None if
    they never do). Every line read is added to `seen`, for the log."""
    votes: dict[str, dict] = {name: {} for name in parsers}
    found: dict[str, Any] = {name: None for name in parsers}
    for how, scale in tries:
        read = texts(ocr.read(_PREP[how](img), scale))
        seen.extend(t for t in read if t not in seen)
        for name, parse in parsers.items():
            if found[name] is not None:
                continue
            value = parse(read)
            if value is not None:
                votes[name][value] = votes[name].get(value, 0) + 1
                if votes[name][value] >= 2:
                    found[name] = value
        if all(v is not None for v in found.values()):
            break
    return found


def read_top(ocr: Ocr, client: Image.Image) -> TopHud:
    """Vote timer and storage from the client picture. None for what can't be read."""
    seen: list[str] = []
    timer = _agreed(ocr, crop(client, REGIONS["timer"]), {"t": parse_timer}, seen)["t"]
    storage = _agreed(ocr, crop(client, REGIONS["storage"]), {"s": parse_storage}, seen)["s"]
    # Last resort: the whole HUD row, in case the pills moved a little. Same
    # two-reads-agree rule, so it never guesses.
    missing = {}
    if timer is None:
        missing["t"] = parse_timer
    if storage is None:
        missing["s"] = parse_storage
    if missing:
        row = _agreed(ocr, crop(client, REGIONS["top"]), missing, seen, _ROW_TRIES)
        timer = row.get("t", timer)
        storage = row.get("s", storage)
    return TopHud(timer, storage, seen)


@dataclass(frozen=True)
class Find:
    """A block that was collected: from chat ('X has collected a Y with a
    1 / N chance!') or a worker banner ('Void King just picked up a Y block!')."""
    block: str
    variant: str
    denominator: int | None
    how: str                    # chat | pickup
    who: str = ""               # the player (chat only)


# The block name is a few words with no '!' in it: the parts of the chat are
# joined into one string, and when one message's 'with a 1 / N chance' is
# misread, the name must not run on into the next message.
_COLLECT = re.compile(r"([A-Za-z0-9_]{3,20})\s+has\s+co[l1I]{2}ected\s+an?\s+"
                      r"((?:[^\s!]+\s+){0,3}?[^\s!]+)\s+with\s+an?\s+"
                      r"[1lI|]\s*/\s*([\dOo,. ]{1,16}?)\s*chance", re.I)
_PICKUP = re.compile(r"just\s+pi\w*\s+up\s+an?\s+(.+?)\s+\w*lo\w*k\b", re.I)


def _split_variant(name: str) -> tuple[str, str]:
    """'Chromatic Mosaic' -> ('Mosaic', 'chromatic'); OCR'd 'cnromatic' too."""
    words = name.split()
    if len(words) > 1:
        first = words[0].lower()
        for v in ("chromatic", "inverted"):
            if difflib.SequenceMatcher(None, first, v).ratio() >= 0.75:
                return " ".join(words[1:]), v
    # Glued together by OCR: 'Inverte#vergreen' -> ('Evergreen', 'inverted').
    k = re.sub(r"[^a-z]", "", name.lower())
    for v in ("chromatic", "inverted"):
        for cut in range(len(v) - 2, len(v) + 2):
            head, rest = k[:cut], k[cut:]
            if (len(rest) >= 3 and difflib.SequenceMatcher(None, head, v).ratio() >= 0.8
                    and blocks.is_known(rest)):
                return blocks.fix_name(rest), v
    return name, "normal"


def _number(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text.replace("O", "0").replace("o", "0"))
    return int(digits) if digits and int(digits) > 1 else None


def parse_collects(lines: list[str]) -> list[Find]:
    """Every 'has collected' chat line; wrapped lines are joined first."""
    text = " ".join(" ".join(str(t).split()) for t in lines)
    out = []
    for m in _COLLECT.finditer(text):
        block, variant = _split_variant(m.group(2).strip())
        block = blocks.fix_name(block)
        variant, den = blocks.snap(block, variant, _number(m.group(3)))
        out.append(Find(block, variant, den, "chat", m.group(1)))
    return out


# Letters OCR swaps in a Roblox name ('SomePIayer' for 'SomePlayer').
_NAME_FOLD = str.maketrans({"l": "i", "1": "i", "|": "i", "!": "i", "0": "o", "5": "s"})
SAME_PLAYER = 0.8           # difflib ratio of two folded names that are the same account


def _fold_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(name or "").lower()).translate(_NAME_FOLD)


def same_player(seen: str, name: str) -> bool:
    """Is `seen`, a name read off the chat, the account `name`? A letter or
    two may be misread; a clearly different name doesn't match."""
    a, b = _fold_name(seen), _fold_name(name)
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SAME_PLAYER


def parse_pickup(text: str) -> Find | None:
    m = _PICKUP.search(" ".join(str(text).split()))
    if not m:
        return None
    block, variant = _split_variant(m.group(1).strip())
    block = blocks.fix_name(block)
    return Find(block, variant, blocks.rarity_of(block, variant), "pickup")


def read_chat(ocr: Ocr, client: Image.Image) -> tuple[list[str], list[Find]]:
    lines = texts(ocr.read(crop(client, REGIONS["chat"]), 2.0))
    return lines, parse_collects(lines)


def esc_menu_open(ocr: Ocr, client: Image.Image) -> bool:
    """Roblox's own Esc menu is over the game: its 'Resume' button together
    with 'Leave' or 'Reset'. Words the game itself never shows together."""
    text = " ".join(texts(ocr.read(client, 1.0))).lower()
    return "resume" in text and ("leave" in text or "reset" in text or "respawn" in text)


def find_play(ocr: Ocr, client: Image.Image) -> tuple[int, int] | None:
    """The title screen's PLAY button: a line that says just 'Play' (never
    quest text like 'Play for 30 Minutes!'). Client pixels of its centre."""
    best = None
    for ln in ocr.read(client, 1.0):
        if ln.text.strip().strip("!>▶ ").lower() != "play" or not ln.words:
            continue
        w = max(ln.words, key=lambda w: w.h)
        if best is None or w.h > best[2]:
            best = (int(w.x + w.w / 2), int(w.y + w.h / 2), w.h)
    return None if best is None else (best[0], best[1])


def pinned_open(ocr: Ocr, client: Image.Image) -> bool:
    """The pinned recipe panel is showing ('Pinned Recipe' header)."""
    text = " ".join(texts(ocr.read(crop(client, REGIONS["pinned"]), 2.0))).lower()
    return "pinned" in text or "recipe" in text


def _has_hole(filled: set, h: int, w: int) -> bool:
    """True when some empty pixel inside the box can't reach the box's edge."""
    outside, stack = set(), []
    for y in range(h):
        for x in (0, w - 1):
            stack.append((y, x))
    for x in range(w):
        for y in (0, h - 1):
            stack.append((y, x))
    while stack:
        y, x = stack.pop()
        if (y, x) in outside or (y, x) in filled or not (0 <= y < h and 0 <= x < w):
            continue
        outside.add((y, x))
        stack += [(y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)]
    return len(outside) + len(filled) < h * w


def find_pinned_arrow(client: Image.Image) -> tuple[int, int] | None:
    """The pinned recipe's arrow button: a white triangle (~17x22 px at 1080p)
    on a dark square, in the right-hand strip. Client pixels of its centre,
    or None. Bright scenery (snow) fails the dark-square test."""
    found = pinned_arrow(client)
    return None if found is None else (found[0], found[1])


def pinned_arrow(client: Image.Image) -> tuple[int, int, str] | None:
    """find_pinned_arrow() plus which way the triangle points: 'left' (◀,
    the panel is closed: clicking opens it) or 'right' (▶, the panel is
    open: clicking would close it)."""
    import numpy as np
    x0, y0, x1, y1 = (int(round(f * s)) for f, s in zip(REGIONS["pinned"], client.size * 2))
    a = np.asarray(client.convert("RGB").crop((x0, y0, x1, y1))).astype(np.int16)
    # Bright pixels: the white arrow, its grey edge and the light-blue tint the
    # closed (◀) arrow has on one side.
    lum = a[..., 0] * 0.3 + a[..., 1] * 0.59 + a[..., 2] * 0.11
    white = (lum > 165) | ((a[..., 1] > 170) & (a[..., 2] > 170) & (a[..., 0] > 60))
    h, w = white.shape
    seen = np.zeros_like(white)
    best = None
    scale = client.size[1] / 1042
    for sy, sx in zip(*np.nonzero(white)):
        if seen[sy, sx]:
            continue
        stack, pts = [(sy, sx)], []
        seen[sy, sx] = True
        while stack:                        # flood fill one white blob
            y, x = stack.pop()
            pts.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and white[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        ys = [p[0] for p in pts]
        xs = [p[1] for p in pts]
        bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if not (10 * scale <= bw <= 30 * scale and 14 * scale <= bh <= 34 * scale):
            continue
        if not 0.35 <= len(pts) / (bw * bh) <= 0.75:      # a triangle fills about half its box
            continue
        # A sideways triangle (▶ or ◀): rows widest in the middle and narrow at the
        # top and bottom. Letters ('e', 'R') don't have that shape.
        rows = [0] * bh
        for y, _x in pts:
            rows[y - min(ys)] += 1
        peak = max(range(bh), key=lambda i: rows[i])
        if not (0.3 * bh <= peak <= 0.7 * bh and rows[0] <= 0.45 * rows[peak]
                and rows[-1] <= 0.45 * rows[peak]):
            continue
        # ...and one straight vertical side (the base), which round letters lack.
        left = {}
        right = {}
        for y, x in pts:
            left[y] = min(x, left.get(y, x))
            right[y] = max(x, right.get(y, x))
        core = [y for y in left if min(ys) + bh * 0.2 <= y <= max(ys) - bh * 0.2]
        if not core:
            continue
        left_sd = float(np.std([left[y] for y in core]))
        right_sd = float(np.std([right[y] for y in core]))
        if min(left_sd, right_sd) > 1.2:
            continue
        points = "right" if left_sd < right_sd else "left"     # the flat side is the base
        # ...and no hole: 'e', 'R', 'p', 'o' enclose a gap, a triangle doesn't.
        if _has_hole({(y - min(ys), x - min(xs)) for y, x in pts}, bh, bw):
            continue
        cy, cx = (min(ys) + max(ys)) // 2, (min(xs) + max(xs)) // 2
        pad = int(18 * scale)
        # Must sit on a dark button: the pixels right around the arrow (not the
        # arrow itself) are dark grey, even in Arctic's white snow.
        r0, c0 = max(0, cy - pad // 2 - 4), max(0, cx - pad // 2 - 4)
        near = a[r0:cy + pad // 2 + 5, c0:cx + pad // 2 + 5]
        bg = near[~white[r0:cy + pad // 2 + 5, c0:cx + pad // 2 + 5]]
        if bg.size == 0 or float(np.median(bg)) > 120:
            continue
        if best is None or cx > best[0]:                   # the panel's arrow is the rightmost
            best = (int(cx), int(cy), points)
    return None if best is None else (best[0] + x0, best[1] + y0, best[2])


BANNER_TEXT_H = 28 / 1009    # spawn banner letter height, share of the client height


def _checked(b: Banner) -> Banner:
    """A parsed banner checked against the block table: the name's OCR typo
    fixed ('Reljc' -> 'Relic') and a misread number snapped to a real one
    ('1 / 250' -> 250,000). Every way a banner is read goes through this."""
    b = replace(b, block=blocks.fix_name(b.block))
    v, d = blocks.snap(b.block, b.variant, b.denominator)
    return replace(b, variant=v, denominator=d)


def _zoom_read(ocr: Ocr, img: Image.Image, ln) -> Banner | None:
    """Read one shrunken banner line again at 5x: tiny text read at 2x loses
    words ('Inverted') and digits ('1,000,000' -> '100,000')."""
    if not ln.words:
        return None
    x0 = min(w.x for w in ln.words) - 8
    y0 = min(w.y for w in ln.words) - 6
    x1 = max(w.x + w.w for w in ln.words) + 8
    y1 = max(w.y + w.h for w in ln.words) + 6
    sub = img.crop((max(0, int(x0)), max(0, int(y0)), min(img.width, int(x1)), min(img.height, int(y1))))
    b = parse_banner(" ".join(texts(ocr.read(sub, 5.0))))
    return replace(_checked(b), small=True) if b else None


def spawn_banner(ocr: Ocr, img: Image.Image, ocr_lines: list, client_h: float) -> Banner | None:
    """The spawn banner among OCR lines of the banner strip, or None.

    `ocr_lines` were read off `img` (their boxes are in its pixels, at the
    client's scale); `client_h` is the client height, which says how tall
    full-size banner text is. Used for the banner strip itself and for the
    vote scan's whole-window read, so both get the same checks.
    """
    full = BANNER_TEXT_H * client_h             # a full-size banner's letter height
    found = None
    for ln in ocr_lines:
        b = parse_banner(ln.text)
        if b is None:
            continue
        b = _checked(b)
        hs = sorted(w.h for w in ln.words)
        if hs and hs[len(hs) // 2] < 0.65 * full:
            b = _zoom_read(ocr, img, ln) or replace(b, small=True)
        if b.denominator is not None and not b.small:
            return b
        if found is None or (found.denominator is None and b.denominator is not None):
            found = b
    lines = texts(ocr_lines)
    if len(lines) > 1 and (found is None or found.denominator is None):
        # A long banner can wrap, leaving '(1 / 1,000,000)' on a line of its
        # own. Only to fill a gap: a line that had its number (maybe from the
        # zoomed read) is better than a parse of everything run together.
        b = parse_banner(" ".join(lines))
        if b is not None and (found is None or b.denominator is not None):
            b = _checked(b)
            found = b if found is None else replace(b, small=found.small)
    if found is not None and found.denominator is None:
        # The odds were covered (a tooltip, another banner): a known block's
        # rarity still comes from the table.
        den = blocks.rarity_of(found.block, found.variant)
        if den:
            found = replace(found, denominator=den)
    return found


def read_banner(ocr: Ocr, client: Image.Image) -> tuple[list[str], Banner | None]:
    """All text in the banner strip, and the spawn banner if one is there.

    The lines come back even when they are not a spawn, so the app can log
    banners it doesn't know yet (they get learned from the log later).
    """
    band = crop(client, REGIONS["banner"])
    ocr_lines = ocr.read(band, 2.0)
    return texts(ocr_lines), spawn_banner(ocr, band, ocr_lines, client.height)
