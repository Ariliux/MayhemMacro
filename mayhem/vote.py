"""The world vote: which maps are on the vote screen, and which one to click.

The vote screen shows three cards in a row, each with the map's name, a green
VOTE button under it and an 'N Votes' count under that. A map name only counts
as an option when it is a card: a line that is nothing but the name, outside
the chat, with a VOTE button or a vote count right under it. So a map named in
chat ('dreamland pls') or in a banner ('The new world is Arctic') is never an
option, and two of them can't fake a vote screen. Options are sorted the way a
person reads (rows top to bottom, each row left to right). The engine saves a
picture of every vote screen so the layout can be checked and the click offset
tuned later.
"""
import difflib
import re
from dataclasses import dataclass

from . import MAPS
from .hud import REGIONS
from .ocr import OcrLine, OcrWord

FUZZY_MIN = 0.8         # difflib ratio for 'Dreamlnad' -> Dreamland
ROW_GAP = 40            # px: option centres this close vertically share a row
HUD_BAND = 0.17         # the top 17% of the client is the HUD, never an option
# Under a card's name: its VOTE button (~50 px down at 1009) and its vote
# count (~130 px down). Shares of the client height and width.
CUE_BELOW = 0.18
CUE_SIDE = 0.06         # cards are ~310 px apart, so a neighbour's button is never this close
# Client size of the one tested setup (1920x1080, maximised window). Only
# used when the caller doesn't pass the real one.
DEFAULT_CLIENT_H = 1009
DEFAULT_CLIENT_W = 1920


@dataclass(frozen=True)
class VoteOption:
    name: str                                   # as in MAPS, e.g. 'Dreamland'
    cx: float                                   # centre of the name, client px
    cy: float
    box: tuple[float, float, float, float]      # x, y, w, h of the name, client px


# Letters OCR mixes up, folded the same way on both sides so 'Vold' still
# reads as Void and 'Lava' isn't hurt by it.
_FOLD = str.maketrans({"1": "i", "l": "i", "|": "i", "!": "i", "0": "o", "5": "s", "8": "b"})


def _norm(text: str) -> str:
    # Punctuation around a word goes ('Lava!'); '|' and '!' inside one are
    # usually an i or l ('Arct!c').
    t = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", text.lower())
    return re.sub(r"[^a-z0-9|!]", "", t).translate(_FOLD)


_MAP_KEYS = {name: _norm(name) for name in MAPS}


def _best_map(text: str) -> tuple[str, float]:
    """The map `text` looks most like, and how much (0-1)."""
    t = _norm(text)
    if not t:
        return "", 0.0
    best, score = "", 0.0
    for name, key in _MAP_KEYS.items():
        s = 1.0 if t == key else difflib.SequenceMatcher(None, t, key).ratio()
        if s > score:
            best, score = name, s
    return best, score


def _union(words: tuple[OcrWord, ...]) -> tuple[float, float, float, float]:
    x0 = min(w.x for w in words)
    y0 = min(w.y for w in words)
    x1 = max(w.x + w.w for w in words)
    y1 = max(w.y + w.h for w in words)
    return x0, y0, x1 - x0, y1 - y0


def _line_maps(line: OcrLine) -> list[VoteOption]:
    """The map names on one OCR line, one per word; two neighbouring words
    count as one name when that fits better ('Over world' -> Overworld).
    Nothing at all unless EVERY word of the line is part of a map name: a
    card's title is just the name, a sentence that mentions a map is not."""
    words = line.words
    cands = []          # (score, words used, first word, map)
    for i in range(len(words)):
        for span in (1, 2):
            if i + span > len(words):
                break
            name, score = _best_map("".join(w.text for w in words[i:i + span]))
            if score >= FUZZY_MIN:
                cands.append((score, span, i, name))
    # Best matches first, a two-word name before its halves; a word is used once.
    cands.sort(key=lambda c: (-c[0], -c[1]))
    used = {i for i, w in enumerate(words) if not _norm(w.text)}    # bare punctuation
    out: list[VoteOption] = []
    for score, span, first, name in cands:
        spots = set(range(first, first + span))
        if spots & used:
            continue
        used |= spots
        box = _union(words[first:first + span])
        out.append(VoteOption(name, box[0] + box[2] / 2, box[1] + box[3] / 2, box))
    return out if len(used) == len(words) else []


def _below(opt: VoteOption, word: OcrWord, client_w: float, client_h: float) -> bool:
    """`word` sits under the option's name, inside the same card."""
    cx = word.x + word.w / 2
    return (word.y >= opt.cy and word.y <= opt.cy + CUE_BELOW * client_h
            and abs(cx - opt.cx) <= CUE_SIDE * client_w)


def _cue_word(text: str) -> bool:
    """'VOTE', 'Votes', 'Voted', or the 'OTE' OCR makes of the button."""
    t = _norm(text)
    return t in ("vote", "votes", "voted", "ote", "otes") or \
        difflib.SequenceMatcher(None, t, "vote").ratio() >= 0.75


def _is_card(opt: VoteOption, lines: list[OcrLine], client_w: float, client_h: float) -> bool:
    return any(_cue_word(w.text) and _below(opt, w, client_w, client_h)
               for ln in lines for w in ln.words)


def _in_chat(opt: VoteOption, client_w: float, client_h: float) -> bool:
    x0, y0, x1, y1 = REGIONS["chat"]
    return x0 * client_w <= opt.cx <= x1 * client_w and y0 * client_h <= opt.cy <= y1 * client_h


def _by_rows(options: list[VoteOption]) -> list[VoteOption]:
    """Reading order: rows top to bottom (centres within ROW_GAP share a row),
    each row left to right."""
    rows: list[list[VoteOption]] = []
    for opt in sorted(options, key=lambda o: o.cy):
        if rows and opt.cy - rows[-1][0].cy <= ROW_GAP:
            rows[-1].append(opt)
        else:
            rows.append([opt])
    return [opt for row in rows for opt in sorted(row, key=lambda o: o.cx)]


def find_options(lines: list[OcrLine], client_h: float | None = None,
                 client_w: float | None = None) -> list[VoteOption]:
    """The vote cards on screen, one option per map, in reading order.

    `lines` must come from an OCR of the whole client picture (boxes in client
    px). `client_h` and `client_w` are that picture's size (the tested setup's
    1920x1009 when not given), used to skip the HUD band at the top and the
    chat, and to find each card's VOTE button. When a map shows up twice, the
    biggest one is the option.
    """
    h = float(client_h or DEFAULT_CLIENT_H)
    w = float(client_w or DEFAULT_CLIENT_W)
    biggest: dict[str, VoteOption] = {}
    for opt in (o for line in lines for o in _line_maps(line)):
        if opt.cy < HUD_BAND * h or _in_chat(opt, w, h) or not _is_card(opt, lines, w, h):
            continue
        old = biggest.get(opt.name)
        if old is None or opt.box[2] * opt.box[3] > old.box[2] * old.box[3]:
            biggest[opt.name] = opt
    return _by_rows(list(biggest.values()))


def is_vote_screen(options: list[VoteOption]) -> bool:
    """Two different map cards on screen at once."""
    return len({o.name for o in options}) >= 2


# 'O Votes', 'I Vote', '12 Votes': the count, with OCR's letters for digits.
_COUNT = re.compile(r"^\s*([0-9OoIl|!]{1,3})\s*v[o0]t", re.I)
_COUNT_DIGITS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "!": "1"})


def card_votes(lines: list[OcrLine], opt: VoteOption, client_h: float | None = None,
               client_w: float | None = None) -> int | None:
    """How many votes the card of `opt` has ('0 Votes' under its button),
    None when it can't be read."""
    h = float(client_h or DEFAULT_CLIENT_H)
    w = float(client_w or DEFAULT_CLIENT_W)
    for ln in lines:
        m = _COUNT.match(ln.text or "")
        if m and ln.words and _below(opt, ln.words[-1], w, h):
            digits = m.group(1).translate(_COUNT_DIGITS)
            if digits.isdigit():
                return int(digits)
    return None


def choose(options: list[VoteOption], ranking: list[str]) -> VoteOption | None:
    """The highest-ranked ticked map that is an option. If none of them is (or
    nothing is ticked), the first option. None only when there are no options."""
    if not options:
        return None
    by_name = {o.name.lower(): o for o in options}
    for want in ranking or []:
        opt = by_name.get(str(want).strip().lower())
        if opt is not None:
            return opt
    return options[0]


def click_point(opt: VoteOption, offset: tuple[int, int]) -> tuple[int, int]:
    """Where to click for `opt`, in client px: the centre of its name moved by
    the user's offset (vote_click_offset, [0, 0] by default)."""
    try:
        dx, dy = float(offset[0]), float(offset[1])
    except (TypeError, ValueError, IndexError):
        dx = dy = 0.0
    return round(opt.cx + dx), round(opt.cy + dy)
