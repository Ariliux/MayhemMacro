"""MayhemMacro - keeps you in Block Mayhem X while you're away.

The window is GlassMacro's shell (sidebar, header with Start/Stop and F1/F2,
cards, toasts, the 14-day chart) with its own warm accent and a cube for a
mark, so the two read as siblings rather than copies.

The run itself lives in mayhem/engine.py on ONE worker thread, and that thread
never touches the window: it hands back plain event dicts through emit(),
which go into a queue that the Tk thread drains every 50 ms. What an event
MEANS - the feed line, the counters, the toast and which Discord alerts go out
- is decided in one pure function, plan_event(), so that part is tested
without a window at all (tests/test_ui_logic.py). Session applies the plan to
the run's counters, stats.json and the webhook sender; the window only draws.
"""
import copy
import ctypes
import ctypes.wintypes as wintypes
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageTk

import customtkinter as ctk
import tkinter as tk
import tkinter.font as tkfont

from mayhem import APP_NAME, APP_VER, MAPS
from mayhem import discord as D
from mayhem import paths, rejoin, safety, update
from mayhem import stats as S
from mayhem.blocks import display_name


def resource(name: str) -> str:
    """A file shipped inside the exe. PyInstaller unpacks those to _MEIPASS."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


_user32 = ctypes.windll.user32

# ---- GlassMacro's surfaces, a warm accent of its own -------------------------
# Same deep blue-black surfaces, so the two apps sit together; the accent is a
# block-game amber instead of GlassMacro's ice blue. Surfaces step up in
# brightness BG < PANEL < CARD < CARD_HI; with no shadows in Tk, that layering
# plus a 1px border is the only depth there is.
ACCENT, ACCENT_SOFT, ACCENT_DEEP = "#f5a524", "#ffd089", "#b86e0a"
INK = "#1c1102"                         # text on an accent-filled button
BG, PANEL, CARD, CARD_HI = "#070b11", "#0c121b", "#111a26", "#182334"
LINE, TEXT, SUBTLE, MUTED = "#203047", "#e8f0f8", "#8ea2b8", "#7389a0"
# AMBER is the warning colour. With an amber accent it had to move to a clear
# yellow, or "Paused" and the Start button would read as the same thing.
GREEN, RED, AMBER = "#4ade80", "#f87171", "#facc15"
# rare blocks: violet like the Discord alert's edge; the two variants too
RARE, CHROMA, INVERT = "#c084fc", "#f472b6", "#e2e8f0"

# Quiet Premium: one type family, a strict size scale, and depth from layered
# panels plus 1px hairlines.
INSET, HAIRLINE, SHEEN = "#0a1019", "#172538", "#28405c"
ACCENT_DIM, GREEN_DIM = "#2d2110", "#0f2a1d"
AMBER_DIM, RED_DIM, RARE_DIM = "#2a2610", "#2a1418", "#241a33"
KEYCAP_LINE = "#2c4058"
# the selected page's row, and a row under the mouse (a warm tint of the
# accent goes muddy on navy, so these stay neutral; the bar beside it is amber)
NAV_ACTIVE, NAV_HOVER = "#172131", "#101b28"
LINE_DIM = "#34465e"                    # a disabled arrow: there, but clearly off


def blend(c1: str, c2: str, t: float) -> str:
    """Mix two #rrggbb colours: t=0 is c1, t=1 is c2. Tk has no alpha."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def span(secs) -> str:
    """'2h 14m' for a duration, '14m' under an hour, '40s' under a minute."""
    s = int(max(0, secs or 0))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


# The brand mark: a small isometric block, three faces on a 20x20 grid -
# light on top, the accent on the left, a deep amber on the shaded side.
CUBE_FACES = (((10, 1, 18.5, 5.75, 10, 10.5, 1.5, 5.75), ACCENT_SOFT),
              ((1.5, 5.75, 10, 10.5, 10, 19.5, 1.5, 14.75), ACCENT),
              ((10, 10.5, 18.5, 5.75, 18.5, 14.75, 10, 19.5), ACCENT_DEEP))


def draw_cube(canvas: tk.Canvas, x: float, y: float, size: float,
              dim: bool = False) -> None:
    """The app icon (mayhem/icon.py) on a canvas. dim fades it into the panel,
    for the empty pages."""
    img = cube_image(max(8, int(round(size))))
    if dim:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * 0.45)))
    photo = ImageTk.PhotoImage(img, master=canvas)
    canvas._mayhem_icons = getattr(canvas, "_mayhem_icons", []) + [photo]   # keep it alive
    canvas.create_image(x, y, image=photo, anchor="nw")


def cube_image(size: int, ss: int = 4) -> Image.Image:
    """The app icon as an RGBA picture (window icon, logo). Tiny sizes use the
    simpler version so they stay crisp."""
    from mayhem.icon import render
    return render(size, detail=size >= 40)


# Stats chart: soft round capsules, lighter at the top. Tk's canvas has no
# anti-aliasing, so the bars are drawn by PIL at 4x and scaled down.
DAY_TRACK, DAY_ZERO = "#172333", "#2b3d55"
DAY_BAR = ("#ffc66e", "#c2730c")             # top, bottom
DAY_TODAY = ("#ffe6b3", ACCENT)
# the header's running shimmer: a soft band sliding along the hairline
SHIMMER_RUN = ("#3b2a10", ACCENT)
SHIMMER_AMBER = ("#3a3412", AMBER)
SHIMMER_RECTS = 17


def render_day_bars(w: int, h: int, bars: list[dict], bg: str | None = None,
                    ss: int = 4, glow_px: int = 6) -> Image.Image:
    """The 14 day bars as one smooth picture, w x h real pixels.

    Each bar is a dict: x0, x1 (its column), top and base (the full-height
    track), y (the bar's top), cols (top, bottom gradient colours, or None for
    a day with no play: a small dot) and glow (a colour, or None).
    """
    bg = bg or CARD
    W, H = max(1, w * ss), max(1, h * ss)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    for b in bars:                         # faint full-height tracks
        x0, x1 = b["x0"] * ss, b["x1"] * ss
        d.rounded_rectangle((x0, b["top"] * ss, x1, b["base"] * ss),
                            radius=(x1 - x0) / 2, fill=DAY_TRACK)
    for b in bars:                         # a soft glow behind one bar
        if not b.get("glow") or b.get("cols") is None:
            continue
        g = glow_px * ss
        x0, x1, y0, y1 = (b["x0"] * ss, b["x1"] * ss, b["y"] * ss,
                          b["base"] * ss)
        box = (int(max(0, x0 - 3 * g)), int(max(0, y0 - 3 * g)),
               int(min(W, x1 + 3 * g)), int(min(H, y1 + 3 * g)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        m = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (x0 - box[0], y0 - box[1], x1 - box[0], y1 - box[1]),
            radius=(x1 - x0) / 2, fill=110)
        img.paste(b["glow"], box, m.filter(ImageFilter.GaussianBlur(g)))
    for b in bars:
        x0, x1 = round(b["x0"] * ss), round(b["x1"] * ss)
        bw = max(2, x1 - x0)
        if b.get("cols") is None:          # no play that day: a small dot
            r = bw * 0.24
            cx, cy = (x0 + x1) / 2, b["base"] * ss - bw / 2
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=DAY_ZERO)
            continue
        y0, y1 = round(b["y"] * ss), round(b["base"] * ss)
        bh = max(bw, y1 - y0)
        y0 = y1 - bh
        top, bottom = b["cols"]
        grad = Image.linear_gradient("L").resize((bw, bh))
        fill = Image.composite(Image.new("RGB", (bw, bh), bottom),
                               Image.new("RGB", (bw, bh), top), grad)
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw - 1, bh - 1),
                                               radius=bw / 2, fill=255)
        img.paste(fill, (x0, y0), mask)
    return img.resize((max(1, w), max(1, h)), Image.LANCZOS)


def round_box(w: int, h: int, fill: str, border: str, bg: str | None = None,
              radius: int = 8, ss: int = 4) -> Image.Image:
    """A small anti-aliased rounded box (the chart's tooltip). bg=None: the
    corners are see-through."""
    img = (Image.new("RGB", (w * ss, h * ss), bg) if bg else
           Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0)))
    ImageDraw.Draw(img).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=fill,
        outline=border, width=ss)
    return img.resize((w, h), Image.LANCZOS)


# ------------------------------------------------------------ the screen ---
# The screen reading (banner, HUD timer, vote) is only tested on a 1920x1080
# screen at 100% Windows scaling, so the app says so when the screen differs.
SUPPORTED_SCREEN = (1920, 1080)
SUPPORTED_SCALING = 100


def screen_size() -> tuple[int, int]:
    """Primary screen in real pixels (CustomTkinter makes us DPI aware)."""
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def display_scaling() -> int:
    """Windows display scaling of the MAIN screen, in percent (100, 125...).

    MAYHEM_TEST_SCALING=150 in the environment pretends, so the warning can be
    checked without touching anyone's display settings. Needs DPI awareness
    (CustomTkinter turns it on), or Windows answers 100% whatever the truth is.
    """
    fake = os.environ.get("MAYHEM_TEST_SCALING", "")
    if fake.isdigit():
        return int(fake)
    try:
        u = ctypes.WinDLL("user32")          # own handle: own argtypes
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        sh = ctypes.WinDLL("shcore")
        sh.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_uint),
                                        ctypes.POINTER(ctypes.c_uint)]
        mon = u.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # the primary one
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        if sh.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return round(dx.value * 100 / 96)
    except Exception:
        pass
    try:
        return round(_user32.GetDpiForSystem() * 100 / 96)
    except Exception:
        return SUPPORTED_SCALING


def keep_awake(on: bool) -> bool:
    """While a run is going, stop Windows going to sleep - a sleeping PC ends
    an AFK session for good. Only the PC; the screen may still turn off.
    Per thread, so call it from the Tk thread. Never while testing."""
    try:
        safety.guard("keep the PC awake")
        flags = 0x80000000 | (0x00000001 if on else 0)  # CONTINUOUS | SYSTEM
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


# ------------------------------------------------------------------ log ---
LOG_KEEP_BYTES = 5 * 1024 * 1024


def rotate_log(path: str | None = None, limit: int = LOG_KEEP_BYTES) -> bool:
    """At start-up, a log over `limit` becomes log.old.txt (replacing the
    previous one) - day-long runs grew it without end. True if rotated."""
    try:
        path = path or paths.log_path()
        if os.path.getsize(path) > limit:
            os.replace(path, os.path.splitext(path)[0] + ".old.txt")
            return True
    except Exception:
        pass
    return False


def write_log_line(msg: str, path: str | None = None) -> bool:
    """Append one line to log.txt. The in-window log dies with the app; this
    file survives, so an overnight run can be explained the next morning.
    Never raises - logging must never break the macro."""
    try:
        path = path or paths.log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg) + "\n")
        return True
    except Exception:
        return False


# Its own mutex and taskbar id: sharing GlassMacro's would make the second app
# to open just bring the other one to the front and quit.
SINGLE_INSTANCE_MUTEX = "Local\\MayhemMacro.SingleInstance"
APP_USER_MODEL_ID = "MayhemMacro.App"


def claim_single_instance(name: str = SINGLE_INSTANCE_MUTEX,
                          focus: bool = True) -> bool:
    """True if no other MayhemMacro is running. Two copies would both press
    keys in the same game; a second start just brings the open one forward."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, name)
    # 183 = already exists. 5 = access denied, which is what a normal copy
    # gets when the first one was started with "Run as administrator".
    if ctypes.get_last_error() in (183, 5) or not handle:
        if focus:
            try:
                safety.guard("focus")           # our own window, but still a focus change
                u = ctypes.windll.user32
                h = u.FindWindowW("TkTopLevel", APP_NAME) or 0
                if h:
                    u.ShowWindow(h, 9)                  # SW_RESTORE
                    u.SetForegroundWindow(h)
            except Exception:
                pass
        return False
    _held_mutexes.append(handle)             # held until the app exits
    return True


_held_mutexes: list = []


# ======================================================================
# What an event means. Pure: no window, no files, no network - so it is
# tested on its own, and the window can't disagree with the alerts.
# ======================================================================
# The rarity menus on the Alerts page: "1 in 10,000" up to "1 in 100,000,000".
RARE_STEPS = (10_000, 100_000, 1_000_000, 10_000_000, 100_000_000)
AFK_MIN, AFK_MAX, AFK_DEFAULT = 2, 15, 10
VOTE_CYCLE = 600            # a world vote every 10 minutes
HUD_FRESH = 25.0            # a vote timer read this recently is shown as sure
TILE_KEYS = ("rares", "votes", "afk", "rejoins")
TILE_NAMES = {"rares": "Rares", "votes": "Votes", "afk": "AFK saves",
              "rejoins": "Rejoins"}


def rarity_text(n) -> str:
    """'1 in 100,000' for a denominator; '' when there isn't one."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"1 in {n:,}" if n > 1 else ""


def rarity_from_text(text) -> int | None:
    """'1 in 100,000' -> 100000 (what the option menus hand back)."""
    digits = re.sub(r"[^\d]", "", str(text or "").split("in", 1)[-1])
    return int(digits) if digits else None


BAR_RARE_STEPS = (100_000, 1_000_000, 10_000_000, 50_000_000, 100_000_000,
                  500_000_000, 1_000_000_000)
BAR_RARE_DEFAULT = 10_000_000


def bar_rare_floor(settings: dict) -> int:
    """How rare a block must be to count as a rare on the session bar."""
    try:
        n = int(settings.get("bar_rare_min") or BAR_RARE_DEFAULT)
    except (TypeError, ValueError):
        n = BAR_RARE_DEFAULT
    return n if n > 1 else BAR_RARE_DEFAULT


def bar_rares(spawns: list, found: list, floor: int) -> tuple[tuple | None, int]:
    """The session bar's RAREST and RARES for this run, from its rare spawns
    and its collected blocks (rows with name / odds / colour / den, newest
    spawn first). RAREST is the rarest of all of them (the newest spawn when
    none has a number); RARES counts spawns or collects at `floor`+, whichever
    is more, so a block that spawned and was then picked up counts once and
    the number never goes down during a run."""
    known = [r for r in list(spawns) + list(found) if r.get("den")]
    rarest = None
    if known:
        r = max(known, key=lambda r: r["den"])
        rarest = (r["name"], r.get("odds") or "", r.get("colour"))
    elif spawns or found:
        r = spawns[0] if spawns else found[-1]
        rarest = (r["name"], r.get("odds") or "", None)
    count = max(sum(1 for r in spawns if (r.get("den") or 0) >= floor),
                sum(1 for r in found if (r.get("den") or 0) >= floor))
    return rarest, count


def short_odds(n: int) -> str:
    """10_000_000 -> '10M+', 500_000 -> '500K+', 1_000_000_000 -> '1B+'."""
    for size, mark in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            v = n / size
            return f"{v:.0f}{mark}+" if v == int(v) else f"{v:.1f}{mark}+"
    return f"{n}+"


def rarity_choices(current) -> list[str]:
    """The menu's choices; a value set by hand in settings.json that isn't one
    of the steps is kept in the list, so opening the menu can't change it."""
    vals = set(RARE_STEPS)
    try:
        if int(current) > 1:
            vals.add(int(current))
    except (TypeError, ValueError):
        pass
    return [rarity_text(v) for v in sorted(vals)]


def clock_text(secs) -> str:
    """'08:31' for the vote timer; '—' when it isn't known."""
    if secs is None:
        return "—"
    s = max(0, int(secs))
    return f"{s // 60:02d}:{s % 60:02d}"


def vote_left(timer: int | None, read_at: float | None, now: float) -> int | None:
    """Seconds to the next world vote: the last timer read counted down, and
    rolled over to the next 10-minute vote once it has passed."""
    if timer is None or read_at is None:
        return None
    left = timer - (now - read_at)
    if left < 0:
        left %= VOTE_CYCLE
    return int(left)


def clamp_minutes(v, lo: int = AFK_MIN, hi: int = AFK_MAX,
                  default: int = AFK_DEFAULT) -> int:
    """Anti-AFK minutes, always 2-15 (settings.json can hold anything)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clean_ranking(ranking) -> list[str]:
    """Only real map names, in their proper case, each once, order kept."""
    names = {m.lower(): m for m in MAPS}
    out: list[str] = []
    for item in ranking if isinstance(ranking, (list, tuple)) else []:
        name = names.get(str(item).strip().lower())
        if name and name not in out:
            out.append(name)
    return out


def rank_toggle(ranking, name: str, on: bool) -> list[str]:
    """Ticking a map puts it at the bottom of the ranking; unticking takes it
    out. Everything else keeps its place."""
    out = [m for m in clean_ranking(ranking) if m != name]
    if on and name in MAPS:
        out.append(name)
    return out


def rank_move(ranking, name: str, step: int) -> list[str]:
    """Move one map up (step -1) or down (+1); the ends just stay put."""
    out = clean_ranking(ranking)
    if name not in out:
        return out
    i = out.index(name)
    j = max(0, min(len(out) - 1, i + step))
    out.insert(j, out.pop(i))
    return out


def _int_or_none(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _variant_of(ev: dict) -> str:
    v = str(ev.get("variant") or "normal").lower()
    if ev.get("source") == "log_chromatic":
        return "chromatic"
    return v if v in ("inverted", "chromatic") else "normal"


def block_label(block, variant: str = "normal") -> str:
    """The name to show: 'Resonance', 'SNAKEBLOCK', 'Inverted Relic'. '' for
    a block the log couldn't name ('nil_chromatic')."""
    raw = str(block or "").strip()
    if raw.lower() in ("", "nil", "none"):
        return ""
    name = display_name(raw) or raw
    if variant in ("inverted", "chromatic") and not name.lower().startswith(variant):
        name = f"{variant.title()} {name}"
    return name


def rare_passes(den: int | None, variant: str, settings: dict) -> bool:
    """Worth an alert? Rarer than (or exactly) the user's 'Alert me' choice;
    Inverted and Chromatic always when that switch is on; and a block with no
    known rarity always - a music block or a late Chromatic notice is never
    common, and there is no number to compare."""
    if den is None:
        return True
    if variant in ("inverted", "chromatic") and settings.get("alert_variants_always", True):
        return True
    try:
        floor = int(settings.get("rare_min", 100_000))
    except (TypeError, ValueError):
        floor = 100_000
    return den >= floor


def rare_wants_ping(den: int | None, flagged, settings: dict) -> bool:
    """@ me for this one? When the engine flagged it, or it is at least as
    rare as the '@ me for blocks rarer than' choice."""
    if flagged is True:
        return True
    try:
        floor = int(settings.get("rare_ping_min", 1_000_000))
    except (TypeError, ValueError):
        floor = 1_000_000
    return den is not None and den >= floor


def _discord_cfg(settings: dict) -> dict:
    cfg = settings.get("discord") if isinstance(settings, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def rejoins_public(settings: dict) -> bool:
    """Rejoin goes to a public server: no link, or one that can't work."""
    link = str(settings.get("private_server_link") or "")
    return rejoin.deep_links(link) == [rejoin.PUBLIC_LINK]


@dataclass
class RunCtx:
    """What plan_event needs to know about the run, as plain values."""
    live: bool = False              # a real Start is running (alerts/stats armed)
    run_no: int | None = None
    world: str | None = None
    ran: float = 0.0                # seconds since Start
    counts: dict = field(default_factory=dict)
    lifetime: float = 0.0           # stats total_secs
    narrated: bool = False          # a storage / rejoin / vote event came just now
    rarest: tuple | None = None     # (name, 1-in-N) of the rarest block this run

    def count_fields(self) -> dict:
        out = {TILE_NAMES[k]: int(self.counts.get(k, 0)) for k in TILE_KEYS}
        if self.rarest:
            out["Rarest"] = f"{self.rarest[0]} (1 in {self.rarest[1]:,})"
        return out


@dataclass
class Alert:
    """One Discord message to queue."""
    group: str                      # the switch on the Discord page it belongs to
    embed: dict
    mention: str | None = None
    picture: Any = None
    priority: int = D.PRI_NORMAL


@dataclass
class FeedRow:
    key: str                        # FEED_FILTERS keys; same key twice in a row merges
    glyph: str
    colour: str                     # a colour constant's NAME (RED, GREEN...)
    title: str
    sub: str = ""


@dataclass
class Plan:
    """Everything one event changes. The window draws it; Session counts it."""
    feed: list = field(default_factory=list)       # FeedRow
    log: list = field(default_factory=list)        # (level, text)
    tiles: list = field(default_factory=list)      # TILE_KEYS to add one to
    stats: list = field(default_factory=list)      # stats.json counters
    toast: tuple | None = None                     # (text, kind)
    alerts: list = field(default_factory=list)     # Alert
    state: tuple | None = None                     # (title, detail, colour name)
    mode: str | None = None                        # running|paused|rejoining|waiting|stopped
    rare: dict | None = None                       # a row for the Alerts page list
    hud: dict | None = None                        # timer / storage / world as read
    world: str | None = None                       # the world changed to this
    end: tuple | None = None                       # (reason, detail): the run is ending
    error: str | None = None                       # an error line (it may end the run)
    voted: str | None = None                       # the map a vote went to
    found: dict | None = None                      # a collected block, for the session bar


STATE_LOOK = {
    "running": ("Running", "Keeping you in Block Mayhem X.", "GREEN"),
    "paused": ("Paused", "Roblox isn't in front, so it's waiting.", "AMBER"),
    "rejoining": ("Rejoining", "Getting you back into your server.", "AMBER"),
    "waiting": ("Waiting", "Waiting until you're away from your PC.", "ACCENT"),
}


def alerts_for(ev: dict, settings: dict, ctx: RunCtx) -> list[Alert]:
    """THE one place that decides which Discord alerts an event sends.

    Nothing goes out unless a real Start is running (ctx.live), Discord alerts
    are on with a valid link, and that kind of alert is ticked. On top of
    that, rare blocks follow the Alerts page (switch, rarity floor, variants,
    picture) and storage follows its own switch.
    """
    if not ctx.live or not isinstance(ev, dict) or not isinstance(settings, dict):
        return []
    cfg = _discord_cfg(settings)
    out: list[Alert] = []

    def add(group: str, embed: dict, ping: bool = True, picture=None) -> None:
        if D.should_send(cfg, group):
            out.append(Alert(group, embed, D.mention_for(cfg, group, ping=ping),
                             picture, D.priority_for(group)))

    kind = ev.get("type")
    if kind == "rare":
        if settings.get("rare_alerts", True) is not True:
            return []
        den, variant = _int_or_none(ev.get("denominator")), _variant_of(ev)
        if not rare_passes(den, variant, settings):
            return []
        picture = ev.get("picture") if settings.get("attach_picture") is True else None
        # the engine says which world it was in (None = not sure since the vote)
        world = ev.get("world") if "world" in ev else ctx.world
        add("rare", D.rare_message(block_label(ev.get("block")), den, variant,
                                   world if isinstance(world, str) else None,
                                   str(ev.get("source") or "banner"), run_no=ctx.run_no),
            ping=rare_wants_ping(den, ev.get("ping"), settings), picture=picture)
    elif kind == "collected":
        if settings.get("rare_alerts", True) is not True:
            return []
        den, variant = _int_or_none(ev.get("denominator")), _variant_of(ev)
        if den is None or not rare_passes(den, variant, settings):
            return []                       # a collect with no known rarity: no ping-worthy news
        add("rare", D.collected_message(block_label(ev.get("block")), den, variant,
                                        str(ev.get("how") or "chat"), run_no=ctx.run_no),
            ping=rare_wants_ping(den, None, settings))
    elif kind == "rejoin":
        phase = ev.get("phase")
        tries = _int_or_none(ev.get("try")) or 1
        why = str(ev.get("why") or "")
        if phase == "gave_up":
            add("stuck", D.rejoin_message("gave_up", tries=tries, why=why,
                                          run_no=ctx.run_no))
        elif phase in ("start", "ok", "fail"):
            add("rejoin", D.rejoin_message(phase, tries=tries, why=why,
                                           wait=ev.get("wait"),
                                           public=rejoins_public(settings),
                                           run_no=ctx.run_no), ping=False)
    elif kind == "storage_full":
        if settings.get("storage_alert", True) is True:
            add("storage", D.storage_message(_int_or_none(ev.get("n")) or 0,
                                             _int_or_none(ev.get("m")) or 0,
                                             run_no=ctx.run_no))
    elif kind == "run":
        rk = ev.get("kind")
        if rk == "start":
            add("run", D.run_message("start", lifetime=ctx.lifetime,
                                     run_no=ctx.run_no))
        elif rk in ("stop", "closed"):
            ran = ev.get("ran", ctx.ran)
            if ev.get("reason") == "error" and D.should_send(cfg, "error"):
                add("error", D.error_message(str(ev.get("detail") or ""),
                                             stopped=True, run_no=ctx.run_no))
            else:
                add("run", D.run_message(rk, ran=ran, counts=ctx.count_fields(),
                                         lifetime=ctx.lifetime, run_no=ctx.run_no))
        elif rk == "hourly":
            add("hourly", D.run_message("hourly", ran=ev.get("ran", ctx.ran),
                                        counts=ctx.count_fields(),
                                        lifetime=ctx.lifetime,
                                        hours=ev.get("hours"), run_no=ctx.run_no))
    return out


def _sentence(text: str) -> str:
    """The engine's short status texts ('Roblox isn't open') as sentences."""
    text = text.strip()
    return text if not text or text[-1] in ".!?…" else text + "."


def plan_event(ev: dict, settings: dict, ctx: RunCtx) -> Plan:
    """What one engine event (or one of the app's own 'run' events) means:
    the feed row, the counters, the toast, the status card and the alerts.
    Pure - it only reads its arguments.

    The engine writes its own plain-English Log line for everything it does,
    so a typed event adds no Log line here; only the engine's 'log' events
    (and the app's own lines) reach the Log page.
    """
    p = Plan()
    if not isinstance(ev, dict):
        return p
    settings = settings if isinstance(settings, dict) else {}
    kind = ev.get("type")

    if kind == "log":
        level = str(ev.get("level") or "info")
        level = level if level in ("info", "warn", "error") else "info"
        text = str(ev.get("text") or "").strip()
        if text:
            p.log.append((level, text))
            # a warning right after a storage / rejoin / vote event says the
            # same thing again: that row is already in the feed
            if level == "warn" and not ctx.narrated:
                p.feed.append(FeedRow("warn", "!", "AMBER", text))
            elif level == "error":
                if not ctx.narrated:
                    p.feed.append(FeedRow("err", "×", "RED", text))
                p.error = text

    elif kind == "state":
        state = str(ev.get("state") or "")
        why = str(ev.get("why") or "").strip()
        p.mode = state if state in STATE_LOOK or state == "stopped" else None
        if state == "running" and why.lower() == "starting":
            p.state = ("Starting", "Looking for Block Mayhem X…", "GREEN")
        elif state in STATE_LOOK:
            title, detail, colour = STATE_LOOK[state]
            p.state = (title, _sentence(why) or detail, colour)
            if state == "paused":
                p.feed.append(FeedRow("pause", "❚❚", "AMBER", "Paused",
                                      why or "Roblox isn't in front"))

    elif kind == "hud":
        storage = ev.get("storage")
        ok = (isinstance(storage, (list, tuple)) and len(storage) == 2
              and all(isinstance(v, int) and not isinstance(v, bool) for v in storage))
        timer = ev.get("timer")
        world = ev.get("world")
        world = str(world).strip() if isinstance(world, str) and world.strip() else None
        # None means "not known right now": the engine stops vouching for a
        # reading once it is old, and for the world after every vote
        p.hud = {"timer": timer if isinstance(timer, int) and not isinstance(timer, bool)
                 and timer >= 0 else None,
                 "storage": (int(storage[0]), int(storage[1])) if ok else None,
                 "world": world}
        if world and world != ctx.world:
            p.world = world
            if ctx.world:                   # a change, not the first reading
                p.feed.append(FeedRow("world", "◎", "ACCENT", f"Now in {world}",
                                      "the world changed"))

    elif kind == "rare":
        den, variant = _int_or_none(ev.get("denominator")), _variant_of(ev)
        source = str(ev.get("source") or "banner")
        name = block_label(ev.get("block"), variant)
        odds = rarity_text(den)
        colour = {"chromatic": "CHROMA", "inverted": "INVERT"}.get(variant, "RARE")
        if source == "log_chromatic":
            title = f"A {name} was here" if name else "A Chromatic block was here"
            sub = "Roblox noted it a few minutes late"
        elif source == "log_song":
            title = f"{name or 'A music block'} appeared"
            sub = "its music started" + (f" · {odds}" if odds else "")
        else:
            title = f"{name or 'A rare block'} spawned!"
            sub = odds or "rarity unknown"
        p.feed.append(FeedRow("rare", "◆", colour, title, sub))
        p.tiles.append("rares")
        p.stats.append("rares")
        p.rare = {"name": name or "Unknown block", "odds": odds, "source": source, "den": den,
                  "colour": colour, "variant": variant,
                  "ts": time.strftime("%H:%M")}
        if settings.get("rare_alerts", True) is True and rare_passes(den, variant, settings):
            p.toast = (title + (f" · {odds}" if odds else ""), "rare")

    elif kind == "collected":
        den, variant = _int_or_none(ev.get("denominator")), _variant_of(ev)
        name = block_label(ev.get("block"), variant)
        colour = {"chromatic": "CHROMA", "inverted": "INVERT"}.get(variant, "RARE")
        odds = rarity_text(den)
        how = "a worker picked it up" if ev.get("how") == "pickup" else "you collected it"
        p.feed.append(FeedRow("rare", "◆", colour, f"Collected {name}",
                              f"{odds} · {how}" if odds else how))
        p.found = {"name": name, "odds": odds, "den": den, "colour": colour}

    elif kind == "vote":
        chose = str(ev.get("chose") or "").strip()
        options = [str(o) for o in ev.get("options") or [] if str(o).strip()]
        if ev.get("ok") is True and chose:
            p.feed.append(FeedRow("vote", "✓", "ACCENT", f"Voted for {chose}",
                                  f"{len(options)} maps in the vote" if options else ""))
            p.tiles.append("votes")
            p.stats.append("votes")
            p.voted = chose
        else:
            what = f"Tried to vote for {chose}" if chose else "Couldn't vote"
            p.feed.append(FeedRow("votefail", "!", "AMBER", what,
                                  "couldn't tell if it worked"))

    elif kind == "anti_afk":
        try:
            idle = float(ev.get("idle") or 0)
        except (TypeError, ValueError):
            idle = 0.0
        front = ev.get("how") == "front_jump"
        sub = ("brought Roblox forward while you were away" if front else
               f"you'd been idle {span(idle)}" if idle >= 60 else "")
        title = {"double_jump": "Double-jumped to stop the AFK kick",
                 "click_jump": "Clicked and jumped to stop the AFK kick"}.get(
                     ev.get("how"), "Jumped to stop the AFK kick")
        p.feed.append(FeedRow("afk", "↑", "GREEN", title, sub))
        p.tiles.append("afk")
        p.stats.append("anti_afk")

    elif kind == "rejoin":
        phase = ev.get("phase")
        tries = _int_or_none(ev.get("try")) or 1
        why = str(ev.get("why") or "").strip()
        if phase == "start":
            p.feed.append(FeedRow("rejoin", "↻", "AMBER", "Rejoining",
                                  why or "you got disconnected"))
        elif phase == "ok":
            p.feed.append(FeedRow("back", "✓", "GREEN", "Back in Block Mayhem X",
                                  f"after {tries} tries" if tries > 1 else "rejoined"))
            p.tiles.append("rejoins")
            p.stats.append("rejoins")
        elif phase == "fail":
            try:
                wait = float(ev.get("wait") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            p.feed.append(FeedRow("rejoinfail", "!", "AMBER", "Rejoin didn't work",
                                  f"try {tries} · trying again in {span(wait)}" if wait
                                  else f"try {tries}"))
            p.stats.append("rejoin_fails")
        elif phase == "gave_up":
            p.feed.append(FeedRow("stuck", "×", "RED", "Couldn't rejoin",
                                  f"gave up after {tries} tries"))
            p.toast = ("Couldn't rejoin · the run stopped", "err")
            p.end = ("stuck", why or f"Couldn't rejoin after {tries} tries in a row.")

    elif kind == "storage_full":
        n, m = _int_or_none(ev.get("n")) or 0, _int_or_none(ev.get("m")) or 0
        p.feed.append(FeedRow("storage", "■", "AMBER", "Storage is full",
                              f"{n}/{m} · new blocks won't fit"))
        p.stats.append("storage_full")
        if settings.get("storage_alert", True) is True:
            p.toast = (f"Storage is full · {n}/{m}", "warn")

    elif kind == "run":
        rk = ev.get("kind")
        if rk == "start":
            p.feed.append(FeedRow("start", "▶", "SUBTLE", "Started", ""))
        elif rk in ("stop", "closed"):
            ran = ev.get("ran", ctx.ran)
            reason = ev.get("reason") or "stopped"
            if reason == "error":
                p.stats.append("errors")
            title = {"error": "Stopped on an error",
                     "stuck": "Stopped: couldn't rejoin"}.get(reason, "Stopped")
            p.feed.append(FeedRow("stop", "■", "RED" if reason == "error" else "SUBTLE",
                                  title, f"after {span(ran)}"))
        elif rk == "hourly":
            h = _int_or_none(ev.get("hours")) or 1
            p.feed.append(FeedRow("hour", "●", "GREEN", f"{h}h in Block Mayhem X",
                                  "still going"))
            p.toast = (f"{h}h in Block Mayhem X · still going", "ok")

    p.alerts = alerts_for(ev, settings, ctx)
    return p


def dispatch(alerts: list, settings: dict, sender_for: Callable[[str], Any]) -> int:
    """Queue the alerts on the sender for the saved link. Returns how many
    were queued. sender_for(url) gives the sender (a fake one in tests)."""
    if not alerts:
        return 0
    url = D.normalize_hook(_discord_cfg(settings).get("webhook"))
    if not url:
        return 0
    n = 0
    for a in alerts:
        try:
            sender = sender_for(url)
            if sender is not None and sender.enqueue(
                    D.make_event(a.embed, a.mention, a.picture), a.priority):
                n += 1
        except Exception:
            pass
    return n


class Session:
    """A run's bookkeeping, with no window: the counters, lifetime stats.json
    and the Discord sender. The app calls begin() on a real Start, handle()
    for every event, tick() once a second and end() when the run stops.

    Only begin() arms it (live). Until then handle() still plans (the feed
    can show things) but nothing is counted in stats.json and no alert is
    sent - so tools that only feed events in can never post or count a run.
    """

    def __init__(self, get_settings: Callable[[], dict], *,
                 make_sender: Callable[[str], Any] | None = None,
                 stats_path: str | None = None,
                 log: Callable[[str], None] | None = None):
        self._get = get_settings
        self._make_sender = make_sender or (lambda url: D.WebhookSender(url, log=log))
        self.stats_path = stats_path
        try:
            self.stats = S.load_stats(stats_path)
        except Exception:
            self.stats = S.new_stats()       # the data folder is unusable
        self.live = False
        self.run_no: int | None = None
        self.started: float | None = None
        self.counts = dict.fromkeys(TILE_KEYS, 0)
        self.world: str | None = None
        self.end_reason, self.end_detail = "stopped", ""
        self.last_error: tuple[str, float] | None = None     # (line, when)
        self._narrated_at = float("-inf")
        self.hour_sent: int | None = None
        self.flushed_at = 0.0
        self.saved_at = 0.0
        self.sender = None

    # The engine follows a storage, failed-rejoin or failed-vote event with a
    # warning that says the same thing; within this long it isn't a new row.
    NARRATED_GAP = 1.0

    def ctx(self, now: float) -> RunCtx:
        return RunCtx(live=self.live, run_no=self.run_no, world=self.world,
                      ran=max(0.0, now - self.started) if self.started else 0.0,
                      counts=dict(self.counts),
                      lifetime=float(self.stats.get("total_secs", 0)),
                      narrated=0.0 <= now - self._narrated_at <= self.NARRATED_GAP,
                      rarest=getattr(self, "rarest", None))

    @staticmethod
    def _narrates(ev: dict) -> bool:
        kind = ev.get("type")
        return (kind == "storage_full"
                or (kind == "rejoin" and ev.get("phase") in ("fail", "gave_up"))
                or (kind == "vote" and ev.get("ok") is not True))

    def settings(self) -> dict:
        try:
            s = self._get()
            return s if isinstance(s, dict) else {}
        except Exception:
            return {}

    # ---- the run ----
    def begin(self, now: float) -> Plan:
        """A real Start: the only place a run starts counting and arms alerts."""
        if self.live:                        # restarted before the end was seen
            self.end(now)
        st = self.stats
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        self.live = True
        self.started = now
        self.counts = dict.fromkeys(TILE_KEYS, 0)
        self.world = None
        self.rarest = None
        self.end_reason, self.end_detail = "stopped", ""
        self.last_error = None
        self.hour_sent = 0
        self.flushed_at = self.saved_at = now
        st["runs"] = int(st.get("runs", 0)) + 1
        st["first_run"] = st.get("first_run") or stamp
        st["last_run"] = stamp
        st["current"] = {"start": stamp, "secs": 0, "flushed_at": now}
        self.run_no = st["runs"]
        self._save()
        return self.handle({"type": "run", "kind": "start"}, now)

    def handle(self, ev: dict, now: float) -> Plan:
        plan = plan_event(ev, self.settings(), self.ctx(now))
        for key in plan.tiles:
            self.counts[key] = self.counts.get(key, 0) + 1
        if self.live:
            for key in plan.stats:
                try:
                    S.stats_bump(self.stats, key)
                except KeyError:
                    pass
        if plan.world:
            self.world = plan.world
        for found in (plan.rare, plan.found):
            den = (found or {}).get("den")
            if den and (getattr(self, "rarest", None) is None or den > self.rarest[1]):
                self.rarest = (found["name"], den)
        if plan.end and self.live:
            self.end_reason, self.end_detail = plan.end
        if plan.error:
            self.last_error = (plan.error, now)
        if isinstance(ev, dict) and self._narrates(ev):
            self._narrated_at = now
        self.dispatch(plan.alerts)
        return plan

    # the last error line this close before the engine died says why
    ERROR_RECENT = 60.0

    def engine_stopped(self, why: str, now: float, crashed: bool = False) -> None:
        """The engine stopped by itself (not Stop or F2). Its own stop is
        either 'couldn't rejoin' (already the reason) or a plain stop with a
        why. Its errors are logged as 'the macro keeps going', so only a
        worker that DIED counts as stopping on an error."""
        if not self.live or self.end_reason in ("stuck", "error"):
            return
        if crashed:
            recent = self.last_error and now - self.last_error[1] <= self.ERROR_RECENT
            self.end_reason = "error"
            self.end_detail = self.last_error[0] if recent else why
        elif why:
            self.end_detail = why

    def tick(self, now: float) -> Plan | None:
        """Once a second during a real run: playtime into stats.json (saved
        once a minute) and the hourly alert."""
        if not self.live:
            return None
        try:
            due = now - self.saved_at >= 60 or now < self.saved_at
            self._flush(now, save=due)
        except Exception:
            pass
        h = int(max(0.0, now - (self.started or now)) // 3600)
        if self.hour_sent is not None and h > self.hour_sent:
            self.hour_sent = h
            return self.handle({"type": "run", "kind": "hourly", "hours": h,
                                "ran": now - (self.started or now)}, now)
        return None

    def end(self, now: float, kind: str = "stop", ran: float | None = None) -> Plan | None:
        """The run ended (Stop, F2, the engine stopping, or closing the app):
        final stats, the longest run, then the stop / error alert."""
        if not self.live:
            return None
        st = self.stats
        try:
            self._flush(now)
            cur = st.get("current") or {}
            secs = cur.get("secs", 0)
            if secs > st.get("longest_secs", 0):
                st["longest_secs"] = secs
                st["longest_on"] = str(cur.get("start", ""))[:10]
            st["current"] = None
        except Exception:
            pass
        ran = (now - self.started if self.started else 0.0) if ran is None else ran
        plan = None
        try:
            plan = self.handle({"type": "run", "kind": kind, "reason": self.end_reason,
                                "detail": self.end_detail, "ran": ran}, now)
        finally:
            self._save()
            self.live = False
            self.hour_sent = None
        return plan

    def _flush(self, now: float, save: bool = False) -> None:
        """Add the time since the last tick (0-5 s, so a clock that jumps
        can't add hours)."""
        st = self.stats
        d = min(5.0, max(0.0, now - self.flushed_at))
        S.stats_add_time(st, now - d, now)
        self.flushed_at = now
        cur = st.get("current")
        if cur is not None:
            cur["secs"] = cur.get("secs", 0) + d
            if save:
                cur["flushed_at"] = now
        if save:
            self._save()
            self.saved_at = now

    def _save(self) -> None:
        try:
            S.save_stats(self.stats, self.stats_path)
        except Exception:
            pass                             # stats never stop a run

    def unflushed(self, now: float) -> float:
        """Seconds of the live run not yet added to stats.json."""
        return min(5.0, max(0.0, now - self.flushed_at)) if self.live else 0.0

    # ---- Discord ----
    def sender_for(self, url: str):
        if self.sender is None or getattr(self.sender, "url", None) != url:
            self.kill_sender()
            self.sender = self._make_sender(url)
        return self.sender

    def dispatch(self, alerts: list) -> int:
        return dispatch(alerts, self.settings(), self.sender_for)

    def kill_sender(self) -> None:
        """Alerts off or a different link: whatever is still queued or waiting
        to retry is thrown away, never posted to the old link."""
        s, self.sender = self.sender, None
        if s is not None:
            s.dead = True                    # ends _send's retries and _loop
            try:
                s._discard()
            except Exception:
                pass

    def flush_sender(self, timeout: float = 2.0) -> None:
        if self.sender is not None:
            try:
                self.sender.flush(timeout)
            except Exception:
                pass


# ======================================================================
# The window
# ======================================================================
class GlassButton(ctk.CTkFrame):
    """The big Start / Stop button: a glyph, a word and an F1 keycap.

    A CTkButton holds one string, and 'Start    F1' spaced out by hand read
    like a placeholder. This is a frame that behaves like a button. Its
    configure() takes the options the app sets and passes anything else (the
    bg_color a parent frame pushes down) straight through.
    """

    def __init__(self, master, command, font, glyph_font, key_font,
                 height=46, corner_radius=12, width=None, key="F1"):
        size = {"width": width} if width else {}
        super().__init__(master, height=height, corner_radius=corner_radius,
                         border_width=1, fg_color=ACCENT, border_color=ACCENT,
                         **size)
        self._command = command
        self._fill, self._hover = ACCENT, ACCENT_SOFT
        self._hovering = False
        self._row = ctk.CTkFrame(self, fg_color="transparent")
        self._row.place(relx=0.5, rely=0.5, anchor="center")
        self._glyph = ctk.CTkLabel(self._row, text="▶", font=glyph_font,
                                   text_color=INK, height=20)
        self._glyph.pack(side="left", padx=(0, 8))
        self._text = ctk.CTkLabel(self._row, text="Start", font=font,
                                  text_color=INK, height=20)
        self._text.pack(side="left")
        self._key = ctk.CTkFrame(self._row, corner_radius=6, border_width=1,
                                 fg_color=PANEL, border_color=KEYCAP_LINE)
        self._key.pack(side="left", padx=(12, 0))
        self._keytext = ctk.CTkLabel(self._key, text=key, font=key_font,
                                     text_color=SUBTLE, height=16)
        self._keytext.pack(padx=7, pady=2)
        for w in (self, self._row, self._glyph, self._text, self._key,
                  self._keytext):
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")
            w.bind("<ButtonRelease-1>", self._click, add="+")
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def _paint_fill(self, colour):
        super().configure(fg_color=colour)
        for w in (self._row, self._glyph, self._text, self._key):
            try:
                w.configure(bg_color=colour)
            except Exception:
                pass

    def _inside(self):
        x, y = self.winfo_pointerxy()
        return (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y < self.winfo_rooty()
                + self.winfo_height())

    def _enter(self, _e=None):
        if not self._hovering:
            self._hovering = True
            self._paint_fill(self._hover)

    def _leave(self, _e=None):
        # moving from the frame onto its own label fires Leave too
        if self._hovering and not self._inside():
            self._hovering = False
            self._paint_fill(self._fill)

    def _click(self, _e=None):
        if self._inside() and self._command:
            self._command()

    def configure(self, **kw):
        mine = {}
        for k in ("text", "glyph", "fg_color", "hover_color", "text_color",
                  "glyph_color", "border_color", "key_fg", "key_border",
                  "key_text", "key"):
            if k in kw:
                mine[k] = kw.pop(k)
        if kw:
            super().configure(**kw)
        if "text" in mine:
            self._text.configure(text=mine["text"])
        if "glyph" in mine:
            self._glyph.configure(text=mine["glyph"])
        if "text_color" in mine:
            self._text.configure(text_color=mine["text_color"])
        if "glyph_color" in mine:
            self._glyph.configure(text_color=mine["glyph_color"])
        if "border_color" in mine:
            super().configure(border_color=mine["border_color"])
        if "key_fg" in mine:
            self._key.configure(fg_color=mine["key_fg"])
            self._keytext.configure(bg_color=mine["key_fg"])
        if "key_border" in mine:
            self._key.configure(border_color=mine["key_border"])
        if "key_text" in mine:
            self._keytext.configure(text_color=mine["key_text"])
        if "key" in mine:
            self._keytext.configure(text=mine["key"])
        if "hover_color" in mine:
            self._hover = mine["hover_color"]
        if "fg_color" in mine:
            self._fill = mine["fg_color"]
        if "fg_color" in mine or "hover_color" in mine:
            self._paint_fill(self._hover if self._hovering else self._fill)

    config = configure


EngineFactory = Callable[[Callable[[], dict], Callable[[dict], None]], Any]


# The real engine, made on the first Start and kept for the app's whole life:
# its hourly rejoin cap must survive Stop and Start, and its OCR engine and
# Roblox log reader are reused instead of piling up one per run.
_real_engine: list = []


def default_engine(get_settings: Callable[[], dict],
                   emit: Callable[[dict], None]):
    """The real run loop, imported only when Start is first pressed. Never
    while testing: tests and the screenshot tool pass in a fake engine."""
    safety.guard("start the real engine")
    if _real_engine:
        eng = _real_engine[0]
        eng.listen(get_settings, emit)       # this run's settings and events
        return eng
    from mayhem.engine import Engine
    eng = Engine(get_settings, emit)
    _real_engine.append(eng)
    return eng


def release_engine() -> None:
    """At exit: free the real engine's OCR event loop (if one was made)."""
    for eng in _real_engine:
        try:
            eng.deps.ocr.close()
        except Exception:
            pass


class MayhemMacro(ctk.CTk):
    def __init__(self, engine_factory: EngineFactory | None = None):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)        # must stay APP_NAME: claim_single_instance finds it by title
        # a plain file read - before the window is sized, so the saved size
        # and place can be used
        self.settings = self._load_settings()
        self._fit_to_screen()
        self.configure(fg_color=BG)
        self._set_icon()
        self.after(50, self._style_titlebar)

        self.session_start: float | None = None
        self.running = False
        self._engine = None
        self._engine_factory = engine_factory or default_engine
        self._stopping: threading.Thread | None = None
        self._run_seq = 0
        self._q: queue.Queue = queue.Queue()         # callables from any thread
        self._events: queue.Queue = queue.Queue()    # (run seq, event) from the engine
        # what the engine reads (from its own thread): a copy, swapped whole
        # on every change, so it never sees a dict half-way through an edit
        self._snap = copy.deepcopy(self.settings)
        self._save_job = None
        self.session = Session(lambda: self.settings, log=self._hook_log)
        self._init_updates()

        self._build()
        self._drain()
        self._hotkey_handle = self._register_hotkey()
        self.log(f"--- {APP_NAME} v{APP_VER} opened ---")
        self._warn_display()
        self._after_update()
        self._start_update_checks()
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- cross-thread UI. tkinter's after() is not thread safe, so workers
    # -- post callables (and the engine posts events) and the Tk thread runs them.
    def _ui(self, fn):
        try:
            self._q.put_nowait(fn)
        except Exception:
            pass

    def _emit_for(self, seq: int) -> Callable[[dict], None]:
        """The engine's emit(): any thread, never blocks, tagged with the run
        it belongs to so a stopped engine's last words can't change the next
        run."""
        def emit(event: dict) -> None:
            try:
                self._events.put_nowait((seq, event))
            except Exception:
                pass
        return emit

    def _drain(self):
        for _ in range(40):
            try:
                self._q.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                pass
        self._drain_events(60)
        self.after(50, self._drain)

    def _drain_events(self, limit: int = 1000):
        """Hand queued engine events to _on_event (Tk thread only)."""
        for _ in range(limit):
            try:
                seq, ev = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                self._on_event(seq, ev)
            except Exception as exc:
                write_log_line(f"event not shown ({type(exc).__name__}: {exc})")

    # ---------------------------------------------------------- settings --
    def _load_settings(self) -> dict:
        try:
            s = paths.load_settings()
        except Exception:
            # testing without a temp folder, or the folder is unreadable
            s = copy.deepcopy(paths.DEFAULT_SETTINGS)
        ui = s.get("ui") if isinstance(s.get("ui"), dict) else {}
        s["ui"] = ui
        s["anti_afk_after_min"] = clamp_minutes(s.get("anti_afk_after_min"))
        s["vote_maps"] = clean_ranking(s.get("vote_maps"))
        return s

    def _ui_set(self, key: str, default=None):
        ui = self.settings.get("ui")
        return ui.get(key, default) if isinstance(ui, dict) else default

    def _changed(self, save_now: bool = False):
        """A setting changed: the engine sees it at once, the file a moment
        later (a burst of clicks is one write)."""
        self._snap = copy.deepcopy(self.settings)
        if self._save_job:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        if save_now:
            self._save_settings()
        else:
            self._save_job = self.after(400, self._save_settings)

    def _save_settings(self):
        self._save_job = None
        try:
            if not paths.save_settings(self.settings):
                self.log("couldn't save settings.json", "warn")
        except Exception:
            pass                       # testing without a data folder

    def _get_settings(self) -> dict:
        """What the engine reads, from its own thread."""
        return self._snap

    # ---------------------------------------------------------------- ui --
    def _set_icon(self):
        """The cube. An .ico shipped with a build wins; from source the cube
        is drawn here, so no image file is needed."""
        ico = resource("mayhemmacro.ico")
        try:
            if os.path.exists(ico):
                self.iconbitmap(ico)
                # CustomTkinter puts its own icon back shortly after start-up
                self.after(250, lambda: self.iconbitmap(ico))
                return
            self._icons = [ImageTk.PhotoImage(cube_image(n), master=self)
                           for n in (64, 32, 16)]
            self._iconbitmap_method_called = True    # keeps CTk's icon away
            self.iconphoto(True, *self._icons)
            self.after(250, lambda: self.iconphoto(True, *self._icons))
        except Exception:
            pass

    def _style_titlebar(self):
        """Colour the Windows title bar to match, so it reads as one piece.
        Windows 11 only; older versions keep the normal dark bar."""
        def ref(hex_colour):                      # COLORREF is 0x00BBGGRR
            r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
            return ctypes.c_int(b << 16 | g << 8 | r)
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            for attr, val in ((20, ctypes.c_int(1)),     # dark mode
                              (35, ref(PANEL)),          # caption
                              (36, ref(SUBTLE)),         # caption text
                              (34, ref(LINE))):          # border
                dwm(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _fit_to_screen(self, want_w=985, want_h=560):
        """985x560 where it fits; smaller where it doesn't.

        A 1080p laptop at the usual 150% scaling has only about 688 logical
        pixels above the taskbar, so the size is capped by the work area.
        CustomTkinter scales the size but not the position, so the position
        is worked out in real pixels.
        """
        try:
            s = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            s = 1.0
        try:
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0,
                                                       ctypes.byref(r), 0)
            left, top, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            left, top, (w, h) = 0, 0, screen_size()
        # never below the minimum size: Tk enforces it anyway, and centring
        # a smaller size would push the bottom under the taskbar
        width = max(640, min(want_w, int(w / s) - 16))
        height = max(500, min(want_h, int(h / s) - 48))  # title bar, air
        # under 830 across the sidebar folds to a 56px rail, so 640 still fits
        self.minsize(640, 500)
        x = left + max(0, (w - round(width * s)) // 2)
        y = top + max(0, (h - round((height + 32) * s)) // 2)
        saved = self._saved_window(s)
        if saved:
            width, height, x, y = saved
        self._win_w, self._win_h = width, height
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _saved_window(self, s):
        """Last time's size and place, when Remember is on and at least 120px
        of it both ways is still inside the work area of a screen (a monitor
        that was unplugged since must not leave the window out of reach)."""
        if self._ui_set("remember", True) is False:
            return None
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
                         str(self._ui_set("geometry", "") or ""))
        if not m:
            return None
        ww, hh, x, y = (int(g) for g in m.groups())
        ww, hh = max(640, ww), max(500, hh)
        pw, ph = round(ww * s), round((hh + 32) * s)    # real px, title bar
        try:
            class _MI(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("mon", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            rc = wintypes.RECT(x, y, x + pw, y + ph)
            mon = ctypes.windll.user32.MonitorFromRect(ctypes.byref(rc), 2)
            mi = _MI()
            mi.size = ctypes.sizeof(_MI)
            if not ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                return None
            wa = mi.work
            ox = min(x + pw, wa.right) - max(x, wa.left)
            oy = min(y + ph, wa.bottom) - max(y, wa.top)
            if ox < 120 * s or oy < 120 * s:
                return None
            # never bigger than that screen
            ww = max(640, min(ww, int((wa.right - wa.left) / s)))
            hh = max(500, min(hh, int((wa.bottom - wa.top) / s) - 32))
        except Exception:
            return None
        return ww, hh, x, y

    # ------------------------------------------------------ fonts & sizes --
    def _init_fonts(self):
        """Segoe UI Variable on Windows 11, plain Segoe UI on Windows 10.

        Tk only knows normal and bold, so semibold has to be its own family.
        """
        try:
            fams = set(tkfont.families(self))
        except Exception:
            fams = set()
        if "Segoe UI Variable Text" in fams:
            self._fam = "Segoe UI Variable Text"
        else:
            self._fam = "Segoe UI"
        if "Segoe UI Variable Text Semibold" in fams:
            self._fam_semi = "Segoe UI Variable Text Semibold"
        elif "Segoe UI Semibold" in fams:
            self._fam_semi = "Segoe UI Semibold"
        else:
            self._fam_semi = self._fam
        self._fam_sym = "Segoe UI Symbol" if "Segoe UI Symbol" in fams \
            else self._fam
        # sidebar icons: Windows 11's icon font, Windows 10's, or plain
        # Segoe UI Symbol glyphs when neither is there
        if "Segoe Fluent Icons" in fams:
            self._fam_icon = "Segoe Fluent Icons"
        elif "Segoe MDL2 Assets" in fams:
            self._fam_icon = "Segoe MDL2 Assets"
        else:
            self._fam_icon = None
        self._fonts = {}

    def F(self, size, semi=False, bold=False, family=None):
        key = (size, semi, bold, family)
        if key not in self._fonts:
            fam = family or (self._fam_semi if semi else self._fam)
            self._fonts[key] = ctk.CTkFont(family=fam, size=size,
                                           weight="bold" if bold else "normal")
        return self._fonts[key]

    def _px(self, n):
        """Scale a raw Tk (canvas / text tag) size for Windows display
        scaling. CustomTkinter scales its own widgets; plain Tk ones it
        does not."""
        try:
            return max(1, round(n * ctk.ScalingTracker.get_widget_scaling(self)))
        except Exception:
            return n

    def _tkfont(self, size, semi=False, family=None):
        fam = family or (self._fam_semi if semi else self._fam)
        return (fam, -self._px(size))

    def _icon(self, glyph: str, alt: str) -> str:
        """An icon-font glyph, or the plain Unicode one without that font."""
        return glyph if self._fam_icon else alt

    # ------------------------------------------------------------- build --
    def _build(self):
        self._init_fonts()
        self._run_t0 = None                 # start of the current / last run
        self._last_run = None
        self._dot_colour = MUTED
        self._run_mode = "IDLE"
        self._mode = None                   # the engine's state while running
        self._bar_votes, self._bar_dens = [], []
        self._bar_found = []
        self._bar_spawns = []
        self._state_title = ""
        self._state_since = 0.0
        self._feed_rows = 0
        self._feed_items = []               # newest first, merged like the feed
        self._feed_filter = "All"           # never saved: always starts at All
        self._log_follow = True
        self._log_query, self._log_mode = "", "All"
        self._log_shown = 0
        self._log_after = None
        self._rares = []                    # this session's rare spawns, newest first
        self._timer = None                  # (seconds, time.monotonic() it was read)
        self._storage = None                # (n, m)
        self._hud_world = None              # the world the engine says we're in
        self._dead_ticks = 0                # ticks the engine has looked dead
        # 1.1 polish: fades, the shimmer, toasts, the rail, badges
        self._tweens = {}                   # key -> (after id, colour now)
        self._state_detail = ""
        self._pill_pulse = False
        self._pill_colours = (MUTED, PANEL)
        self._anim_delay_now = 300
        self._anim_rest = set()             # what the clock already put away
        self._shim_mode = None
        self._rail = False
        self._force_full = False            # Ctrl+B opened the sidebar on a narrow window
        self._foot_shown = True
        self._unread = 0                    # Activity rows not seen yet
        self._log_alert = False             # an error line not seen yet
        self._page_pref = None              # the last page picked by hand
        self._win_save = None
        self._toast_job = None
        self._tip_job = None
        self._days_job = None
        self._days_geo = None
        self._tip_i = None
        self._reset_armed = None

        sw, sh = screen_size()
        self._screen_ok = (sw, sh) == SUPPORTED_SCREEN
        self._scaling = display_scaling()

        # ---- the window: sidebar | 1px line | header / hairline / pages ----
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_sidebar(sw, sh)
        ctk.CTkFrame(self, width=1, height=1, fg_color=LINE, corner_radius=0
                     ).grid(row=0, column=1, rowspan=3, sticky="ns")
        self._build_header()
        # the hairline under the header; a shimmer slides along it while a
        # run is going (items made once here, the clock only moves them)
        self.hair = tk.Canvas(self, width=1, height=self._px(2), bg=HAIRLINE,
                              highlightthickness=0, bd=0)
        self.hair.grid(row=1, column=2, sticky="ew")
        self._shim = [self.hair.create_rectangle(-99, 0, -98, self._px(2),
                                                 fill=HAIRLINE, outline="")
                      for _ in range(SHIMMER_RECTS)]
        # every page is built once and stays alive; switching only raises
        # one over the others
        self.stack = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.stack.grid(row=2, column=2, sticky="nsew")
        self.stack.grid_rowconfigure(0, weight=1)
        self.stack.grid_columnconfigure(0, weight=1)
        self._pages = {}
        for key in self.PAGE_INFO:
            page = ctk.CTkFrame(self.stack, fg_color=BG, corner_radius=0)
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[key] = page
        self._page = None

        self._build_home(self._pages["home"])
        self._build_activity(self._pages["activity"])
        self._build_alerts(self._pages["alerts"])
        self._build_vote(self._pages["vote"])
        self._build_server(self._pages["server"])
        self._build_discord(self._pages["discord"])
        self._build_stats(self._pages["stats"])
        self._build_log(self._pages["log"])
        self._build_settings(self._pages["settings"])
        self._build_about(self._pages["about"])
        self._paint_res(sw, sh, self._scaling)
        self._build_toast()
        self._build_rail_tip()
        self._bind_keys()
        self.bind("<Configure>", self._root_configured, add="+")
        self._layout_for(self._win_w, self._win_h)

        self._show_page(self._start_page(), remember=False)
        self._show_ready()
        self._show_playtime(None)
        self._paint_hud()
        self._tick()
        self._animate()
        if (self._ui_set("remember", True) is not False
                and self._ui_set("zoomed") is True):
            self.after(60, lambda: self.state("zoomed"))

    # ---- the window shell: sidebar, header, pages ----
    # key: (title, header subtitle, sidebar group, icon-font glyph, fallback)
    PAGE_INFO = {
        "home": ("Home", "What it's doing right now",
                 "RUN", "", "⌂"),
        "activity": ("Activity", "What happened, newest first",
                     "RUN", "", "◷"),
        "alerts": ("Alerts", "Rare blocks and a full storage",
                   "SET UP", "", "!"),
        "vote": ("World vote", "Votes for the maps you want",
                 "SET UP", "", "◎"),
        "server": ("Server", "Rejoining and the AFK kick",
                   "SET UP", "", "↻"),
        "discord": ("Discord", "Alerts in your own Discord channel",
                    "SET UP", "", "✉"),
        "stats": ("Stats", "Totals from every run",
                  "HISTORY", "", "▤"),
        "log": ("Log", "Every line it writes, for bug reports",
                "HISTORY", "", "≡"),
        "settings": ("Settings", "How the app behaves",
                     "APP", "", "⚙"),
        "about": ("About", f"{APP_NAME} {APP_VER}",
                  "APP", "", "ⓘ"),
    }

    def _build_sidebar(self, sw, sh):
        side = ctk.CTkFrame(self, width=220, fg_color=PANEL, corner_radius=0)
        side.grid(row=0, column=0, rowspan=3, sticky="ns")
        side.grid_propagate(False)
        side.pack_propagate(False)
        self.sidebar = side

        # brand: cube, wordmark, version
        brand = ctk.CTkFrame(side, fg_color="transparent", height=52)
        brand.pack(fill="x", padx=20)
        brand.pack_propagate(False)
        cube = tk.Canvas(brand, width=self._px(20), height=self._px(20),
                         bg=PANEL, highlightthickness=0, bd=0)
        draw_cube(cube, 0, 0, self._px(20))
        cube.pack(side="left")
        self._brand = brand
        self._brand_words = []
        for text, colour, font, pad in (
                ("Mayhem", ACCENT, self.F(15, semi=True), {"padx": (8, 0)}),
                ("Macro", TEXT, self.F(15, semi=True), {}),
                (APP_VER, MUTED, self.F(11), {"padx": (6, 0),
                                              "pady": (3, 0)})):
            w = ctk.CTkLabel(brand, text=text, text_color=colour, font=font)
            w.pack(side="left", **pad)
            self._brand_words.append((w, pad))

        # footer, packed before the nav so a short window squeezes the nav
        # and never pushes the screen line off the end
        foot = ctk.CTkFrame(side, fg_color="transparent", height=44)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        ctk.CTkFrame(foot, width=1, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(side="top", fill="x", padx=14)
        self._side_foot = foot
        self.lbl_res = ctk.CTkLabel(foot, font=self.F(11), text="",
                                    anchor="w", justify="left",
                                    wraplength=184, height=16)
        self.lbl_res.pack(side="left", padx=20)
        self._build_update_link(foot)

        nav = ctk.CTkFrame(side, fg_color="transparent")
        nav.pack(fill="both", expand=True)
        self._nav_frame = nav
        self._nav = {}
        self._nav_heads = []                # (label, rail line, first row)
        group = None
        icon_font = self.F(14, family=self._fam_icon or self._fam_sym)
        for key, (title, _sub, grp, glyph, alt) in self.PAGE_INFO.items():
            head = None
            line = None
            if grp != group:
                head = ctk.CTkLabel(nav, text=grp, text_color=MUTED,
                                    anchor="w", font=self.F(10, semi=True),
                                    height=14)
                head.pack(fill="x", padx=22,
                          pady=(0 if group is None else 8, 4))
                # in the rail a short line stands in for the group's name
                line = None if group is None else ctk.CTkFrame(
                    nav, width=1, height=1, fg_color=HAIRLINE,
                    corner_radius=0)
                group = grp
            row = ctk.CTkFrame(nav, height=32, corner_radius=8,
                               fg_color=PANEL)
            row.pack(fill="x", padx=10, pady=1)
            row.pack_propagate(False)
            if head is not None:
                self._nav_heads.append((head, line, row))
            icon = ctk.CTkLabel(row, text=self._icon(glyph, alt),
                                width=20, height=20, text_color=SUBTLE,
                                font=icon_font)
            icon.pack(side="left", padx=(12, 0))
            lbl = ctk.CTkLabel(row, text=title, text_color=SUBTLE,
                               font=self.F(13), anchor="w", height=20)
            lbl.pack(side="left", padx=(10, 0))
            # badges: a count / "On" pill, or a dot (made once, placed when
            # there is something to show)
            pill = ctk.CTkLabel(row, text="", width=22, height=18,
                                corner_radius=9, fg_color=ACCENT_DIM,
                                text_color=ACCENT, font=self.F(10, semi=True))
            dot = ctk.CTkFrame(row, width=8, height=8, corner_radius=4,
                               fg_color=AMBER)
            for w in (row, icon, lbl, pill, dot):
                w.bind("<Enter>", lambda _e, k=key: self._nav_hover(k, True),
                       add="+")
                w.bind("<Leave>", lambda _e, k=key: self._nav_hover(k, False),
                       add="+")
                w.bind("<ButtonRelease-1>",
                       lambda _e, k=key: self._nav_click(k), add="+")
                try:
                    w.configure(cursor="hand2")
                except Exception:
                    pass
            self._nav[key] = {"row": row, "icon": icon, "label": lbl,
                              "pill": pill, "dot": dot, "badge": None,
                              "hover": False, "painted": None, "fill": PANEL}
        # the 3x16 bar beside the selected row. Plain Tk: place() will not
        # size a CustomTkinter widget
        self._nav_ind = tk.Frame(nav, bg=ACCENT, bd=0, highlightthickness=0)
        self._ind_job = None

    def _nav_inside(self, key):
        row = self._nav[key]["row"]
        x, y = self.winfo_pointerxy()
        rx, ry = row.winfo_rootx(), row.winfo_rooty()
        return (rx <= x < rx + row.winfo_width()
                and ry <= y < ry + row.winfo_height())

    def _nav_hover(self, key, on):
        # moving from the row onto its own label fires Leave too
        if not on and self._nav_inside(key):
            return
        if self._nav[key]["hover"] == on:
            return
        self._nav[key]["hover"] = on
        self._paint_nav()
        self._rail_tip(key if on else None)

    def _nav_click(self, key):
        if self._nav_inside(key):
            self._rail_tip(None)
            self._show_page(key)

    def _paint_nav(self, slide=True):
        for key, n in self._nav.items():
            on = key == self._page
            fill = (NAV_ACTIVE if on else
                    NAV_HOVER if n["hover"] else PANEL)
            if n["painted"] == (on, fill):
                continue
            # hover fades; becoming (or no longer being) the page is instant,
            # so a page switch doesn't queue redraws behind the new page
            hover_only = n["painted"] is not None and n["painted"][0] == on
            n["painted"] = (on, fill)
            row = n["row"]
            self._tween("nav:" + key, lambda c, r=row: r.configure(fg_color=c),
                        n["fill"], fill, steps=6 if hover_only else 1)
            n["fill"] = fill
            n["icon"].configure(text_color=ACCENT if on else SUBTLE)
            n["label"].configure(text_color=TEXT if on else SUBTLE,
                                 font=self.F(13, semi=on))
        self._slide_indicator(slide)

    def _slide_indicator(self, slide=True):
        """The 3x16 bar glides to the selected row: 6 frames of 16 ms,
        easing out. It ends pinned to the row itself, so a resize or the
        rail can't leave it behind."""
        if self._page not in self._nav:
            return
        row = self._nav[self._page]["row"]
        ind = self._nav_ind
        size = {"width": self._px(3), "height": self._px(16)}
        if self._ind_job:
            try:
                self.after_cancel(self._ind_job)
            except Exception:
                pass
            self._ind_job = None
        try:
            to = row.winfo_y() + row.winfo_height() / 2
            frm = ind.winfo_y() + self._px(16) / 2
            ok = (slide and self._motion() and ind.winfo_ismapped()
                  and row.winfo_height() > 1 and abs(frm - to) >= 2)
        except Exception:
            ok = False

        def pin():
            ind.place(in_=row, x=0, y=0, rely=0.5, anchor="w", **size)
            ind.lift()
        if not ok:
            pin()
            return

        t0 = []

        def step(i):
            # 6 frames of 16 ms, but placed by the clock, so a frame that
            # comes late lands where it should be by then
            self._ind_job = None
            now = time.perf_counter()
            if not t0:
                t0.append(now - 0.016)
            t = (now - t0[0]) / 0.096
            if t >= 1 or i > 12:
                pin()
                return
            e = 1 - (1 - t) ** 3
            try:
                ind.place(in_=self._nav_frame, x=row.winfo_x(), rely=0,
                          y=frm + (to - frm) * e, anchor="w", **size)
                ind.lift()
            except Exception:
                pin()
                return
            self._ind_job = self.after(16, step, i + 1)
        # once the new page has drawn - raising a page is a big redraw, and
        # frames queued behind it would all land at once
        self._ind_job = self.after_idle(step, 1)

    def _build_header(self):
        head = ctk.CTkFrame(self, height=56, fg_color=PANEL, corner_radius=0)
        head.grid(row=0, column=2, sticky="ew")
        head.grid_propagate(False)
        head.pack_propagate(False)
        self.header = head
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(24, 0))
        self.lbl_title = ctk.CTkLabel(titles, text="", text_color=TEXT,
                                      font=self.F(16, semi=True), anchor="w",
                                      height=22)
        self.lbl_title.pack(fill="x")
        self.lbl_sub = ctk.CTkLabel(titles, text="", text_color=MUTED,
                                    font=self.F(11), anchor="w", height=16)
        self.lbl_sub.pack(fill="x")

        # right to left: Start/Stop, the pin, the state pill
        self.btn_run = GlassButton(head, self._run_clicked,
                                   font=self.F(13, semi=True),
                                   glyph_font=self.F(10, family=self._fam_sym),
                                   key_font=self.F(10, semi=True,
                                                   family="Consolas"),
                                   height=36, corner_radius=10, width=156)
        self.btn_run.pack(side="right", padx=(0, 20))   # = the cards' edge
        self.btn_pin = ctk.CTkButton(
            head, text="", width=36, height=36, corner_radius=10,
            fg_color="transparent", hover_color=CARD_HI, border_width=1,
            border_color=LINE, text_color=SUBTLE,
            font=self.F(13, family=self._fam_icon or self._fam_sym),
            command=self._toggle_pin)
        self.btn_pin.pack(side="right", padx=(0, 10))
        self.pill = ctk.CTkFrame(head, height=26, corner_radius=13,
                                 border_width=1, fg_color=PANEL,
                                 border_color=LINE)
        self.pill.pack(side="right", padx=(0, 10))
        d, r = self._px(10), self._px(4)
        self.pill_dot = tk.Canvas(self.pill, width=d, height=d, bg=PANEL,
                                  highlightthickness=0, bd=0)
        self.pill_dot.pack(side="left", padx=(10, 5))
        self._pill_core = self.pill_dot.create_oval(d / 2 - r, d / 2 - r,
                                                    d / 2 + r, d / 2 + r,
                                                    fill=MUTED, outline="")
        self.lbl_pill = ctk.CTkLabel(self.pill, text="Idle",
                                     text_color=SUBTLE, height=18,
                                     font=self.F(11, semi=True))
        self.lbl_pill.pack(side="left", padx=(0, 12), pady=4)
        self._pill_painted = None
        self._paint_pin()
        if self._ui_set("pin"):
            self._apply_topmost()

    def _show_page(self, key, remember=True):
        page = self._pages.get(key)
        if page is None:
            return
        self._page = key
        page.lift()
        title, sub = self.PAGE_INFO[key][:2]
        self.lbl_title.configure(text=title)
        self.lbl_sub.configure(text=sub)
        self._paint_nav()
        if key == "activity":
            self._unread = 0
        elif key == "log":
            self._log_alert = False
        self._paint_badges()
        if remember:
            self._page_pref = key
            self._queue_window_save()
        if key == "log" and self._log_follow:
            try:
                self.txt.see("end")
            except Exception:
                pass
        elif key == "stats":
            self._paint_stats()
            self._draw_days(True)

    def _start_page(self):
        """The page it was on last time (when Remember is on), or Home."""
        page = self._ui_set("page")
        if self._ui_set("remember", True) is not False and page in self.PAGE_INFO:
            return page
        return "home"

    def _paint_pill(self):
        """Idle / Running / Paused... worked out fresh from the state the
        card already shows, so the two can never disagree."""
        try:
            if getattr(self, "_updating", False):
                text, colour = "Updating", ACCENT
            elif self._run_mode == "RUNNING" and self._mode == "paused":
                text, colour = "Paused", AMBER
            elif self._run_mode == "RUNNING" and self._mode == "rejoining":
                text, colour = "Rejoining", AMBER
            elif self._run_mode == "RUNNING" and self._mode == "waiting":
                text, colour = "Waiting", ACCENT
            elif self._run_mode == "RUNNING":
                text = ("Running · " + span(time.time() - self.session_start)
                        if self.session_start else "Running")
                colour = GREEN
            else:
                text, colour = "Idle", None
            if colour is None:
                fill, border, dot, fg = PANEL, LINE, MUTED, SUBTLE
            else:
                fill, border = blend(PANEL, colour, 0.10), blend(PANEL, colour,
                                                                0.40)
                dot, fg = colour, colour
            # the dot breathes while something is happening
            self._pill_pulse = colour is not None
            self._pill_colours = (dot, fill)
            if self._pill_painted == (text, colour):
                return
            self._pill_painted = (text, colour)
            self.pill.configure(fg_color=fill, border_color=border)
            self.pill_dot.configure(bg=fill)
            self.pill_dot.itemconfigure(self._pill_core, fill=dot)
            self.lbl_pill.configure(text=text, text_color=fg)
        except Exception:
            pass

    # ---- keep on top ----
    def _toggle_pin(self):
        ui = self.settings.setdefault("ui", {})
        ui["pin"] = not ui.get("pin", False)
        self._changed()
        self._apply_topmost()
        self._paint_pin()
        self._toast("Kept on top while idle · it steps back during a run"
                    if ui["pin"] else "No longer kept on top", "info")

    def _apply_topmost(self):
        """Keep on top only while idle: during a run it would sit over
        Roblox, and the screen reading would see this window instead.

        Only when it changes: Tk sets it with SetWindowPos, which can also
        activate the window - and a Stop pressed with F2 must never pull the
        keyboard away from Roblox."""
        want = bool(self._ui_set("pin") and not self.running)
        if want == getattr(self, "_topmost", False):
            return
        try:
            # SWP_NOACTIVATE: Tk's own '-topmost' leaves it out, so pinning
            # again after F2 in Roblox could pull the keyboard to this window
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if hwnd:
                flags = 0x0001 | 0x0002 | 0x0010          # NOSIZE | NOMOVE | NOACTIVATE
                ctypes.windll.user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(-1 if want else -2),
                    0, 0, 0, 0, flags)
            else:
                self.attributes("-topmost", want)
            self._topmost = want
        except Exception:
            pass

    def _drop_pin(self):
        """Get out of Roblox's way for a run. Un-pinning alone leaves the
        window above every normal window (HWND_NOTOPMOST), so a pinned window
        is also pushed to the bottom - HWND_BOTTOM never activates it."""
        self._apply_topmost()
        if self._ui_set("pin"):
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                ctypes.windll.user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(1), 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010)              # HWND_BOTTOM, no activate
            except Exception:
                pass

    def _paint_pin(self):
        on = bool(self._ui_set("pin"))
        if self._fam_icon:
            glyph = "" if on else ""
        else:
            glyph = "▲" if on else "△"
        self.btn_pin.configure(text=glyph,
                               text_color=ACCENT if on else SUBTLE,
                               border_color=ACCENT if on else LINE,
                               fg_color=ACCENT_DIM if on else "transparent")
        sw = getattr(self, "sw_ontop", None)     # Settings mirrors the pin
        if sw is not None and bool(sw.get()) != on:
            sw.select() if on else sw.deselect()

    def _run_clicked(self):
        """The header button. F1 and F2 start and stop too."""
        self.toggle_run()

    # ---- fades ----
    def _tween(self, key, apply, a, b, ms=120, steps=6, fresh=False):
        """Fade a colour from a to b in a few steps, calling apply(colour)
        each time. A new fade on the same key replaces the old one and starts
        from wherever that had got to (fresh=True starts over at a)."""
        job = self._tweens.pop(key, None)
        if job:
            try:
                self.after_cancel(job[0])
            except Exception:
                pass
            if not fresh:
                a = job[1]
        if a == b or steps < 2 or not self._motion():
            try:
                apply(b)
            except Exception:
                pass
            return
        gap = max(10, ms // steps)

        def step(i):
            c = blend(a, b, i / steps)
            try:
                apply(c)
            except Exception:
                self._tweens.pop(key, None)
                return
            if i >= steps:
                self._tweens.pop(key, None)
                return
            self._tweens[key] = (self.after(gap, step, i + 1), c)
        step(1)

    def _bump(self, lbl):
        """A counter that just went up flashes light amber, then settles."""
        end = TEXT if self.running else SUBTLE
        self._tween("bump:%d" % id(lbl),
                    lambda c: lbl.configure(text_color=c), ACCENT_SOFT, end,
                    ms=600, steps=12, fresh=True)

    # ---- toasts: one small note at a time, bottom-right, inside the window
    # (never a Windows pop-up, so nothing ever takes focus) ----
    TOAST_KINDS = {"ok": ("✓", GREEN), "info": ("●", ACCENT),
                   "warn": ("!", AMBER), "err": ("×", RED),
                   "rare": ("◆", RARE)}

    def _build_toast(self):
        t = ctk.CTkFrame(self, fg_color=CARD_HI, corner_radius=12,
                         border_width=1, border_color=LINE)
        row = ctk.CTkFrame(t, fg_color="transparent")
        row.pack(padx=(14, 16), pady=10)
        self._toast_glyph = ctk.CTkLabel(row, text="", width=16, height=18,
                                         font=self.F(12, family=self._fam_sym))
        self._toast_glyph.pack(side="left")
        self._toast_text = ctk.CTkLabel(row, text="", text_color=TEXT,
                                        font=self.F(12), height=18,
                                        anchor="w", justify="left",
                                        wraplength=380)
        self._toast_text.pack(side="left", padx=(8, 0))
        self._toast_act = ctk.CTkLabel(row, text="", text_color=ACCENT,
                                       font=self.F(12, semi=True), height=18,
                                       cursor="hand2")
        self._toast_act.bind("<Button-1>", lambda _e: self._toast_action())
        for w in (t, row, self._toast_glyph, self._toast_text):
            w.bind("<Button-1>", lambda _e: self._toast_hide(), add="+")
        self._toast_w = t
        self._toast_fn = None
        self._toast_anim = None

    def _toast(self, text, kind="info", action=None):
        """Show a note for 3.2 s. A new one replaces the one showing.
        action = (label, function) adds a link to it."""
        try:
            glyph, colour = self.TOAST_KINDS.get(kind,
                                                 self.TOAST_KINDS["info"])
            self._toast_glyph.configure(text=glyph, text_color=colour)
            self._toast_text.configure(text=text)
            if action:
                label, self._toast_fn = action
                self._toast_act.configure(text=label + "  ›")
                if not self._toast_act.winfo_manager():
                    self._toast_act.pack(side="left", padx=(14, 0))
            else:
                self._toast_fn = None
                self._toast_act.pack_forget()
            for job in (self._toast_job, self._toast_anim):
                if job:
                    self.after_cancel(job)
            self._toast_anim = None
            showing = bool(self._toast_w.winfo_manager())
            self._toast_slide(0 if showing or not self._motion() else 1)
            self._toast_job = self.after(3200, self._toast_hide)
        except Exception:
            pass

    def _toast_slide(self, i, steps=5):
        """Rises 12px into place over a few frames (i=0: straight there)."""
        self._toast_anim = None
        e = 1.0 if not i else 1 - (1 - min(i, steps) / steps) ** 3
        try:
            self._toast_w.place(relx=1.0, rely=1.0, anchor="se", x=-20,
                                y=-16 + 12 * (1 - e))
            self._toast_w.lift()
        except Exception:
            return
        if i and i < steps:
            self._toast_anim = self.after(16, self._toast_slide, i + 1)

    def _toast_hide(self):
        for job in (self._toast_job, self._toast_anim):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._toast_job = self._toast_anim = None
        try:
            self._toast_w.place_forget()
        except Exception:
            pass

    def _toast_action(self):
        fn = self._toast_fn
        self._toast_hide()
        if fn:
            fn()

    # ---- the narrow rail ----
    def _root_configured(self, e):
        """The window was resized or moved (children's events are ignored)."""
        if e.widget is not self or not self.winfo_ismapped():
            return
        try:
            s = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            s = 1.0
        self._layout_for(e.width / s, e.height / s)
        self._queue_window_save()

    def _layout_for(self, w, h):
        """Under 830 across the sidebar folds to a 56px rail of icons; from
        850 it opens again (in between it stays as it is, so dragging the
        edge doesn't make it flicker). Ctrl+B can pin the rail. Under 520
        high the sidebar's footer goes, so the nav keeps its room."""
        if w < 100 or h < 100:
            return
        self._win_w, self._win_h = w, h
        rail = self._auto_rail(w)
        if self._ui_set("sidebar_collapsed") is True:
            rail = True
        elif self._force_full:
            # opened by hand on a narrow window - only where it fits; under
            # 800 the header and tiles would clip
            rail = w < 800
        self._set_rail(rail)
        self._show_foot(not rail and h >= 520)

    def _auto_rail(self, w):
        return self._rail if 830 <= w < 850 else w < 830

    def _set_rail(self, rail):
        if rail == self._rail:
            return
        self._rail = rail
        self._rail_tip(None)
        self.sidebar.configure(width=56 if rail else 220)
        for w, pad in self._brand_words:
            if rail:
                w.pack_forget()
            else:
                w.pack(side="left", **pad)
        self._brand.pack(fill="x", padx=18 if rail else 20)
        for head, line, first in self._nav_heads:
            if rail:
                head.pack_forget()
                if line is not None:
                    line.pack(fill="x", padx=16, pady=(7, 6), before=first)
            else:
                if line is not None:
                    line.pack_forget()
                head.pack(fill="x", padx=22, pady=(0 if line is None else 8,
                                                   4), before=first)
        for n in self._nav.values():
            n["row"].pack(fill="x", padx=8 if rail else 10, pady=1)
            n["icon"].pack(side="left", padx=(10 if rail else 12, 0))
            if rail:
                n["label"].pack_forget()
            else:
                n["label"].pack(side="left", padx=(10, 0))
            n["badge"] = None                  # re-placed for the new shape
        self._paint_badges()
        self._slide_indicator(False)

    def _show_foot(self, show):
        """The sidebar footer (the screen line)."""
        if show == self._foot_shown:
            return
        self._foot_shown = show
        if show:
            self._side_foot.pack(side="bottom", fill="x",
                                 before=self._nav_frame)
        else:
            self._side_foot.pack_forget()

    def _build_rail_tip(self):
        """The rail's page names: a small frame inside the window (never a
        Toplevel, so it can't take focus), shown after 400 ms of hover."""
        tip = ctk.CTkFrame(self, fg_color=CARD_HI, corner_radius=8,
                           border_width=1, border_color=LINE)
        self._tip_lbl = ctk.CTkLabel(tip, text="", text_color=TEXT,
                                     font=self.F(12), height=18)
        self._tip_lbl.pack(padx=10, pady=4)
        self._tip = tip

    def _rail_tip(self, key):
        if self._tip_job:
            try:
                self.after_cancel(self._tip_job)
            except Exception:
                pass
            self._tip_job = None
        if key is None or not self._rail:
            self._tip.place_forget()
            return
        self._tip_job = self.after(400, lambda: self._rail_tip_show(key))

    def _rail_tip_show(self, key):
        self._tip_job = None
        n = self._nav.get(key)
        if not n or not self._rail or not n["hover"]:
            return
        try:
            s = ctk.ScalingTracker.get_widget_scaling(self)
            row = n["row"]
            x = (row.winfo_rootx() - self.winfo_rootx()
                 + row.winfo_width()) / s + 10
            y = (row.winfo_rooty() - self.winfo_rooty()
                 + row.winfo_height() / 2) / s
            text = self.PAGE_INFO[key][0]
            if key == "activity" and self._unread:
                text += f" · {self._unread} new"
            self._tip_lbl.configure(text=text)
            self._tip.place(x=x, y=y, anchor="w")
            self._tip.lift()
        except Exception:
            pass

    # ---- sidebar badges ----
    def _paint_badges(self):
        """Activity: unread count. Log: red dot after an error line.
        Discord: On. World vote: On while it votes. About: a dot when a
        newer version is out. In the rail every badge is a dot on the icon."""
        nav = getattr(self, "_nav", None)
        if not nav:
            return
        want = {}
        if self._unread:
            want["activity"] = ("9+" if self._unread > 9 else
                                str(self._unread), ACCENT_DIM, ACCENT)
        if self._log_alert:
            want["log"] = (None, None, RED)
        cfg = _discord_cfg(self.settings)
        if cfg.get("enabled") is True and D.normalize_hook(cfg.get("webhook")):
            want["discord"] = ("On", GREEN_DIM, GREEN)
        if self.settings.get("auto_vote") is True and self.settings.get("vote_maps"):
            want["vote"] = (str(len(self.settings["vote_maps"])), ACCENT_DIM, ACCENT)
        if getattr(self, "_update_info", None):
            want["about"] = (None, None, ACCENT)      # a newer version is out
        for key, n in nav.items():
            b = want.get(key)
            sig = (b, self._rail)
            if n["badge"] == sig:
                continue
            n["badge"] = sig
            pill, dot = n["pill"], n["dot"]
            try:
                if b is None:
                    pill.place_forget()
                    dot.place_forget()
                    continue
                text, fill, colour = b
                if text is None or self._rail:
                    pill.place_forget()
                    dot.configure(fg_color=colour)
                    if self._rail:
                        dot.place(relx=0, rely=0, x=27, y=6, anchor="nw")
                    else:
                        dot.place(relx=1.0, x=-14, rely=0.5, y=0, anchor="e")
                else:
                    dot.place_forget()
                    pill.configure(text=text, fg_color=fill, text_color=colour)
                    pill.place(relx=1.0, x=-8, rely=0.5, y=0, anchor="e")
            except Exception:
                pass

    # ---- keyboard: all inside the window. F1/F2 are the only global keys ----
    def _bind_keys(self):
        order = list(self.PAGE_INFO)
        for i, key in enumerate(order[:10], start=1):
            self.bind(f"<Control-Key-{i % 10}>",
                      lambda _e, k=key: self._key_page(k))
        self.bind("<Control-Tab>", lambda _e: self._key_cycle(1))
        for seq in ("<Control-Shift-Tab>", "<Control-ISO_Left_Tab>"):
            try:
                self.bind(seq, lambda _e: self._key_cycle(-1))
            except tk.TclError:
                pass
        for k in ("t", "T"):
            self.bind(f"<Control-{k}>", self._key_pin)
        for k in ("f", "F"):
            self.bind(f"<Control-{k}>", self._key_find)
        for k in ("b", "B"):
            self.bind(f"<Control-{k}>", self._key_sidebar)
        self.bind("<Escape>", self._key_escape)
        # Tk's Entry class binds Ctrl+T to "swap the last two characters"
        # and runs before the root binding. A binding on the entry itself
        # runs first and its "break" stops both.
        for e in (self.e_log, self.e_link, self.e_hook, self.e_uid):
            for k in ("t", "T"):
                e._entry.bind(f"<Control-{k}>", self._key_pin)

    def _key_page(self, key):
        self._show_page(key)
        return "break"

    def _key_cycle(self, step):
        order = list(self.PAGE_INFO)
        at = order.index(self._page) if self._page in order else 0
        self._show_page(order[(at + step) % len(order)])
        return "break"

    def _key_pin(self, _e=None):
        self._toggle_pin()
        return "break"

    def _key_find(self, _e=None):
        self._show_page("log")
        try:
            self.e_log.focus_set()
            self.e_log._entry.select_range(0, "end")
        except Exception:
            pass
        return "break"

    def _key_sidebar(self, _e=None):
        """Ctrl+B: the other sidebar. The rail is remembered; opening the
        full sidebar on a narrow window lasts until the app closes."""
        ui = self.settings.setdefault("ui", {})
        if self._rail:
            ui["sidebar_collapsed"] = False
            self._force_full = self._auto_rail(self._win_w)
        else:
            ui["sidebar_collapsed"] = True
            self._force_full = False
        self._changed()
        self._layout_for(self._win_w, self._win_h)
        return "break"

    def _key_escape(self, _e=None):
        self._toast_hide()
        self._rail_tip(None)
        try:
            if (self._page == "log" and self.e_log.get()
                    and self.focus_get() is self.e_log._entry):
                self.e_log.delete(0, "end")
                self._log_query_changed()
        except Exception:
            pass
        if self._reset_armed:
            self._disarm_reset()

    # ---- remember the window and page ----
    def _queue_window_save(self):
        """Saved a second after the last move, resize or page change."""
        if self._ui_set("remember", True) is False:
            return
        if self._win_save:
            try:
                self.after_cancel(self._win_save)
            except Exception:
                pass
        self._win_save = self.after(1000, self._save_window)

    def _save_window(self):
        self._win_save = None
        try:
            ui = self.settings.setdefault("ui", {})
            if ui.get("remember", True) is False:
                return
            st = self.state()
            if st in ("iconic", "withdrawn"):
                return
            changed = False
            zoomed = st == "zoomed"
            if bool(ui.get("zoomed")) != zoomed:
                ui["zoomed"] = zoomed
                changed = True
            if not zoomed:               # keep the size it un-maximises to
                geo = self.geometry()
                if re.fullmatch(r"\d+x\d+\+-?\d+\+-?\d+", geo) and geo != ui.get("geometry"):
                    ui["geometry"] = geo
                    changed = True
            if self._page_pref and ui.get("page") != self._page_pref:
                ui["page"] = self._page_pref
                changed = True
            if changed:
                self._changed()
        except Exception:
            pass

    # ---- page helpers ----
    def _scroll_page(self, page):
        """A scrolling page: a CTkScrollableFrame INSIDE the plain page
        frame (raising a scrollable frame itself does not work)."""
        sf = ctk.CTkScrollableFrame(page, fg_color="transparent",
                                    scrollbar_button_color=LINE,
                                    scrollbar_button_hover_color=MUTED)
        sf.pack(fill="both", expand=True, padx=(14, 4), pady=(0, 8))
        # CTk always shows the scrollbar. Keep its width (the cards stay in
        # line with the header) but paint it away while everything fits.
        shown = [True]

        def fit():
            try:
                need = sf._parent_canvas.yview() != (0.0, 1.0)
                if need != shown[0]:
                    shown[0] = need
                    sf.configure(
                        scrollbar_button_color=LINE if need else BG,
                        scrollbar_button_hover_color=MUTED if need else BG)
            except Exception:
                pass

        def later(_e=None):
            try:
                self.after_idle(fit)
            except Exception:
                pass
        sf.bind("<Configure>", later, add="+")
        sf._parent_canvas.bind("<Configure>", later, add="+")
        later()
        return sf

    def _card(self, parent, **kw):
        opts = dict(fg_color=CARD, corner_radius=16, border_width=1,
                    border_color=LINE)
        opts.update(kw)
        return ctk.CTkFrame(parent, **opts)

    def _sheen(self, card):
        """A 1px lighter line along the card's top edge - the glass edge."""
        # plain Tk: CustomTkinter will not size a widget from place()
        line = tk.Frame(card, height=1, bg=SHEEN, bd=0, highlightthickness=0)
        line.place(x=self._px(18), y=self._px(1), relwidth=1.0,
                   width=-self._px(36))

    def _group(self, parent, title):
        lbl = ctk.CTkLabel(parent, text=title, text_color=MUTED, anchor="w",
                           font=self.F(11, semi=True), height=16)
        lbl.pack(fill="x", padx=4, pady=(16, 6))
        return lbl

    def _ghost(self, parent, text, command):
        return ctk.CTkButton(parent, text=text, width=84, height=30,
                             corner_radius=10, fg_color="transparent",
                             hover_color=CARD_HI, border_width=1,
                             border_color=LINE, text_color=TEXT,
                             text_color_disabled=MUTED,
                             font=self.F(12), command=command)

    def _hairline(self, card):
        ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(fill="x", padx=1)

    def _switch_row(self, card, title, note, first=False, on=True,
                    command=None, chip=None):
        """A row with a title, a note under it and a switch on the right.
        chip adds a small tag after the title (e.g. 'BETA')."""
        if not first:
            self._hairline(card)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        sw = ctk.CTkSwitch(row, text="", width=40, switch_width=34,
                           switch_height=18, progress_color=ACCENT,
                           fg_color=LINE, button_color=TEXT,
                           button_hover_color=ACCENT_SOFT, command=command)
        sw.pack(side="right")
        if on:
            sw.select()
        else:
            sw.deselect()
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        head = ctk.CTkFrame(txt, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title, text_color=TEXT, font=self.F(13),
                     anchor="w", height=18).pack(side="left")
        if chip:
            ctk.CTkLabel(head, text=chip, text_color=ACCENT, fg_color=ACCENT_DIM,
                         corner_radius=6, height=16, width=40,
                         font=self.F(9, semi=True)).pack(side="left", padx=(8, 0))
        sw.note = ctk.CTkLabel(txt, text=note, text_color=SUBTLE,
                               font=self.F(11), anchor="w", justify="left",
                               height=16, wraplength=420)
        sw.note.pack(fill="x")
        self._rewrap_on(txt, (sw.note,))
        return sw

    def _check(self, parent, text, on, command, small=False):
        c = ctk.CTkCheckBox(parent, text=text, onvalue=True, offvalue=False,
                            checkbox_width=16 if small else 18,
                            checkbox_height=16 if small else 18,
                            corner_radius=5, border_width=1,
                            border_color=KEYCAP_LINE, fg_color=ACCENT,
                            hover_color=ACCENT_SOFT, checkmark_color=INK,
                            text_color=SUBTLE if small else TEXT,
                            font=self.F(11 if small else 12),
                            command=command)
        if on:
            c.select()
        else:
            c.deselect()
        return c

    def _link_row(self, card, title, note, command, first=False):
        """A row that opens something: title, note and a chevron."""
        if not first:
            self._hairline(card)
        row = ctk.CTkFrame(card, fg_color=CARD, corner_radius=12,
                           cursor="hand2")
        row.pack(fill="x", padx=2, pady=2)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        a = ctk.CTkLabel(txt, text=title, text_color=TEXT, font=self.F(13),
                         anchor="w", height=18)
        a.pack(fill="x")
        b = ctk.CTkLabel(txt, text=note, text_color=SUBTLE, font=self.F(11),
                         anchor="w", height=16)
        b.pack(fill="x")
        c = ctk.CTkLabel(inner, text="›", text_color=SUBTLE, font=self.F(16))
        c.pack(side="right")
        key = "link:%d" % id(row)
        for w in (row, inner, txt, a, b, c):
            w.bind("<Button-1>", lambda _e: command(), add="+")
            w.bind("<Enter>", lambda _e: self._tween(
                key, lambda col: row.configure(fg_color=col), CARD, CARD_HI),
                add="+")
            w.bind("<Leave>", lambda _e: self._link_leave(row, key), add="+")
        return row

    def _link_leave(self, row, key):
        x, y = self.winfo_pointerxy()
        rx, ry = row.winfo_rootx(), row.winfo_rooty()
        if not (rx <= x < rx + row.winfo_width()
                and ry <= y < ry + row.winfo_height()):
            self._tween(key, lambda col: row.configure(fg_color=col),
                        CARD_HI, CARD)

    def _segmented(self, parent, values, command):
        seg = ctk.CTkSegmentedButton(
            parent, values=values, command=command, height=28,
            corner_radius=8, font=self.F(12), fg_color=PANEL,
            selected_color=CARD_HI, selected_hover_color=CARD_HI,
            unselected_color=PANEL, unselected_hover_color=NAV_HOVER,
            text_color=TEXT, text_color_disabled=MUTED)
        seg.set(values[0])
        return seg

    def _option(self, parent, values, current, command, width=150):
        m = ctk.CTkOptionMenu(parent, values=values, command=command,
                              width=width, height=30, corner_radius=8,
                              fg_color=PANEL, button_color=CARD_HI,
                              button_hover_color=LINE, text_color=TEXT,
                              dropdown_fg_color=CARD_HI,
                              dropdown_hover_color=NAV_ACTIVE,
                              dropdown_text_color=TEXT,
                              font=self.F(12), dropdown_font=self.F(12),
                              dynamic_resizing=False)
        m.set(current)
        return m

    def _option_row(self, card, title, note, values, current, command,
                    first=False):
        """A row like a switch row, with a drop-down menu on the right."""
        if not first:
            self._hairline(card)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        menu = self._option(row, values, current, command)
        menu.pack(side="right", anchor="n")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ctk.CTkLabel(txt, text=title, text_color=TEXT, font=self.F(13),
                     anchor="w", height=18).pack(fill="x")
        menu.note = ctk.CTkLabel(txt, text=note, text_color=SUBTLE,
                                 font=self.F(11), anchor="w", justify="left",
                                 height=16, wraplength=380)
        menu.note.pack(fill="x")
        self._rewrap_on(txt, (menu.note,))
        return menu

    def _rewrap(self, width_px, labels, indent=0):
        w = width_px / max(0.5, ctk.ScalingTracker.get_widget_scaling(self))
        for lbl in labels:
            lbl.configure(wraplength=max(120, int(w - indent - 6)))

    def _rewrap_on(self, frame, labels, indent=0):
        """Wrap text to the width it really has. A fixed wraplength wider
        than the label clips the end off instead of wrapping. Bound on the
        frame, never on a label, whose width follows its own wrapping."""
        frame.bind("<Configure>",
                   lambda e: self._rewrap(e.width, labels, indent), add="+")

    def _draw_badge(self, cv, text, fill, colour, bg):
        """A real circle with a number in it (a CTkLabel with corner_radius
        turns into a pill once its text has padding)."""
        cv.delete("all")
        s = int(cv.cget("width"))
        cv.configure(bg=bg)
        cv.create_oval(1, 1, s - 1, s - 1, fill=fill, outline="")
        cv.create_text(s / 2, s / 2, text=text, fill=colour,
                       font=self._tkfont(11, semi=True))

    @staticmethod
    def _entry_set(e, text):
        """Replace a CTkEntry's text. Deleting from an entry that shows its
        placeholder blanks the placeholder (a CustomTkinter quirk), so only
        delete real text."""
        if e.get():
            e.delete(0, "end")
        if text:
            e.insert(0, text)

    # ================================================================ Home ==
    def _build_home(self, page):
        """Fixed size, no scrolling: the status card, four counters, what
        the game shows (world, next vote, storage), then recent activity."""
        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)
        self._home = body
        self._build_hero(body)
        self._build_tiles(body)
        self._build_ingame(body)
        self._build_recent(body)
        self.hero.grid(row=0, column=0, sticky="ew")
        self.tiles.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.ingame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.recent.grid(row=3, column=0, sticky="nsew", pady=(12, 0))

    def _build_hero(self, parent):
        """The status card, 148 high: what it's doing on the left, the
        run time well on the right."""
        self.hero = self._card(parent, height=148)
        self.hero.grid_propagate(False)
        self.hero.pack_propagate(False)
        self._sheen(self.hero)
        self.hero.grid_columnconfigure(0, weight=3, uniform="h")
        self.hero.grid_columnconfigure(1, weight=2, uniform="h")
        self.hero.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self.hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=(18, 14))
        top = ctk.CTkFrame(left, fg_color="transparent")
        top.pack(fill="x")
        d = self._px(16)
        self.dotc = tk.Canvas(top, width=d, height=d, bg=CARD,
                              highlightthickness=0, bd=0)
        self.dotc.pack(side="left", padx=(0, 10))
        c = d / 2
        self._halo = self.dotc.create_oval(c, c, c, c, fill=CARD, outline="")
        r = self._px(4)
        self._core = self.dotc.create_oval(c - r, c - r, c + r, c + r,
                                           fill=MUTED, outline="")
        self.lbl_state = ctk.CTkLabel(top, text="Ready", text_color=TEXT,
                                      font=self.F(20, semi=True), anchor="w",
                                      height=28)
        self.lbl_state.pack(side="left")
        self.lbl_since = ctk.CTkLabel(left, text="", text_color=MUTED,
                                      font=self.F(11), height=16, anchor="w")
        self.lbl_since.pack(side="bottom", fill="x", padx=(26, 0))
        self.lbl_detail = ctk.CTkLabel(left, text="", text_color=SUBTLE,
                                       font=self.F(12), anchor="nw",
                                       justify="left", wraplength=380,
                                       height=18)
        self.lbl_detail.pack(fill="x", padx=(26, 0), pady=(4, 0))
        self._rewrap_on(left, (self.lbl_detail,), indent=26)

        well = ctk.CTkFrame(self.hero, fg_color=INSET, corner_radius=12,
                            border_width=1, border_color=HAIRLINE)
        well.grid(row=0, column=1, sticky="nsew", padx=(8, 14), pady=14)
        play = ctk.CTkFrame(well, fg_color="transparent")
        play.pack(fill="both", expand=True, padx=16, pady=(12, 10))
        self.lbl_play_cap = ctk.CTkLabel(play, text="RUN TIME",
                                         text_color=MUTED, height=14,
                                         font=self.F(11, semi=True),
                                         anchor="w")
        self.lbl_play_cap.pack(fill="x")
        num = ctk.CTkFrame(play, fg_color="transparent")
        num.pack(fill="x", pady=(2, 0))
        self._num = []
        for i, (size, colour) in enumerate(((36, TEXT), (18, SUBTLE),
                                            (36, TEXT), (18, SUBTLE))):
            lbl = ctk.CTkLabel(num, text="", text_color=colour, height=44,
                               font=self.F(size, bold=size > 20),
                               anchor="sw")
            lbl.pack(side="left", anchor="s",
                     padx=(6 if i == 2 else 0, 0),
                     pady=(0, 6 if size < 20 else 0))
            self._num.append(lbl)
        self.bar_hour = ctk.CTkProgressBar(play, width=10, height=2,
                                           corner_radius=1,
                                           progress_color=ACCENT,
                                           fg_color=HAIRLINE)
        self.bar_hour.pack(fill="x", pady=(6, 0))
        self.bar_hour.set(0)
        self.lbl_play_sub = ctk.CTkLabel(play, text="", text_color=MUTED,
                                         font=self.F(11), anchor="w",
                                         height=16)
        self.lbl_play_sub.pack(fill="x", pady=(4, 0))

    def _build_tiles(self, parent):
        """This run's RARES / VOTES / AFK SAVES / REJOINS."""
        row = ctk.CTkFrame(parent, fg_color="transparent", height=76)
        row.grid_propagate(False)
        row.grid_rowconfigure(0, weight=1)
        self.tiles = row
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="t")
        self.val = {}
        for i, (key, cap) in enumerate((("rares", "RARES"), ("votes", "VOTES"),
                                        ("afk", "AFK SAVES"),
                                        ("rejoins", "REJOINS"))):
            self.val[key] = self._tile(row, i, cap)
            card = self.val[key].master
            # each tile opens the page that goes with it
            target = {"rares": "alerts", "votes": "vote", "afk": "server",
                      "rejoins": "server"}[key]
            self._hoverable(card, "tile:%d" % id(card), CARD, CARD_HI,
                            lambda p=target: self._show_page(p))
        for v in self.val.values():
            v.configure(text_color=SUBTLE)

    def _hoverable(self, frame, key, rest, hot, command):
        """A frame that fades to `hot` under the mouse and runs command on a
        click, wherever on it (or its labels) the click lands."""
        def inside():
            x, y = self.winfo_pointerxy()
            fx, fy = frame.winfo_rootx(), frame.winfo_rooty()
            return (fx <= x < fx + frame.winfo_width()
                    and fy <= y < fy + frame.winfo_height())

        def enter(_e=None):
            self._tween(key, lambda c: frame.configure(fg_color=c), rest, hot)

        def leave(_e=None):
            if not inside():
                self._tween(key, lambda c: frame.configure(fg_color=c), hot,
                            rest)

        def click(_e=None):
            if inside():
                command()
        for w in [frame] + list(frame.winfo_children()):
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", leave, add="+")
            w.bind("<ButtonRelease-1>", click, add="+")
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def _tile(self, parent, col, caption, value="0", size=22, span_=1,
              row=0, height=1, sub=None, bold=False):
        """A number tile in a 4-column grid. With sub, the value label gets
        a .sub label under it."""
        card = self._card(parent, corner_radius=14, height=height)
        card.grid(row=row, column=col, columnspan=span_, sticky="nsew",
                  padx=(0 if col == 0 else 6, 0 if col + span_ >= 4 else 6),
                  pady=(0 if row == 0 else 12, 0))
        card.pack_propagate(False)
        ctk.CTkLabel(card, text=caption, text_color=MUTED, height=14,
                     font=self.F(11, semi=True), anchor="w"
                     ).pack(fill="x", padx=16, pady=(13, 0))
        v = ctk.CTkLabel(card, text=value, text_color=TEXT,
                         height=max(30, size + 10), anchor="w",
                         font=self.F(size, semi=not bold, bold=bold))
        v.pack(fill="x", padx=16, pady=(4 if size < 30 else 0, 0))
        if sub is not None:
            v.sub = ctk.CTkLabel(card, text=sub, text_color=MUTED, height=14,
                                 font=self.F(11), anchor="w")
            v.sub.pack(fill="x", padx=16)
        return v

    def _build_ingame(self, parent):
        """One slim card: WORLD | NEXT VOTE | STORAGE, as last read off the
        game. The vote timer counts down between reads."""
        card = self._card(parent, corner_radius=14, height=54)
        card.grid_propagate(False)
        card.grid_rowconfigure(0, weight=1)
        self.ingame = card
        self.hud_val = {}
        cols = (("world", "WORLD", "", "◎"),
                ("vote", "NEXT VOTE", "", "◷"),
                ("storage", "STORAGE", "", "■"))
        for i, (key, cap, glyph, alt) in enumerate(cols):
            card.grid_columnconfigure(2 * i, weight=1, uniform="g")
            if i:
                tk.Frame(card, width=1, bg=HAIRLINE, bd=0,
                         highlightthickness=0).grid(row=0, column=2 * i - 1,
                                                    sticky="ns", pady=12)
            cell = ctk.CTkFrame(card, fg_color="transparent")
            cell.grid(row=0, column=2 * i, sticky="ew", padx=16)
            ctk.CTkLabel(cell, text=self._icon(glyph, alt), text_color=MUTED,
                         width=18, height=18,
                         font=self.F(13, family=self._fam_icon or self._fam_sym)
                         ).pack(side="left")
            ctk.CTkLabel(cell, text=cap, text_color=MUTED, height=14,
                         font=self.F(11, semi=True)).pack(side="left", padx=(8, 0))
            v = ctk.CTkLabel(cell, text="—", text_color=SUBTLE, height=20,
                             font=self.F(14, semi=True), anchor="e")
            v.pack(side="right")
            self.hud_val[key] = v
        self._hud_painted = None

    def _build_recent(self, parent):
        """Recent activity, from the feed: the newest few rows."""
        recent = self._card(parent, corner_radius=14, height=1)
        recent.pack_propagate(False)
        self.recent = recent
        head = ctk.CTkFrame(recent, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(13, 6))
        ctk.CTkLabel(head, text="RECENT ACTIVITY", text_color=MUTED,
                     height=14, font=self.F(11, semi=True)).pack(side="left")
        more = ctk.CTkLabel(head, text="See all ›", text_color=ACCENT,
                            height=14, font=self.F(11, semi=True),
                            cursor="hand2")
        more.pack(side="right")
        more.bind("<Button-1>", lambda _e: self._show_page("activity"))
        self._recent_rows = []
        for _ in range(6):
            r = ctk.CTkFrame(recent, fg_color="transparent", height=26)
            r.pack(fill="x", padx=16)
            r.pack_propagate(False)
            ts = ctk.CTkLabel(r, text="", text_color=MUTED, width=40,
                              anchor="w", font=self.F(11, family="Consolas"))
            ts.pack(side="left")
            gl = ctk.CTkLabel(r, text="", width=20, anchor="w",
                              font=self.F(11, family=self._fam_sym))
            gl.pack(side="left", padx=(4, 4))
            ti = ctk.CTkLabel(r, text="", text_color=TEXT, anchor="w",
                              font=self.F(12))
            ti.pack(side="left")
            su = ctk.CTkLabel(r, text="", text_color=SUBTLE, anchor="w",
                              font=self.F(11))
            su.pack(side="left", padx=(10, 0))
            ch = ctk.CTkLabel(r, text="", text_color=ACCENT_SOFT, anchor="w",
                              font=self.F(11, semi=True))
            ch.pack(side="left", padx=(8, 0))
            self._recent_rows.append((r, ts, gl, ti, su, ch))
        self._recent_fit = len(self._recent_rows)
        recent.bind("<Configure>", self._fit_recent, add="+")
        self._paint_recent()

    def _fit_recent(self, _e=None):
        """Only rows that fit whole: a short window showed half a row."""
        try:
            s = ctk.ScalingTracker.get_widget_scaling(self)
            n = max(1, int((self.recent.winfo_height() / s - 37) // 26))
        except Exception:
            return
        if n == self._recent_fit:
            return
        self._recent_fit = n
        for i, row in enumerate(self._recent_rows):
            shown = bool(row[0].winfo_manager())
            if i < n and not shown:
                row[0].pack(fill="x", padx=16)     # the hidden ones are all at the end
            elif i >= n and shown:
                row[0].pack_forget()

    def _paint_recent(self):
        """Home's recent activity: the newest few feed rows, unfiltered."""
        try:
            items = self._feed_items[:len(self._recent_rows)]
            r, ts, gl, ti, su, ch = self._recent_rows[0]
            if not items and ts.winfo_manager():       # the empty line
                ts.pack_forget()
                gl.pack_forget()
            elif items and not ts.winfo_manager():
                ts.pack(side="left", before=ti)
                gl.pack(side="left", padx=(4, 4), before=ti)
            for i, (r, ts, gl, ti, su, ch) in enumerate(self._recent_rows):
                it = items[i] if i < len(items) else None
                if it:
                    ts.configure(text=it["ts"])
                    gl.configure(text=it["glyph"],
                                 text_color=_colour(it["colour"]))
                    ti.configure(text=it["title"], text_color=TEXT)
                    su.configure(text=it["sub"])
                    ch.configure(text=f"×{it['count']}"
                                 if it["count"] > 1 else "")
                else:
                    ts.configure(text="")
                    gl.configure(text="")
                    su.configure(text="")
                    ch.configure(text="")
                    ti.configure(text="Nothing yet · open Block Mayhem X and "
                                      "press Start." if i == 0 and not items
                                 else "", text_color=MUTED)
        except Exception:
            pass

    # ============================================================ Activity ==
    # Activity filters, by FeedRow key. Never saved: the newest row of
    # everything is on top when the app opens.
    FEED_FILTERS = {
        "All": None,
        "Rares": ("rare",),
        "Votes": ("vote", "votefail", "world"),
        "Problems": ("err", "warn", "pause", "rejoin", "rejoinfail", "stuck",
                     "storage", "votefail", "screen"),
    }

    def _build_activity(self, page):
        self.act_page = ctk.CTkFrame(page, fg_color="transparent")
        self.act_page.pack(fill="both", expand=True, padx=20, pady=16)
        # packed first, from the bottom, so a short window squeezes the feed
        # rather than pushing this line off the end
        self.lbl_foot = ctk.CTkLabel(self.act_page,
                                     text="Newest first · every detail "
                                          "is on the Log page",
                                     text_color=MUTED, font=self.F(11),
                                     height=16)
        self.lbl_foot.pack(side="bottom", pady=(8, 0))
        bar = ctk.CTkFrame(self.act_page, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        self.seg_feed = self._segmented(bar, list(self.FEED_FILTERS),
                                        self._set_feed_filter)
        self.seg_feed.pack(side="left")
        self.lbl_feed_count = ctk.CTkLabel(bar, text="", text_color=MUTED,
                                           font=self.F(11), height=16)
        self.lbl_feed_count.pack(side="right")
        box = ctk.CTkFrame(self.act_page, fg_color=PANEL, corner_radius=14,
                           border_width=1, border_color=LINE)
        box.pack(fill="both", expand=True)
        self._act_box = box

        self.feed = ctk.CTkTextbox(box, fg_color=PANEL, text_color=TEXT,
                                   border_width=0, wrap="none",
                                   font=self.F(13), activate_scrollbars=True,
                                   scrollbar_button_color=LINE,
                                   scrollbar_button_hover_color=MUTED)
        self.feed.pack(fill="both", expand=True, padx=8, pady=6)
        tb = self.feed._textbox
        tb.configure(spacing1=self._px(7), spacing3=self._px(7),
                     tabs=(self._px(46), self._px(68)), cursor="arrow")
        tb.tag_configure("ts", foreground=MUTED,
                         font=self._tkfont(11, family="Consolas"))
        for name in ("GREEN", "ACCENT", "AMBER", "RED", "SUBTLE", "RARE",
                     "CHROMA", "INVERT"):
            tb.tag_configure("g_" + name, foreground=_colour(name),
                             font=self._tkfont(11, family=self._fam_sym))
        tb.tag_configure("title", foreground=TEXT, font=self._tkfont(13))
        tb.tag_configure("sub", foreground=SUBTLE, font=self._tkfont(11))
        tb.tag_configure("chip", foreground=ACCENT_SOFT,
                         font=self._tkfont(11, semi=True))
        self.feed.configure(state="disabled")

        # empty state, laid over the feed until the first event
        self.empty = ctk.CTkFrame(box, fg_color=PANEL)
        self._empty_mark = tk.Canvas(self.empty, width=self._px(40),
                                     height=self._px(40), bg=PANEL,
                                     highlightthickness=0, bd=0)
        draw_cube(self._empty_mark, 0, 0, self._px(40), dim=True)
        self._empty_mark.pack(pady=(0, 10))
        self._empty_title = ctk.CTkLabel(self.empty, text="Nothing yet",
                                         text_color=TEXT, height=18,
                                         font=self.F(13, semi=True))
        self._empty_title.pack()
        self._empty_sub = ctk.CTkLabel(
            self.empty, text="Open Block Mayhem X and press Start. Events "
            "show up here.", text_color=SUBTLE, font=self.F(11), height=16)
        self._empty_sub.pack(pady=(2, 0))
        self.empty.place(relx=0.5, rely=0.46, anchor="center")
        box.bind("<Configure>", self._fit_empty, add="+")
        self._paint_feed_empty()

    def _fit_empty(self, e):
        """In a short box the cube would overlap the border - keep just the
        words."""
        small = e.height < self._px(150)
        shown = bool(self._empty_mark.winfo_manager())
        if small and shown:
            self._empty_mark.pack_forget()
        elif not small and not shown:
            self._empty_mark.pack(pady=(0, 10), before=self._empty_title)

    def _set_feed_filter(self, value):
        if value not in self.FEED_FILTERS or value == self._feed_filter:
            return
        self._feed_filter = value
        self._feed_rebuild()

    def _feed_line(self, it):
        """One feed row as (text, tag) chunks - the same as _feed_add's."""
        parts = [(it["ts"] + "\t", "ts"),
                 (it["glyph"] + "\t", "g_" + it["colour"]),
                 (it["title"], "title")]
        if it["sub"]:
            parts.append(("   " + it["sub"], "sub"))
        if it["count"] > 1:
            parts.append((f"   ×{it['count']}", "chip"))
        parts.append(("\n", "title"))
        return parts

    def _feed_rebuild(self):
        """Redraw the feed from _feed_items for the current filter. The
        'All' view comes out exactly as _feed_add would have built it."""
        keys = self.FEED_FILTERS.get(self._feed_filter)
        tb = self.feed
        t = tb._textbox
        tb.configure(state="normal")
        t.delete("1.0", "end")
        for it in self._feed_items:
            if keys is None or it["key"] in keys:
                for chunk, tag in self._feed_line(it):
                    t.insert("end", chunk, tag)
        tb.configure(state="disabled")
        t.see("1.0")
        if keys is None:
            self._feed_rows = len(self._feed_items)
        self._paint_feed_empty()

    def _paint_feed_empty(self):
        """The 'Nothing yet' overlay whenever the view has no rows."""
        try:
            rows = int(self.feed._textbox.index("end-1c").split(".")[0]) - 1
            keys = self.FEED_FILTERS.get(self._feed_filter)
            n = len(self._feed_items) if keys is None else rows
            if keys is None:
                self.lbl_feed_count.configure(
                    text=f"{n} event{'s' if n != 1 else ''}" if n else "")
            else:
                self.lbl_feed_count.configure(
                    text=f"{rows} of {len(self._feed_items)}")
            if rows > 0:
                self.empty.place_forget()
                return
            if keys is None:
                self._empty_title.configure(text="Nothing yet")
                self._empty_sub.configure(text="Open Block Mayhem X and press "
                                               "Start. Events show up here.")
            else:
                self._empty_title.configure(text="Nothing here")
                self._empty_sub.configure(
                    text=f"No {self._feed_filter.lower()} so far.")
            self.empty.place(relx=0.5, rely=0.46, anchor="center")
        except Exception:
            pass

    def _feed_add(self, key, glyph, colour, title, sub=""):
        """Newest on top. The same event twice in a row becomes one row with a
        count ('Jumped to stop the AFK kick  ×6'), so hours read as a few
        lines."""
        items = self._feed_items
        it = {"key": key, "glyph": glyph, "colour": colour, "title": title,
              "sub": sub, "count": 1, "ts": time.strftime("%H:%M")}
        if items and items[0]["key"] == key and items[0]["title"] == title \
                and key not in ("start", "stop"):
            it["count"] = items[0]["count"] + 1
            items[0] = it
        else:
            items.insert(0, it)
            del items[300:]
        if self._page != "activity":       # the sidebar's unread count
            self._unread += 1
            self._paint_badges()
        if self._feed_filter != "All" or it["count"] > 1:
            # a merged row or a filtered view: redraw from the model, the
            # simple way (300 rows at most)
            self._feed_rebuild()
        else:
            t = self.feed._textbox
            self.feed.configure(state="normal")
            for chunk, tag in reversed(self._feed_line(it)):
                t.insert("1.0", chunk, tag)
            self._feed_rows += 1
            if self._feed_rows > 300:
                t.delete("301.0", "end")
                self._feed_rows = 300
            self.feed.configure(state="disabled")
            t.see("1.0")
            self._paint_feed_empty()
        self._paint_recent()

    # ============================================================== Alerts ==
    def _build_alerts(self, page):
        sp = self._scroll_page(page)
        s = self.settings

        self._group(sp, "RARE BLOCKS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_rare = self._switch_row(
            card, "Rare block alerts",
            "A note here, and in your Discord if it's set up.", first=True,
            on=s.get("rare_alerts", True) is True,
            command=lambda: self._switch_saved("rare_alerts", self.sw_rare))
        self.opt_rare = self._option_row(
            card, "Alert me for blocks rarer than",
            "Blocks at exactly this rarity count too.",
            rarity_choices(s.get("rare_min")),
            rarity_text(s.get("rare_min")) or rarity_text(100_000),
            lambda v: self._rarity_picked("rare_min", v))
        self.opt_ping = self._option_row(
            card, "@ me for blocks rarer than",
            "Needs your Discord user ID on the Discord page.",
            rarity_choices(s.get("rare_ping_min")),
            rarity_text(s.get("rare_ping_min")) or rarity_text(1_000_000),
            lambda v: self._rarity_picked("rare_ping_min", v))
        self.sw_variants = self._switch_row(
            card, "Always alert for Inverted and Chromatic",
            "Even when they're more common than the choice above.",
            on=s.get("alert_variants_always", True) is True,
            command=lambda: self._switch_saved("alert_variants_always",
                                               self.sw_variants))

        self._group(sp, "MORE ALERTS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_storage = self._switch_row(
            card, "Storage full alert",
            "When your storage stays full for a minute.", first=True,
            on=s.get("storage_alert", True) is True,
            command=lambda: self._switch_saved("storage_alert", self.sw_storage))
        self.sw_picture = self._switch_row(
            card, "Attach a picture of Roblox",
            "Only a picture of the Roblox window, and only to your own Discord.",
            on=s.get("attach_picture") is True,
            command=lambda: self._switch_saved("attach_picture", self.sw_picture))
        self.sw_medal = self._switch_row(
            card, "Clip rare blocks with Medal",
            "Presses your Medal clip key a moment after a rare block at your rarity "
            "spawns. Only while Roblox is in front and you're away from the keys.",
            on=s.get("medal_clip") is True,
            command=lambda: self._switch_saved("medal_clip", self.sw_medal))
        self._hairline(card)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(row, text="Medal clip key", font=self.F(13), text_color=TEXT,
                     anchor="w").pack(side="left")
        self.lbl_medal = ctk.CTkLabel(row, text="Like F4 or Ctrl+F4", font=self.F(11),
                                      text_color=MUTED, anchor="e")
        self.ent_medal = ctk.CTkEntry(row, width=110, font=self.F(13), justify="center")
        self.ent_medal.insert(0, str(s.get("medal_key") or "F4"))
        self.ent_medal.pack(side="right")
        self.lbl_medal.pack(side="right", padx=10)
        self.ent_medal.bind("<FocusOut>", lambda e: self._medal_key_saved())
        self.ent_medal.bind("<Return>", lambda e: self._medal_key_saved())

        self._group(sp, "RARE SPAWNS THIS SESSION")
        self.rare_card = self._card(sp, corner_radius=14)
        self.rare_card.pack(fill="x")
        self._rare_rows = []
        self._paint_rares()
        ctk.CTkLabel(sp, text="Rarer blocks are seen on screen (the spawn banner) "
                              "and in Roblox's own log (music and Chromatic blocks).",
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(14, 8))

    def _switch_saved(self, key, switch):
        self.settings[key] = bool(switch.get())
        self._changed()
        self._paint_badges()

    def _rarity_picked(self, key, value):
        n = rarity_from_text(value)
        if n:
            self.settings[key] = n
            self._changed()

    def _paint_rares(self):
        """This session's rare spawns, newest first (at most 8)."""
        card = self.rare_card
        for w in self._rare_rows:
            try:
                w.destroy()
            except Exception:
                pass
        self._rare_rows = []
        if not self._rares:
            lbl = ctk.CTkLabel(card, text="None yet this session.",
                               text_color=MUTED, font=self.F(12), anchor="w",
                               height=18)
            lbl.pack(fill="x", padx=16, pady=14)
            self._rare_rows.append(lbl)
            return
        src = {"banner": "on screen", "log_song": "its music played",
               "log_chromatic": "noted late"}
        for i, it in enumerate(self._rares[:8]):
            if i:
                line = ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                                    corner_radius=0)
                line.pack(fill="x", padx=1)
                self._rare_rows.append(line)
            row = ctk.CTkFrame(card, fg_color="transparent", height=34)
            row.pack(fill="x", padx=16, pady=2)
            row.pack_propagate(False)
            ctk.CTkLabel(row, text=it["ts"], text_color=MUTED, width=44,
                         anchor="w", font=self.F(11, family="Consolas")
                         ).pack(side="left")
            ctk.CTkLabel(row, text="◆", text_color=_colour(it["colour"]),
                         width=16, font=self.F(12, family=self._fam_sym)
                         ).pack(side="left", padx=(2, 8))
            ctk.CTkLabel(row, text=it["name"], text_color=TEXT, anchor="w",
                         font=self.F(13, semi=True)).pack(side="left")
            ctk.CTkLabel(row, text=src.get(it["source"], ""), text_color=MUTED,
                         anchor="e", font=self.F(11)).pack(side="right")
            ctk.CTkLabel(row, text=it["odds"] or "rarity unknown",
                         text_color=ACCENT_SOFT if it["odds"] else MUTED,
                         anchor="e", font=self.F(12, semi=True)
                         ).pack(side="right", padx=(0, 14))
            self._rare_rows.append(row)

    # ========================================================== World vote ==
    def _build_vote(self, page):
        sp = self._scroll_page(page)
        s = self.settings

        self._group(sp, "AUTO VOTE")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_vote = self._switch_row(
            card, "Vote automatically",
            "Clicks the VOTE button under your map when the world vote shows up.",
            first=True, on=s.get("auto_vote", True) is True,
            command=lambda: self._switch_saved("auto_vote", self.sw_vote))

        self._group(sp, "MAPS YOU WANT")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=12)
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1, uniform="m")
        self.chk_map = {}
        ranked = set(s.get("vote_maps") or [])
        for i, name in enumerate(MAPS):
            chk = self._check(grid, name, name in ranked,
                              lambda n=name: self._map_ticked(n))
            chk.grid(row=i // 3, column=i % 3, sticky="w", pady=5)
            self.chk_map[name] = chk

        self._group(sp, "YOUR RANKING")
        self.rank_card = self._card(sp, corner_radius=14)
        self.rank_card.pack(fill="x")
        self._rank_rows = []
        wrap = ctk.CTkFrame(sp, fg_color="transparent")
        wrap.pack(fill="x", padx=4, pady=(12, 8))
        rule = ctk.CTkLabel(
            wrap, text="When the vote shows up it picks your #1 map if it's there, "
                       "then #2, and so on. If none of your maps are in the vote "
                       "(or you haven't ticked any), it picks the first map.",
            text_color=SUBTLE, font=self.F(12), anchor="w", justify="left",
            height=18, wraplength=560)
        rule.pack(fill="x")
        self._rewrap_on(wrap, (rule,))
        self._paint_ranking()

    def _map_ticked(self, name):
        on = bool(self.chk_map[name].get())
        self.settings["vote_maps"] = rank_toggle(self.settings.get("vote_maps"),
                                                 name, on)
        self._changed()
        self._paint_ranking()
        self._paint_badges()

    def _map_move(self, name, step):
        self.settings["vote_maps"] = rank_move(self.settings.get("vote_maps"),
                                               name, step)
        self._changed()
        self._paint_ranking()

    def _paint_ranking(self):
        """The ticked maps in rank order: a numbered badge, the name, and
        up / down. Rebuilt on every change (nine rows at most)."""
        card = self.rank_card
        for w in self._rank_rows:
            try:
                w.destroy()
            except Exception:
                pass
        self._rank_rows = []
        ranked = clean_ranking(self.settings.get("vote_maps"))
        if not ranked:
            lbl = ctk.CTkLabel(card, text="Tick maps above to rank them. With "
                                          "none ticked it picks the first map.",
                               text_color=MUTED, font=self.F(12), anchor="w",
                               height=18)
            lbl.pack(fill="x", padx=16, pady=14)
            self._rank_rows.append(lbl)
            return
        arrow = self.F(12, family=self._fam_icon or self._fam_sym)
        for i, name in enumerate(ranked):
            if i:
                line = ctk.CTkFrame(card, height=1, fg_color=HAIRLINE,
                                    corner_radius=0)
                line.pack(fill="x", padx=1)
                self._rank_rows.append(line)
            row = ctk.CTkFrame(card, fg_color="transparent", height=40)
            row.pack(fill="x", padx=14, pady=1)
            row.pack_propagate(False)
            d = self._px(24)
            cv = tk.Canvas(row, width=d, height=d, bg=CARD,
                           highlightthickness=0, bd=0)
            cv.pack(side="left")
            first = i == 0
            self._draw_badge(cv, str(i + 1), ACCENT if first else ACCENT_DIM,
                             INK if first else ACCENT, CARD)
            ctk.CTkLabel(row, text=name, text_color=TEXT, anchor="w",
                         font=self.F(13, semi=first)).pack(side="left", padx=(12, 0))
            if first:
                ctk.CTkLabel(row, text="first choice", text_color=MUTED,
                             font=self.F(11)).pack(side="left", padx=(10, 0))
            down = ctk.CTkButton(row, text=self._icon("", "▼"), width=30,
                                 height=26, corner_radius=8,
                                 fg_color="transparent", hover_color=CARD_HI,
                                 border_width=1, border_color=LINE,
                                 text_color=SUBTLE, text_color_disabled=LINE_DIM,
                                 font=arrow,
                                 state="disabled" if i == len(ranked) - 1 else "normal",
                                 command=lambda n=name: self._map_move(n, 1))
            down.pack(side="right")
            up = ctk.CTkButton(row, text=self._icon("", "▲"), width=30,
                               height=26, corner_radius=8,
                               fg_color="transparent", hover_color=CARD_HI,
                               border_width=1, border_color=LINE,
                               text_color=SUBTLE, text_color_disabled=LINE_DIM,
                               font=arrow, state="disabled" if first else "normal",
                               command=lambda n=name: self._map_move(n, -1))
            up.pack(side="right", padx=(0, 6))
            self._rank_rows.append(row)

    # ============================================================== Server ==
    def _build_server(self, page):
        sp = self._scroll_page(page)
        s = self.settings

        self._group(sp, "PRIVATE SERVER")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=(12, 12))
        ctk.CTkLabel(box, text="Private server link", text_color=TEXT,
                     font=self.F(13), anchor="w", height=18).pack(fill="x")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.e_link = ctk.CTkEntry(
            row, height=32, corner_radius=8, border_width=1,
            border_color=KEYCAP_LINE, fg_color=PANEL, text_color=TEXT,
            show="•", placeholder_text="https://www.roblox.com/share?code=…",
            placeholder_text_color=MUTED, font=self.F(11, family="Consolas"))
        self.e_link.pack(side="left", fill="x", expand=True)
        self.e_link.bind("<KeyRelease>", lambda _e: self._link_validate(), add="+")
        self.btn_link_b = self._ghost(row, "Paste", self._link_paste)
        self.btn_link_b.configure(width=70, height=32)
        self.btn_link_b.pack(side="right", padx=(8, 0))
        self.btn_link_a = self._ghost(row, "Save", self._link_save)
        self.btn_link_a.configure(width=70, height=32)
        self.btn_link_a.pack(side="right", padx=(8, 0))
        self.btn_link_eye = ctk.CTkButton(
            row, text=self._icon("", "👁"), width=32, height=32,
            corner_radius=8, fg_color="transparent", hover_color=CARD_HI,
            border_width=1, border_color=LINE, text_color=SUBTLE,
            font=self.F(13, family=self._fam_icon or self._fam_sym),
            command=self._link_eye)
        self.btn_link_eye.pack(side="right", padx=(8, 0))
        self.lbl_link = ctk.CTkLabel(box, text="", text_color=MUTED,
                                     font=self.F(11), anchor="w",
                                     justify="left", height=16, wraplength=520)
        self.lbl_link.pack(fill="x", pady=(6, 0))
        self._rewrap_on(box, (self.lbl_link,))
        ctk.CTkLabel(box, text="Anyone with this link can join your server. "
                               "Keep it to yourself.",
                     text_color=AMBER, font=self.F(11), anchor="w",
                     height=16).pack(fill="x", pady=(8, 0))

        self._group(sp, "STAY IN THE GAME")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_rejoin = self._switch_row(
            card, "Rejoin automatically",
            "After a kick, a disconnect or Roblox closing. It waits until "
            "you're away from your PC first.", first=True,
            on=s.get("auto_rejoin", True) is True,
            command=lambda: self._switch_saved("auto_rejoin", self.sw_rejoin))
        self.sw_afk = self._switch_row(
            card, "Anti-AFK",
            "Jumps once before Roblox's 20-minute idle kick.",
            on=s.get("anti_afk", True) is True,
            command=lambda: self._switch_saved("anti_afk", self.sw_afk))
        self._hairline(card)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        step = ctk.CTkFrame(row, fg_color="transparent")
        step.pack(side="right")
        self.btn_afk_minus = self._ghost(step, "−", lambda: self._afk_step(-1))
        self.btn_afk_minus.configure(width=30, height=28, font=self.F(14))
        self.btn_afk_minus.pack(side="left")
        self.lbl_afk_min = ctk.CTkLabel(step, text="", text_color=TEXT,
                                        width=64, font=self.F(13, semi=True))
        self.lbl_afk_min.pack(side="left")
        self.btn_afk_plus = self._ghost(step, "+", lambda: self._afk_step(1))
        self.btn_afk_plus.configure(width=30, height=28, font=self.F(14))
        self.btn_afk_plus.pack(side="left")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text="Jump after", text_color=TEXT, font=self.F(13),
                     anchor="w", height=18).pack(fill="x")
        ctk.CTkLabel(txt, text=f"Minutes of you not touching the game "
                               f"({AFK_MIN} to {AFK_MAX}).",
                     text_color=SUBTLE, font=self.F(11), anchor="w",
                     height=16).pack(fill="x")
        self.sw_front = self._switch_row(
            card, "Bring Roblox to the front to stop the AFK kick",
            "Only when you're away from your PC: it jumps, then puts your "
            "window back.",
            on=s.get("afk_bring_to_front") is True,
            command=lambda: self._switch_saved("afk_bring_to_front", self.sw_front))
        ctk.CTkLabel(sp, text="Rejoin only ever closes and opens Roblox, and "
                              "only during a run you started.",
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(14, 8))
        self._link_reveal = False
        self._link_editing = False
        self._paint_link()
        self._paint_afk()

    def _paint_afk(self):
        n = clamp_minutes(self.settings.get("anti_afk_after_min"))
        self.lbl_afk_min.configure(text=f"{n} min")
        self.btn_afk_minus.configure(state="normal" if n > AFK_MIN else "disabled")
        self.btn_afk_plus.configure(state="normal" if n < AFK_MAX else "disabled")

    def _afk_step(self, step):
        n = clamp_minutes(clamp_minutes(self.settings.get("anti_afk_after_min")) + step)
        if n != self.settings.get("anti_afk_after_min"):
            self.settings["anti_afk_after_min"] = n
            self._changed()
        self._paint_afk()

    def _paint_link(self):
        """The link row: masked once saved, an editable box otherwise."""
        saved = str(self.settings.get("private_server_link") or "").strip()
        e = self.e_link
        e.configure(state="normal")
        self._entry_set(e, "")
        if saved and not self._link_editing:
            e.configure(show="")
            e.insert(0, rejoin.mask_link(saved))
            e.configure(state="disabled", text_color=SUBTLE)
            self.btn_link_a.configure(text="Change", command=self._link_change,
                                      state="normal", fg_color="transparent",
                                      hover_color=CARD_HI, text_color=TEXT,
                                      border_width=1)
            self.btn_link_b.configure(text="Clear", command=self._link_clear)
            self.btn_link_eye.pack_forget()
            ok, why = rejoin.validate_link(saved)
            self.lbl_link.configure(text=why, text_color=GREEN if ok else RED)
        else:
            e.configure(show="" if self._link_reveal else "•", text_color=TEXT)
            self.btn_link_a.configure(text="Save", command=self._link_save)
            self.btn_link_b.configure(text="Paste", command=self._link_paste)
            if not self.btn_link_eye.winfo_manager():
                self.btn_link_eye.pack(side="right", padx=(8, 0))
            self._link_validate()

    def _link_validate(self):
        """The line under the box, as the link is typed or pasted."""
        if self.e_link.cget("state") == "disabled":
            return
        text = self.e_link.get().strip()
        ok, why = rejoin.validate_link(text)
        if not text:
            colour, ok = MUTED, False
            why = why + " Paste your private server link to use it."
        else:
            colour = GREEN if ok else RED
        self.lbl_link.configure(text=why, text_color=colour)
        self.btn_link_a.configure(
            state="normal" if ok else "disabled",
            fg_color=ACCENT if ok else "transparent",
            hover_color=ACCENT_SOFT if ok else CARD_HI,
            text_color=INK if ok else TEXT,
            border_width=0 if ok else 1)

    def _link_eye(self):
        self._link_reveal = not self._link_reveal
        if self.e_link.cget("state") != "disabled":
            self.e_link.configure(show="" if self._link_reveal else "•")

    def _link_paste(self):
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        self._entry_set(self.e_link, str(text).strip()[:2000])
        self._link_validate()

    def _link_save(self):
        text = self.e_link.get().strip()
        ok, why = rejoin.validate_link(text)
        if not text or not ok:
            self._link_validate()
            return
        self.settings["private_server_link"] = text
        self._changed()
        self._link_editing = False
        self._link_reveal = False
        self._paint_link()
        self.log(f"server link saved: {rejoin.mask_link(text)}")
        self._toast("Server link saved · rejoin will use it", "ok")

    def _link_change(self):
        self._link_editing = True
        self._paint_link()
        self.e_link.focus_set()

    def _link_clear(self):
        self.settings["private_server_link"] = ""
        self._changed()
        self._link_editing = False
        self._paint_link()
        self.log("server link cleared - rejoin will use a public server")
        self._toast("Link cleared · rejoin will use a public server", "info")

    # ============================================================= Discord ==
    # (group, label, '@ me' allowed) - the groups are settings["discord"]["send"]
    HOOK_EVENTS = (
        ("rare", "Rare block spawns", True),
        ("run", "Run starts and stops", False),
        ("rejoin", "Rejoins", False),
        ("storage", "Storage full", False),
        ("error", "Stopped by an error", True),
        ("stuck", "Stuck: couldn't rejoin after 5 tries", True),
        ("hourly", "Every full hour of run time", False),
    )

    def _build_discord(self, page):
        sp = self._scroll_page(page)
        cfg = self._hook_settings()

        self._group(sp, "WEBHOOK")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_hook = self._switch_row(
            card, "Send alerts to Discord",
            "Off until a webhook link is saved below.", first=True,
            on=False, command=self._hook_switched)
        self._hairline(card)
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=(10, 12))
        ctk.CTkLabel(box, text="Webhook link", text_color=TEXT,
                     font=self.F(13), anchor="w", height=18).pack(fill="x")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.e_hook = ctk.CTkEntry(
            row, height=32, corner_radius=8, border_width=1,
            border_color=KEYCAP_LINE, fg_color=PANEL, text_color=TEXT,
            show="•", placeholder_text="https://discord.com/api/webhooks/…",
            placeholder_text_color=MUTED,
            font=self.F(11, family="Consolas"))
        self.e_hook.pack(side="left", fill="x", expand=True)
        self.e_hook.bind("<KeyRelease>", lambda _e: self._hook_validate(),
                         add="+")
        self.btn_hook_b = self._ghost(row, "Paste", self._hook_paste)
        self.btn_hook_b.configure(width=70, height=32)
        self.btn_hook_b.pack(side="right", padx=(8, 0))
        self.btn_hook_a = self._ghost(row, "Save", self._hook_save)
        self.btn_hook_a.configure(width=70, height=32)
        self.btn_hook_a.pack(side="right", padx=(8, 0))
        self.btn_eye = ctk.CTkButton(
            row, text=self._icon("", "👁"), width=32,
            height=32, corner_radius=8, fg_color="transparent",
            hover_color=CARD_HI, border_width=1, border_color=LINE,
            text_color=SUBTLE,
            font=self.F(13, family=self._fam_icon or self._fam_sym),
            command=self._hook_eye)
        self.btn_eye.pack(side="right", padx=(8, 0))
        self.lbl_hook = ctk.CTkLabel(box, text="", text_color=MUTED,
                                     font=self.F(11), anchor="w",
                                     justify="left", height=16,
                                     wraplength=520)
        self.lbl_hook.pack(fill="x", pady=(6, 0))
        self._rewrap_on(box, (self.lbl_hook,))
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", pady=(10, 0))
        self.btn_hook_test = self._ghost(r, "Send test", self._hook_test)
        self.btn_hook_test.pack(side="left")
        self.lbl_hook_test = ctk.CTkLabel(r, text="", text_color=MUTED,
                                          font=self.F(11), anchor="w",
                                          height=16)
        self.lbl_hook_test.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(box, text="Anyone with this link can post in that "
                               "channel. Keep it to yourself.",
                     text_color=AMBER, font=self.F(11), anchor="w",
                     height=16).pack(fill="x", pady=(10, 0))

        # the user ID lives with the link - no scrolling to the bottom to set
        # who gets @mentioned
        ctk.CTkFrame(box, height=1, fg_color=HAIRLINE,
                     corner_radius=0).pack(fill="x", pady=(12, 10))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text="Your Discord user ID", text_color=TEXT,
                     font=self.F(13), anchor="w", height=18).pack(fill="x")
        how = ctk.CTkLabel(txt, text="Only for the alerts ticked '@ me'. Discord: "
                                     "Settings › Advanced › Developer Mode, then "
                                     "right-click your name › Copy User ID.",
                           text_color=SUBTLE, font=self.F(11), anchor="w",
                           justify="left", height=16, wraplength=380)
        how.pack(fill="x")
        self._rewrap_on(txt, (how,))
        self.e_uid = ctk.CTkEntry(row, width=190, height=30, corner_radius=8,
                                  border_width=1, border_color=KEYCAP_LINE,
                                  fg_color=PANEL, text_color=TEXT,
                                  placeholder_text="e.g. 123456789012345678",
                                  placeholder_text_color=MUTED,
                                  font=self.F(11, family="Consolas"))
        self.e_uid.pack(side="right", padx=(10, 0), anchor="n")
        if cfg.get("user_id"):
            self.e_uid.insert(0, str(cfg.get("user_id")))
        self.e_uid.bind("<KeyRelease>", lambda _e: self._uid_changed(),
                        add="+")
        self.e_uid.bind("<FocusOut>", lambda _e: self._uid_changed(),
                        add="+")
        self.lbl_uid = ctk.CTkLabel(box, text="", text_color=MUTED,
                                    font=self.F(11), anchor="w", height=16)
        self.lbl_uid.pack(fill="x", pady=(6, 0))

        self._group(sp, "SEND AN ALERT WHEN")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        send = cfg.get("send") if isinstance(cfg.get("send"), dict) else {}
        me = cfg.get("mention") if isinstance(cfg.get("mention"), dict) else {}
        self.chk_hook, self.chk_mention = {}, {}
        for i, (key, text, mention) in enumerate(self.HOOK_EVENTS):
            if i:
                self._hairline(card)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=7)
            chk = self._check(row, text, send.get(key) is True,
                              self._hook_options_changed)
            chk.pack(side="left")
            self.chk_hook[key] = chk
            if mention:
                m = self._check(row, "@ me", me.get(key) is True,
                                self._hook_options_changed, small=True)
                m.pack(side="right")
                self.chk_mention[key] = m

        self._group(sp, "WHAT AN ALERT LOOKS LIKE")
        self._build_hook_preview(sp)
        ctk.CTkLabel(sp, text="Sent only during a run you started, at most one "
                              "message every few seconds. No PC name or file "
                              "paths are ever sent.",
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(14, 8))
        self._hook_reveal = False
        self._hook_test_at = 0.0
        self._hook_editing = False
        self._paint_hook()
        self._uid_changed(save=False)

    def _build_hook_preview(self, sp):
        """A Discord message, drawn: who it's from, the coloured edge, the
        title, the fields and the footer - a rare spawn, the alert that
        matters most here."""
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        mark = tk.Canvas(head, width=self._px(28), height=self._px(28),
                         bg=CARD, highlightthickness=0, bd=0)
        draw_cube(mark, self._px(2), self._px(2), self._px(24))
        mark.pack(side="left")
        ctk.CTkLabel(head, text=APP_NAME, text_color=TEXT, height=18,
                     font=self.F(13, semi=True)).pack(side="left",
                                                     padx=(10, 0))
        ctk.CTkLabel(head, text="APP", text_color=TEXT, height=16, width=30,
                     corner_radius=4, fg_color="#5865f2",
                     font=self.F(9, semi=True)).pack(side="left", padx=(6, 0))
        self.lbl_prev_ping = ctk.CTkLabel(head, text="", text_color=ACCENT,
                                          height=18, font=self.F(12))
        self.lbl_prev_ping.pack(side="left", padx=(8, 0))
        emb = ctk.CTkFrame(inner, fg_color=PANEL, corner_radius=6)
        emb.pack(fill="x", padx=(38, 0), pady=(6, 0))
        edge = tk.Frame(emb, bg=RARE, width=self._px(4), bd=0,
                        highlightthickness=0)
        edge.pack(side="left", fill="y")
        body = ctk.CTkFrame(emb, fg_color="transparent")
        body.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        ctk.CTkLabel(body, text="Resonance spawned!", text_color=TEXT, height=18,
                     anchor="w", font=self.F(13, semi=True)).pack(fill="x")
        ctk.CTkLabel(body, text="It's on your server right now.",
                     text_color=SUBTLE, height=16, anchor="w",
                     font=self.F(12)).pack(fill="x")
        f = ctk.CTkFrame(body, fg_color="transparent")
        f.pack(fill="x", pady=(6, 0))
        for name, value in (("Rarity", "1 in 100,000"), ("World", "Dreamland")):
            col = ctk.CTkFrame(f, fg_color="transparent")
            col.pack(side="left", padx=(0, 28))
            ctk.CTkLabel(col, text=name, text_color=TEXT, height=14,
                         anchor="w", font=self.F(11, semi=True)).pack(fill="x")
            ctk.CTkLabel(col, text=value, text_color=SUBTLE, height=16,
                         anchor="w", font=self.F(11)).pack(fill="x")
        ctk.CTkLabel(body, text=f"{APP_NAME} {APP_VER} · run "
                                f"#{self.session.stats.get('runs', 0) + 1}",
                     text_color=MUTED, height=14, anchor="w",
                     font=self.F(10)).pack(fill="x", pady=(8, 0))

    def _hook_settings(self) -> dict:
        cfg = self.settings.get("discord")
        if not isinstance(cfg, dict):
            cfg = D.webhook_defaults()
            self.settings["discord"] = cfg
        return cfg

    def _paint_hook(self):
        """The link row: masked once saved, an editable box otherwise."""
        cfg = self._hook_settings()
        saved = D.normalize_hook(cfg.get("webhook"))
        e = self.e_hook
        e.configure(state="normal")
        self._entry_set(e, "")
        if saved and not self._hook_editing:
            e.configure(show="")
            e.insert(0, D.mask_hook(saved))
            e.configure(state="disabled", text_color=SUBTLE)
            self.btn_hook_a.configure(text="Change", command=self._hook_change,
                                      state="normal", fg_color="transparent",
                                      hover_color=CARD_HI, text_color=TEXT,
                                      border_width=1)
            self.btn_hook_b.configure(text="Remove", command=self._hook_remove)
            self.btn_eye.pack_forget()
            self.lbl_hook.configure(text="Saved. Send test to check it "
                                         "reaches your channel.",
                                    text_color=GREEN)
            self.sw_hook.configure(state="normal")
            if cfg.get("enabled") is True:
                self.sw_hook.select()
            else:
                self.sw_hook.deselect()
        else:
            e.configure(show="" if self._hook_reveal else "•",
                        text_color=TEXT)
            self.btn_hook_a.configure(text="Save", command=self._hook_save)
            self.btn_hook_b.configure(text="Paste", command=self._hook_paste)
            if not self.btn_eye.winfo_manager():
                self.btn_eye.pack(side="right", padx=(8, 0))
            if not saved:
                self.sw_hook.deselect()
                self.sw_hook.configure(state="disabled")
            self._hook_validate()
        self.btn_hook_test.configure(state="normal" if saved else "disabled")
        self._paint_hook_note()
        self._paint_badges()

    def _hook_validate(self):
        """The line under the box, as the link is typed or pasted."""
        if self.e_hook.cget("state") == "disabled":
            return
        text = self.e_hook.get().strip()
        if not text:
            msg, colour, ok = ("In Discord: channel settings › Integrations › "
                               "Webhooks › New Webhook › Copy Webhook URL, "
                               "then Paste."), MUTED, False
        elif D.normalize_hook(text):
            msg, colour, ok = "Looks right · press Save.", GREEN, True
        else:
            msg, colour, ok = ("That isn't a Discord webhook link. It starts "
                               "https://discord.com/api/webhooks/"), RED, False
        self.lbl_hook.configure(text=msg, text_color=colour)
        # once the link is good, Save is the obvious next step
        self.btn_hook_a.configure(
            state="normal" if ok else "disabled",
            fg_color=ACCENT if ok else "transparent",
            hover_color=ACCENT_SOFT if ok else CARD_HI,
            text_color=INK if ok else TEXT,
            border_width=0 if ok else 1)

    def _hook_eye(self):
        self._hook_reveal = not self._hook_reveal
        if self.e_hook.cget("state") != "disabled":
            self.e_hook.configure(show="" if self._hook_reveal else "•")

    def _hook_paste(self):
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        self._entry_set(self.e_hook, str(text).strip()[:300])
        self._hook_validate()

    def _hook_save(self):
        url = D.normalize_hook(self.e_hook.get())
        if not url:
            self._hook_validate()
            return
        cfg = self._hook_settings()
        if url != D.normalize_hook(cfg.get("webhook")):
            self.session.kill_sender()       # nothing queued goes to the old link
        cfg["webhook"] = url
        self._changed()
        self._hook_editing = False
        self._hook_reveal = False
        self._paint_hook()
        self.log("webhook: link saved")

    def _hook_change(self):
        self._hook_editing = True
        self._paint_hook()
        self.e_hook.focus_set()

    def _hook_remove(self):
        cfg = self._hook_settings()
        cfg["webhook"], cfg["enabled"] = "", False
        self._changed()
        self.session.kill_sender()
        self._hook_editing = False
        self._paint_hook()
        self.log("webhook: link removed - alerts are off")

    def _hook_switched(self):
        cfg = self._hook_settings()
        on = bool(self.sw_hook.get()) and bool(D.normalize_hook(cfg.get("webhook")))
        cfg["enabled"] = on
        self._changed()
        if not on:
            self.sw_hook.deselect()
            self.session.kill_sender()
        self._paint_hook_note()
        self._paint_badges()

    def _paint_hook_note(self):
        """The line under 'Send alerts to Discord' says where things stand."""
        cfg = self._hook_settings()
        if not D.normalize_hook(cfg.get("webhook")):
            text = "Off until a webhook link is saved below."
        elif cfg.get("enabled") is True:
            text = "On · alerts go to your channel during a run."
        else:
            text = "Link saved · switch this on to send alerts."
        self.sw_hook.note.configure(text=text)

    def _hook_options_changed(self):
        cfg = self._hook_settings()
        send = cfg.setdefault("send", {})
        for key, chk in self.chk_hook.items():
            send[key] = bool(chk.get())
        me = cfg.setdefault("mention", {})
        for key, chk in self.chk_mention.items():
            me[key] = bool(chk.get())
        self._changed()
        self._paint_ping()

    def _uid_changed(self, save=True):
        uid = self.e_uid.get().strip()
        ok = bool(re.fullmatch(r"\d{17,20}", uid))
        if not uid:
            self.lbl_uid.configure(text="No ID: alerts never @mention anyone.",
                                   text_color=MUTED)
        elif ok:
            self.lbl_uid.configure(text="Ticked alerts will @mention you.",
                                   text_color=GREEN)
        else:
            self.lbl_uid.configure(text="A user ID is 17 to 20 digits.",
                                   text_color=RED)
        if save:
            cfg = self._hook_settings()
            new = uid if ok else ""
            if cfg.get("user_id", "") != new:
                cfg["user_id"] = new
                self._changed()
        self._paint_ping()

    def _paint_ping(self):
        cfg = self._hook_settings()
        me = cfg.get("mention") if isinstance(cfg.get("mention"), dict) else {}
        on = re.fullmatch(r"\d{17,20}", str(cfg.get("user_id") or "")) and \
            me.get("rare") is True
        self.lbl_prev_ping.configure(text="@you" if on else "")

    def _hook_test(self):
        """Send test: only ever on a click, at most once every 5 seconds,
        on its own thread. MAYHEM_NO_SEND (and testing) still block it."""
        cfg = self._hook_settings()
        url = D.normalize_hook(cfg.get("webhook"))
        if not url:
            return
        now = time.time()
        if now - self._hook_test_at < 5:
            return
        self._hook_test_at = now
        self.btn_hook_test.configure(state="disabled", text="Sending…")
        self.lbl_hook_test.configure(text="", text_color=MUTED)
        uid = str(cfg.get("user_id") or "")
        me = cfg.get("mention") if isinstance(cfg.get("mention"), dict) else {}
        mention = [uid] if re.fullmatch(r"\d{17,20}", uid) and any(
            v is True for v in me.values()) else []
        payload = D.build_payload([D.sample_message()], mention)

        def run():
            try:
                sender = D.WebhookSender(url, log=self._hook_log)
                sender._hurry = True          # one try: no minute of retries
                status = sender.post_now(payload)
            except Exception:
                status = "dropped"
            self._ui(lambda s=status: self._hook_tested(s))
        threading.Thread(target=run, daemon=True,
                         name=f"{APP_NAME} send test").start()

    def _medal_key_saved(self):
        from mayhem import inputs as I
        text = self.ent_medal.get().strip()
        if I.parse_keys(text) is None:
            self.lbl_medal.configure(text="Use F1-F24, a letter or a number (Ctrl/Alt/Shift+ ok)",
                                     text_color=RED)
            return
        key = "+".join(p.strip().title() if len(p.strip()) > 1 and not p.strip()[1:].isdigit()
                       else p.strip().upper() for p in text.split("+"))
        self.settings["medal_key"] = key
        self._changed()
        self.lbl_medal.configure(text="Saved", text_color=GREEN)

    def _hook_tested(self, status):
        text, colour = {
            "ok": ("Sent · check the channel.", GREEN),
            "blocked": ("Not sent: sending is switched off in this copy.",
                        MUTED),
            "dead": ("Discord says this link doesn't work. Copy it again.",
                     RED),
        }.get(status, ("Couldn't reach Discord. Try again in a bit.", AMBER))
        if status == "ok" and not (self.settings.get("discord") or {}).get("enabled"):
            text, colour = ("Sent · but alerts are still OFF. Turn on 'Send alerts to "
                            "Discord' above to get them during runs."), AMBER
        self.lbl_hook_test.configure(text=text, text_color=colour)
        self._toast(text, {GREEN: "ok", RED: "err", AMBER: "warn"}.get(
            colour, "info"))
        self.after(max(0, int(5000 - (time.time() - self._hook_test_at)
                              * 1000)),
                   lambda: self.btn_hook_test.configure(
                       state="normal", text="Send test"))

    def _hook_log(self, line):
        """The sender's 'webhook:' lines, from its own thread."""
        self._ui(lambda: self.log(line))

    # =============================================================== Stats ==
    def _build_stats(self, page):
        sp = self._scroll_page(page)
        self._stats_sp = sp
        # nothing counted yet
        self._stats_empty = self._card(sp)
        self._sheen(self._stats_empty)
        inner = ctk.CTkFrame(self._stats_empty, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=28)
        mark = tk.Canvas(inner, width=self._px(40), height=self._px(40),
                         bg=CARD, highlightthickness=0, bd=0)
        draw_cube(mark, 0, 0, self._px(40), dim=True)
        mark.pack()
        ctk.CTkLabel(inner, text="No runs yet", text_color=TEXT, height=22,
                     font=self.F(15, semi=True)).pack(pady=(10, 0))
        ctk.CTkLabel(inner, text="Run time, rare blocks, votes and rejoins add "
                                 "up here from your first run.",
                     text_color=SUBTLE, font=self.F(12), height=18
                     ).pack(pady=(2, 0))

        body = ctk.CTkFrame(sp, fg_color="transparent")
        self._stats_body = body
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x", pady=(16, 0))
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1, uniform="s")
        self._st = {}
        self._st["total"] = self._tile(grid, 0, "TOTAL RUN TIME", "0m",
                                       size=32, span_=2, height=96, sub="",
                                       bold=True)
        self._st["longest"] = self._tile(grid, 2, "LONGEST RUN", "0m",
                                         height=96, sub="")
        self._st["runs"] = self._tile(grid, 3, "RUNS", "0", height=96,
                                      sub="")
        for col, (key, cap) in enumerate((("rares", "RARE BLOCKS"),
                                          ("votes", "VOTES"),
                                          ("anti_afk", "AFK SAVES"),
                                          ("rejoins", "REJOINS"))):
            self._st[key] = self._tile(grid, col, cap, "0", row=1,
                                       height=76)

        self._group(body, "LAST 14 DAYS")
        chart = self._card(body, corner_radius=14)
        chart.pack(fill="x")
        # inset from the card's rounded corners, so no bar comes near them
        self.cv_days = tk.Canvas(chart, height=self._px(158), bg=CARD,
                                 highlightthickness=0, bd=0)
        self.cv_days.pack(fill="x", padx=16, pady=12)
        self.cv_days.bind("<Configure>", self._days_resized, add="+")
        self.cv_days.bind("<Motion>", self._days_hover, add="+")
        self.cv_days.bind("<Leave>", lambda _e: self._days_tip(None),
                          add="+")
        self._days_drawn = None
        self._days_img = self._tip_img = None

        foot = ctk.CTkFrame(body, fg_color="transparent")
        foot.pack(fill="x", pady=(14, 8))
        self.lbl_stats_since = ctk.CTkLabel(foot, text="", text_color=MUTED,
                                            font=self.F(11), height=16)
        self.lbl_stats_since.pack(side="left", padx=4)
        self.btn_reset_stats = self._ghost(foot, "Reset stats",
                                           self._reset_stats_click)
        self.btn_reset_stats.configure(width=104, text_color=SUBTLE)
        self.btn_reset_stats.pack(side="right")
        self._stats_shown = None
        self._paint_stats()

    def _stats_now(self) -> dict:
        """Stored totals plus the part of a live run not yet added."""
        st = self.session.stats
        now = time.time()
        extra = self.session.unflushed(now)
        cur = (st.get("current") or {}) if self.session.live else {}
        v = {"total": st.get("total_secs", 0) + extra,
             "runs": st.get("runs", 0),
             "longest": max(st.get("longest_secs", 0),
                            cur.get("secs", 0) + extra if cur else 0)}
        for k in ("rares", "votes", "anti_afk", "rejoins"):
            v[k] = st.get(k, 0)
        return v

    def _paint_stats(self):
        try:
            v = self._stats_now()
            empty = v["runs"] == 0
            if empty != self._stats_shown:
                self._stats_shown = empty
                if empty:
                    self._stats_body.pack_forget()
                    self._stats_empty.pack(fill="x", pady=(16, 0))
                else:
                    self._stats_empty.pack_forget()
                    self._stats_body.pack(fill="x")
            if empty:
                return
            st, t = self.session.stats, self._st
            t["total"].configure(text=span(v["total"]) if v["total"] >= 60
                                 else "0m")
            first = str(st.get("first_run", ""))[:10]
            t["total"].sub.configure(text=f"since {self._day(first)}"
                                     if first else "")
            t["longest"].configure(text=span(v["longest"]))
            on = st.get("longest_on", "")
            t["longest"].sub.configure(text=f"on {self._day(on)}" if on
                                       else "")
            t["runs"].configure(text=str(v["runs"]))
            unclean = st.get("unclean_ends", 0)
            t["runs"].sub.configure(text=f"{unclean} cut short"
                                    if unclean else "")
            for k in ("rares", "votes", "anti_afk", "rejoins"):
                t[k].configure(text=f"{int(v[k]):,}")
            last = str(st.get("last_run", ""))[:10]
            self.lbl_stats_since.configure(
                text=f"Last run {self._day(last)} · saved in stats.json"
                if last else "Saved in stats.json")
            self._draw_days()
        except Exception:
            pass

    @staticmethod
    def _day(iso):
        try:
            return time.strftime("%b %d", time.strptime(iso[:10], "%Y-%m-%d")
                                 ).replace(" 0", " ")
        except Exception:
            return iso

    def _draw_days(self, force=False):
        """The 14-day bars. Redrawn only when the size or a value changed."""
        import datetime
        cv = self.cv_days
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
        except Exception:
            return
        if w < 50:
            return
        today = datetime.date.today()
        days = [today - datetime.timedelta(days=13 - i) for i in range(14)]
        stored = self.session.stats.get("days", {})
        vals = [stored.get(d.isoformat(), 0) for d in days]
        vals[-1] += self.session.unflushed(time.time())
        sig = (w, h, tuple(int(x // 60) for x in vals))
        if sig == self._days_drawn and not force:
            return
        self._days_drawn = sig
        p = self._px
        pad = p(6)                     # equal room at both ends
        top, bottom = p(24), p(24)     # value labels above, days below
        base = h - bottom
        slot = (w - 2 * pad) / 14.0
        bar = max(p(8), min(p(22), round(slot * 0.46)))
        peak = max(max(vals), 3600)
        best = max(range(14), key=lambda i: vals[i])
        bars, tops = [], []
        for i, v in enumerate(vals):
            cx = pad + slot * i + slot / 2
            x0 = round(cx - bar / 2)
            is_today = i == 13
            y = base - max(bar, (base - top) * v / peak) if v >= 60 else base
            tops.append(y)
            bars.append({"x0": x0, "x1": x0 + bar, "top": top, "base": base,
                         "y": y,
                         "cols": None if v < 60 else
                         DAY_TODAY if is_today else DAY_BAR,
                         "glow": ACCENT if is_today else None})
        try:
            img = render_day_bars(w, h, bars, bg=CARD, glow_px=p(5))
            self._days_img = ImageTk.PhotoImage(img, master=cv)
        except Exception:
            self._days_img = None
        cv.delete("all")
        self._tip_i = None
        if self._days_img is not None:
            cv.create_image(0, 0, image=self._days_img, anchor="nw")
        # the words stay Tk text on top of the picture, so they stay crisp
        narrow = slot < p(40)
        for i, (d, v) in enumerate(zip(days, vals)):
            cx = pad + slot * i + slot / 2
            is_today = i == 13
            show = is_today or (i == best and not (narrow and best == 12))
            if v >= 60 and show:
                cv.create_text(cx, tops[i] - p(10), text=span(v),
                               fill=TEXT if is_today else SUBTLE,
                               font=self._tkfont(10, semi=True))
            cv.create_text(cx, base + p(13),
                           text="Today" if is_today and not narrow
                           else str(d.day),
                           fill=ACCENT if is_today else MUTED,
                           font=self._tkfont(10, semi=is_today))
        self._days_geo = {"pad": pad, "slot": slot, "days": days,
                          "vals": vals, "tops": tops, "top": top, "bar": bar}

    def _days_resized(self, _e=None):
        """Resizing fires dozens of these: draw once it settles (80 ms)."""
        if self._days_job:
            try:
                self.after_cancel(self._days_job)
            except Exception:
                pass
        self._days_job = self.after(80, self._days_settled)

    def _days_settled(self):
        self._days_job = None
        self._draw_days(True)

    def _days_hover(self, e):
        geo = self._days_geo
        if not geo:
            return
        i = int((e.x - geo["pad"]) // geo["slot"])
        self._days_tip(i if 0 <= i < 14 else None)

    def _days_tip(self, i):
        """A small rounded note over the bar under the mouse: the day and its
        exact run time."""
        if i == self._tip_i:
            return
        self._tip_i = i
        cv = self.cv_days
        cv.delete("tip")
        geo = self._days_geo
        if i is None or not geo:
            return
        try:
            p = self._px
            d, v = geo["days"][i], geo["vals"][i]
            text = (f"{d.strftime('%a %b')} {d.day} · "
                    + (span(v) if v >= 60 else "no runs"))
            font = self._tkfont(11, semi=True)
            tw = tkfont.Font(root=cv, font=font).measure(text)
            bw, bh = tw + p(20), p(24)
            cx = geo["pad"] + geo["slot"] * i + geo["slot"] / 2
            cw = cv.winfo_width()
            x = min(max(cx - bw / 2, p(2)), cw - bw - p(2))
            cy = max(geo["tops"][i] - p(18), bh / 2 + p(1))
            bg_img = round_box(int(bw), int(bh), CARD_HI, LINE, None,
                               radius=p(8))
            self._tip_img = ImageTk.PhotoImage(bg_img, master=cv)
            cv.create_image(round(x), round(cy - bh / 2), image=self._tip_img,
                            anchor="nw", tags="tip")
            cv.create_text(round(x + bw / 2), round(cy), text=text,
                           fill=TEXT, font=font, tags="tip")
        except Exception:
            pass

    def _reset_stats_click(self):
        """Two clicks. The old file is renamed, never deleted."""
        if self.running or self.session.live:
            self.btn_reset_stats.configure(text="Stop the run first")
            self.after(2500, self._disarm_reset)
            return
        if not self._reset_armed:
            self._reset_armed = self.after(4000, self._disarm_reset)
            self.btn_reset_stats.configure(text="Click again to reset",
                                           width=150, text_color=RED,
                                           border_color=RED)
            return
        self.after_cancel(self._reset_armed)
        self._reset_armed = None
        try:
            fresh, kept = S.reset_stats(self.session.stats_path)
        except Exception as exc:
            self.log(f"couldn't reset the stats: {exc}", "warn")
            self._disarm_reset()
            return
        self.session.stats = fresh
        self.log(f"stats reset - the old numbers are kept in {kept}")
        self._disarm_reset()
        self._paint_stats()
        self._toast(f"Stats reset · the old numbers are kept in {kept}", "ok",
                    ("Open folder", self.open_data))

    def _disarm_reset(self):
        if self._reset_armed:
            try:
                self.after_cancel(self._reset_armed)
            except Exception:
                pass
        self._reset_armed = None
        self.btn_reset_stats.configure(text="Reset stats", width=104,
                                       text_color=SUBTLE, border_color=LINE)

    # ================================================================= Log ==
    def _build_log(self, page):
        wrap = ctk.CTkFrame(page, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=20, pady=16)
        # packed first, from the bottom, so a short window squeezes the text
        foot = ctk.CTkFrame(wrap, fg_color="transparent")
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.lbl_log_count = ctk.CTkLabel(foot, text="", text_color=MUTED,
                                          font=self.F(11), height=16)
        self.lbl_log_count.pack(side="left")
        self.btn_log_open = self._ghost(foot, "Open log.txt", self._open_log)
        self.btn_log_open.configure(width=104, height=28)
        self.btn_log_open.pack(side="right")
        self.btn_log_copy = self._ghost(foot, "Copy", self._copy_log)
        self.btn_log_copy.configure(width=72, height=28)
        self.btn_log_copy.pack(side="right", padx=(0, 8))
        self.sw_follow = ctk.CTkSwitch(
            foot, text="Follow", width=40, switch_width=30, switch_height=16,
            progress_color=ACCENT, fg_color=LINE, button_color=TEXT,
            button_hover_color=ACCENT_SOFT, text_color=SUBTLE,
            font=self.F(12), command=self._follow_switched)
        self.sw_follow.select()
        self.sw_follow.pack(side="right", padx=(0, 14))

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        self.seg_log = self._segmented(bar, ["All", "Problems"],
                                       self._set_log_mode)
        self.seg_log.pack(side="right", padx=(10, 0))
        self.e_log = ctk.CTkEntry(bar, height=28, corner_radius=8,
                                  border_width=1, border_color=LINE,
                                  fg_color=PANEL, text_color=TEXT,
                                  placeholder_text="Filter lines…",
                                  placeholder_text_color=MUTED,
                                  font=self.F(12))
        self.e_log.pack(side="left", fill="x", expand=True)
        self.e_log.bind("<KeyRelease>", self._log_query_changed, add="+")

        box = ctk.CTkFrame(wrap, fg_color=PANEL, corner_radius=14,
                           border_width=1, border_color=LINE)
        box.pack(fill="both", expand=True)
        # the raw log - every line, timestamped, for bug reports
        self.txt = ctk.CTkTextbox(box, fg_color=PANEL, text_color="#c3cfdd",
                                  border_width=0, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=11),
                                  scrollbar_button_color=LINE,
                                  scrollbar_button_hover_color=MUTED)
        self.txt.tag_config("ts", foreground=MUTED)
        self.txt.tag_config("top", foreground="#d5e0ec")
        self.txt.tag_config("sub", foreground=SUBTLE)
        # made after the tags above, so they win over them
        tb = self.txt._textbox
        tb.tag_configure("warn", foreground=AMBER)
        tb.tag_configure("err", foreground=RED)
        tb.tag_configure("hit", background=ACCENT_DIM, foreground=ACCENT_SOFT)
        tb.tag_configure("hide", elide=True)
        self.txt.configure(state="disabled")
        self.txt.pack(fill="both", expand=True, padx=8, pady=6)
        self._paint_log_count()

    # What the Log page colours (and its Problems view keeps): the level the
    # line came with, plus a few plain words for the app's own lines.
    LOG_ERR_WORDS = ("failed", "Discord says", "stopped on an error",
                     "couldn't rejoin")
    LOG_WARN_WORDS = ("couldn't", "could not", "timed out", "heads up",
                      "didn't work", "may not have worked", "is full")

    def _log_class(self, text, level="info"):
        if level == "error":
            return "err"
        if level == "warn":
            return "warn"
        if any(w in text for w in self.LOG_ERR_WORDS):
            return "err"
        if any(w in text for w in self.LOG_WARN_WORDS):
            return "warn"
        return None

    def _log_visible(self, text, cls):
        if self._log_mode == "Problems" and not cls:
            return False
        q = self._log_query
        return not q or q in text.lower()

    def _log_refilter(self):
        """Hide the lines the filter leaves out and mark what matched."""
        self._log_after = None
        try:
            tb = self.txt._textbox
            tb.tag_remove("hide", "1.0", "end")
            tb.tag_remove("hit", "1.0", "end")
            last = int(tb.index("end-1c").split(".")[0])
            q = self._log_query
            shown = 0
            for n in range(1, last):
                line = tb.get(f"{n}.0", f"{n}.end")
                names = tb.tag_names(f"{n}.10")
                cls = "err" if "err" in names else "warn" if "warn" in names else None
                if not self._log_visible(line[10:], cls):
                    tb.tag_add("hide", f"{n}.0", f"{n + 1}.0")
                    continue
                shown += 1
                if q:
                    low, at = line.lower(), 10
                    while True:
                        at = low.find(q, at)
                        if at < 0:
                            break
                        tb.tag_add("hit", f"{n}.{at}", f"{n}.{at + len(q)}")
                        at += len(q)
            self._log_shown = shown
            if self._page == "log" and self._log_follow:
                tb.see("end")
        except Exception:
            pass
        self._paint_log_count()

    def _paint_log_count(self):
        try:
            total = int(self.txt._textbox.index("end-1c").split(".")[0]) - 1
            if not self._log_query and self._log_mode == "All":
                self._log_shown = total
                text = f"{total} line{'s' if total != 1 else ''}"
            else:
                text = f"{self._log_shown} of {total} lines"
            self.lbl_log_count.configure(text=text)
        except Exception:
            pass

    def _log_query_changed(self, _e=None):
        q = self.e_log.get().strip().lower()
        if q == self._log_query:
            return
        self._log_query = q
        # typing fast refilters once, after the last key
        if self._log_after:
            self.after_cancel(self._log_after)
        self._log_after = self.after(150, self._log_refilter)

    def _set_log_mode(self, value):
        if value != self._log_mode:
            self._log_mode = value
            self._log_refilter()

    def _follow_switched(self):
        self._log_follow = bool(self.sw_follow.get())
        if self._log_follow:
            self.txt.see("end")

    def _copy_log(self):
        """The lines showing now, to the clipboard."""
        try:
            tb = self.txt._textbox
            last = int(tb.index("end-1c").split(".")[0])
            lines = [tb.get(f"{n}.0", f"{n}.end") for n in range(1, last)
                     if "hide" not in tb.tag_names(f"{n}.0")]
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.btn_log_copy.configure(text="Copied")
            self.after(1500, lambda: self.btn_log_copy.configure(text="Copy"))
            self._toast(f"Copied {len(lines)} line"
                        f"{'s' if len(lines) != 1 else ''} to the clipboard",
                        "ok")
        except Exception as exc:
            self.log(f"couldn't copy the log: {exc}", "warn")

    def _open_log(self):
        self._open_path(paths.log_path, "log.txt")

    def open_data(self):
        self._open_path(paths.ensure_data_dir, "the data folder")

    def _open_path(self, where: Callable[[], str], what: str):
        """Explorer or Notepad on one of our own files. Never while testing."""
        try:
            safety.guard(f"open {what}")
            os.startfile(where())
        except safety.SideEffectBlocked:
            self._toast("Opening files is off while testing", "info")
        except Exception as exc:
            self.log(f"couldn't open {what}: {exc}", "warn")

    def log(self, msg, level: str = "info"):
        """One line: to log.txt, and to the Log page with its colour. Tk
        thread only - workers go through _ui, the engine through emit()."""
        text = str(msg)
        write_log_line(text if level == "info" else f"[{level}] {text}")
        try:
            stamp = time.strftime("%H:%M:%S")
            self.txt.configure(state="normal")
            first = int(self.txt.index("end-1c").split(".")[0])
            self.txt.insert("end", f"{stamp}  ", "ts")
            sub = text.startswith("  ")
            self.txt.insert("end", f"{text}\n", "sub" if sub else "top")
            # coloured and filtered as it goes in
            tb = self.txt._textbox
            end = int(self.txt.index("end-1c").split(".")[0])
            cls = self._log_class(text, level)
            if cls:
                tb.tag_add(cls, f"{first}.10", f"{end}.0")
            if cls == "err" and self._page != "log" and not self._log_alert:
                self._log_alert = True       # a red dot on Log until seen
                self._paint_badges()
            if self._log_query or self._log_mode != "All":
                if self._log_visible(text, cls):
                    self._log_shown += 1
                    q = self._log_query
                    at = text.lower().find(q) if q else -1
                    if at >= 0:
                        tb.tag_add("hit", f"{first}.{at + 10}",
                                   f"{first}.{at + 10 + len(q)}")
                else:
                    tb.tag_add("hide", f"{first}.0", f"{end}.0")
            # A day-long run logs tens of thousands of lines, and a Text widget
            # that size gets slow. The full history is in log.txt anyway.
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > 2000:
                self.txt.delete("1.0", f"{lines - 1500}.0")
                if self._log_query or self._log_mode != "All":
                    self._log_refilter()
            if self._page == "log" and self._log_follow:
                self.txt.see("end")
            self.txt.configure(state="disabled")
            self._paint_log_count()
        except Exception:
            pass

    # ============================================================ Settings ==
    def _build_settings(self, page):
        self.set_page = self._scroll_page(page)
        sp = self.set_page
        s = self.settings

        self._group(sp, "WHILE IT RUNS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_awake = self._switch_row(
            card, "Keep the PC awake",
            "Stops Windows going to sleep during a run. The screen can still "
            "turn off.", first=True, on=s.get("keep_awake", True) is True,
            command=self._awake_switched)
        self.sw_bar = self._switch_row(
            card, "Session bar on top of Roblox",
            "A slim stats bar at the top of your screen during a run: time, "
            "rarest block, votes, rejoins. Click-through, never takes focus, "
            "only while Roblox is in front.", on=s.get("session_bar", True) is True,
            command=self._bar_switched)
        self.sw_pinned = self._switch_row(
            card, "Open the pinned recipe at the start",
            "Clicks the arrow on the right so your pinned recipe shows.",
            on=s.get("open_pinned", True) is True,
            command=lambda: self._flag_switched("open_pinned", self.sw_pinned))
        self.sw_center = self._switch_row(
            card, "Put the cursor in the middle at the start",
            "Moves your mouse to the exact middle of the screen.",
            on=s.get("center_cursor", True) is True,
            command=lambda: self._flag_switched("center_cursor", self.sw_center))
        self.sw_pics = self._switch_row(
            card, "Save pictures",
            "Saves a picture of rare blocks at your rarity and of problems to the data "
            "folder (to help fix bugs). Off: nothing is saved to your files.",
            on=s.get("save_pictures") is True,
            command=lambda: self._flag_switched("save_pictures", self.sw_pics))
        self.sw_sjump = self._switch_row(
            card, "Jump at the start",
            "One jump right after the setup above.",
            on=s.get("start_jump", True) is True,
            command=lambda: self._flag_switched("start_jump", self.sw_sjump))
        cur = bar_rare_floor(s)
        vals = sorted(set(BAR_RARE_STEPS) | {cur})
        self.opt_bar_rare = self._option_row(
            card, "Count as a rare on the bar",
            "Blocks this rare or rarer count in the bar's RARES number.",
            [rarity_text(v) + "+" for v in vals], rarity_text(cur) + "+",
            self._bar_rare_picked)

        self._group(sp, "WINDOW")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self.sw_ontop = self._switch_row(
            card, "Keep on top while idle",
            "Same as the pin. Steps back while a run is on.",
            first=True, on=bool(self._ui_set("pin")),
            command=self._ontop_switched)
        self.sw_motion = self._switch_row(
            card, "Animations", "The shimmer and fades while it runs.",
            on=self._ui_set("motion", True) is not False,
            command=lambda: self._ui_switched("motion", self.sw_motion))
        self.sw_remember = self._switch_row(
            card, "Remember window and page",
            "Opens at the same size, place and page.",
            on=self._ui_set("remember", True) is not False,
            command=lambda: self._ui_switched("remember", self.sw_remember))

        self._group(sp, "UPDATES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self._update_switch_row(card)        # the same switch as on About

        self._group(sp, "DISPLAY")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        self.lbl_disp_val = ctk.CTkLabel(row, text="", height=18,
                                         font=self.F(12, semi=True))
        self.lbl_disp_val.pack(side="right", anchor="n")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ctk.CTkLabel(txt, text="Screen", text_color=TEXT, font=self.F(13),
                     anchor="w", height=18).pack(fill="x")
        self.lbl_disp_note = ctk.CTkLabel(
            txt, text="", text_color=SUBTLE, font=self.F(11), anchor="w",
            justify="left", height=16, wraplength=420)
        self.lbl_disp_note.pack(fill="x")
        self._rewrap_on(txt, (self.lbl_disp_note,))

        self._group(sp, "FILES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self._link_row(card, "Open data folder",
                       "Settings, log, stats and saved pictures", self.open_data,
                       first=True)
        self._link_row(card, "Open log.txt",
                       "Every line it has written, for bug reports",
                       self._open_log)

        ctk.CTkLabel(sp, text=(f"{APP_NAME} {APP_VER} · tested on 1920×1080 "
                               f"at 100% · F1 starts, F2 stops"),
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(18, 8))

    def _ui_switched(self, key, switch):
        self.settings.setdefault("ui", {})[key] = bool(switch.get())
        self._changed()

    def _ontop_switched(self):
        if bool(self.sw_ontop.get()) != bool(self._ui_set("pin")):
            self._toggle_pin()

    def _bar_rare_picked(self, value):
        n = rarity_from_text(str(value).rstrip("+"))
        if n:
            self.settings["bar_rare_min"] = n
            self._changed()

    def _flag_switched(self, key, switch):
        self.settings[key] = bool(switch.get())
        self._changed()

    def _bar_switched(self):
        self.settings["session_bar"] = bool(self.sw_bar.get())
        self._changed()

    # ========================================================= session bar ==
    def _update_bar(self, now: float):
        """Once a second: the stats strip at the top of the screen. Made on the
        first tick (the app has focus then), shown only during a run while
        Roblox is the window in front, so it never sits over your other apps."""
        bar = getattr(self, "_bar", None)
        if bar is None:
            if getattr(self, "_bar_failed", False) or safety.testing():
                return
            try:
                from mayhem.overlay import SessionBar
                bar = self._bar = SessionBar(self)
            except Exception as exc:
                self._bar_failed = True
                self.log(f"couldn't make the session bar ({exc})", "warn")
                return
        try:
            from mayhem import window as W
            want = (self.running and bool(self.session_start)
                    and self.settings.get("session_bar", True) is True)
            if want:
                h = W.roblox_hwnd()
                want = bool(h) and W.is_foreground(h)
            if not want:
                bar.hide()
                return
            sw = self.winfo_screenwidth()
            c = self.session.counts
            floor = bar_rare_floor(self.settings)
            rarest, rares = bar_rares(self._bar_spawns, self._bar_found, floor)
            if rarest and rarest[2] and not str(rarest[2]).startswith("#"):
                rarest = (rarest[0], rarest[1], _colour(rarest[2]))
            mine = [m for m in self.settings.get("vote_maps") or [] if m in MAPS]
            if mine:
                votes = ("YOUR MAPS", str(sum(1 for v in self._bar_votes if v in mine)))
            else:
                votes = ("VOTES", str(len(self._bar_votes)))
            bar.update({
                "time": span(now - self.session_start),
                "state": "running" if self._mode in (None, "running") else str(self._mode),
                "rarest": rarest,
                "rares": rares,
                "rares_label": "RARES " + short_odds(floor),
                "votes": votes,
                "rejoins": c.get("rejoins", 0), "afk": c.get("afk", 0),
                "world": self._hud_world,
            }, sw)
            bar.show(sw)
        except Exception as exc:
            if not getattr(self, "_bar_err_noted", False):
                self._bar_err_noted = True
                self.log(f"session bar problem ({exc})", "warn")

    def _awake_switched(self):
        on = bool(self.sw_awake.get())
        self.settings["keep_awake"] = on
        self._changed()
        if self.running:
            keep_awake(on)

    # =============================================================== About ==
    SHORTCUTS = (("F1", "Start · works with Roblox in front"),
                 ("F2", "Stop · works with Roblox in front"),
                 ("Ctrl 1–0", "Go to a page, in sidebar order"),
                 ("Ctrl Tab", "Next page · with Shift, the one before"),
                 ("Ctrl F", "Search the log"),
                 ("Ctrl T", "Keep on top while idle, on or off"),
                 ("Ctrl B", "Wide sidebar or the narrow rail"),
                 ("Esc", "Hide a note, clear the log search"))

    def _build_about(self, page):
        sp = self._scroll_page(page)
        card = self._card(sp)
        card.pack(fill="x", pady=(16, 0))
        self._sheen(card)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=18)
        mark = tk.Canvas(inner, width=self._px(44), height=self._px(44),
                         bg=CARD, highlightthickness=0, bd=0)
        draw_cube(mark, 0, 0, self._px(44))
        mark.pack(side="left", anchor="n")
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True, padx=(16, 0))
        name = ctk.CTkFrame(txt, fg_color="transparent")
        name.pack(fill="x")
        ctk.CTkLabel(name, text="Mayhem", text_color=ACCENT, height=28,
                     font=self.F(22, semi=True)).pack(side="left")
        ctk.CTkLabel(name, text="Macro", text_color=TEXT, height=28,
                     font=self.F(22, semi=True)).pack(side="left")
        ctk.CTkLabel(name, text=f"Version {APP_VER}", text_color=MUTED,
                     height=28, font=self.F(12)).pack(side="left",
                                                      padx=(10, 0))
        tag = ctk.CTkLabel(txt, text="Keeps you in Block Mayhem X while you're "
                                     "away: stops the AFK kick, rejoins your "
                                     "server, votes for your maps and tells "
                                     "you about rare blocks.",
                           text_color=SUBTLE, font=self.F(12), anchor="w",
                           justify="left", height=18, wraplength=500)
        tag.pack(fill="x", pady=(2, 0))
        self._rewrap_on(txt, (tag,))
        by = ctk.CTkFrame(txt, fg_color="transparent")
        by.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(by, text="Made by", text_color=MUTED, height=16,
                     font=self.F(11)).pack(side="left")
        ctk.CTkLabel(by, text="Ariliux", text_color=TEXT, height=16,
                     font=self.F(11, semi=True)).pack(side="left", padx=(5, 0))
        ctk.CTkLabel(by, text="·   Screen", text_color=MUTED, height=16,
                     font=self.F(11)).pack(side="left", padx=(10, 0))
        self.lbl_res_about = ctk.CTkLabel(by, text="", height=16,
                                          font=self.F(11, semi=True))
        self.lbl_res_about.pack(side="left", padx=(6, 0))

        self._build_update_card(sp)

        self._group(sp, "SHORTCUTS")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        for i, (key, what) in enumerate(self.SHORTCUTS):
            if i:
                self._hairline(card)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=8)
            cap = ctk.CTkFrame(row, fg_color=PANEL, corner_radius=6,
                               border_width=1, border_color=KEYCAP_LINE,
                               width=72, height=24)
            cap.pack(side="left")
            cap.pack_propagate(False)
            ctk.CTkLabel(cap, text=key, text_color=SUBTLE, height=18,
                         font=self.F(10, semi=True, family="Consolas")
                         ).pack(expand=True)
            ctk.CTkLabel(row, text=what, text_color=TEXT, font=self.F(12),
                         anchor="w", height=18).pack(side="left",
                                                     padx=(12, 0))
        ctk.CTkLabel(sp, text=self.PRIVACY_NOTE,
                     text_color=MUTED, font=self.F(11), height=16
                     ).pack(pady=(16, 8))

    # ============================================================= Updates ==
    # One read-only request to GitHub for this repo's newest release, when the
    # app opens and every 12 hours (mayhem/update.py does the asking and the
    # installing). It never asks and never updates during a run: both wait
    # until the run stops. From source it only says a new version is out;
    # only the built app replaces itself. GitHub is only mentioned in this
    # section (tests/test_ui_logic.py checks).
    UPDATE_NOTE = ("When it opens and every 12 hours it asks GitHub if there's a "
                   "newer version. Nothing about you is sent.")
    PRIVACY_NOTE = ("Your data stays on this PC. It only sends your own Discord "
                    "alerts (if you turn them on) and asks GitHub for updates.")

    def _init_updates(self):
        self._update_info = None            # the newer release found, or None
        self._update_asked = False          # asked about it (until asked again)
        self._update_pending = False        # found during a run: ask once it stops
        self._update_yes_pending = False    # Yes during a run: update once it stops
        self._updating = False
        self._checking = False
        self._upd_was_running = False
        self._update_switches = []          # About's and Settings' switch
        self._ask_box = None

    def _build_update_card(self, sp):
        """About's UPDATES card: the switch, what the last check found and
        Check now."""
        self._group(sp, "UPDATES")
        card = self._card(sp, corner_radius=14)
        card.pack(fill="x")
        self._update_switch_row(card)
        self._hairline(card)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        self.btn_check = self._ghost(row, "Check now", self._check_now)
        self.btn_check.configure(width=100)
        self.btn_check.pack(side="right")
        self.lbl_upd = ctk.CTkLabel(row, text="", text_color=SUBTLE,
                                    font=self.F(12), anchor="w", height=18)
        self.lbl_upd.pack(side="left", fill="x", expand=True)
        # once an update is found the line asks again (in the narrow rail the
        # sidebar's link is folded away)
        self.lbl_upd.bind("<Button-1>", lambda _e: self._open_update())
        self._paint_upd()

    def _update_switch_row(self, card, first=True):
        holder = []
        sw = self._switch_row(card, "Check for updates", self.UPDATE_NOTE,
                              first=first,
                              on=self.settings.get("check_updates", True) is True,
                              command=lambda: self._update_switched(holder[0]))
        holder.append(sw)
        self._update_switches.append(sw)
        return sw

    def _build_update_link(self, foot):
        """The sidebar footer's 'Update 1.0.1 available' link, shown in place
        of the screen line once a newer version is found."""
        self.lnk_update = ctk.CTkLabel(foot, text="", text_color=ACCENT,
                                       font=self.F(11, semi=True), anchor="w",
                                       height=16, cursor="hand2")
        self.lnk_update.bind("<Button-1>", lambda _e: self._open_update())

    def _update_switched(self, sw):
        on = bool(sw.get())
        self.settings["check_updates"] = on
        self._changed()
        for other in self._update_switches:      # About and Settings agree
            if other is not sw and bool(other.get()) != on:
                other.select() if on else other.deselect()
        self._paint_upd()

    def _paint_upd(self, note=None, colour=None):
        """About's update line: what the last check found."""
        lbl = getattr(self, "lbl_upd", None)
        if lbl is None:
            return
        info = self._update_info
        if note is None and info:
            v = info.get("version", "")
            note, colour = ((f"Version {v} is out · click here to update"
                             if update.frozen() else
                             f"Version {v} is out · this copy runs from source"),
                            ACCENT)
        elif note is None:
            note = ("Checks when it opens and every 12 hours."
                    if self.settings.get("check_updates", True) is True
                    else "Update checks are off.")
        elif note.startswith("update check: up to date"):
            note, colour = f"Up to date · checked {time.strftime('%H:%M')}", GREEN
        elif note.startswith("update check:"):
            note, colour = "Couldn't reach GitHub · it'll try again later.", AMBER
        try:
            lbl.configure(text=note, text_color=colour or SUBTLE,
                          cursor="hand2" if info else "")
        except Exception:
            pass

    def _paint_update_link(self):
        """Sidebar footer: the update link instead of the screen line."""
        info = self._update_info
        try:
            if info:
                self.lnk_update.configure(
                    text=f"Update {info.get('version', '')} available ›")
                self.lbl_res.pack_forget()
                if not self.lnk_update.winfo_manager():
                    self.lnk_update.pack(side="left", padx=20)
            else:
                self.lnk_update.pack_forget()
                if not self.lbl_res.winfo_manager():
                    self.lbl_res.pack(side="left", padx=20)
        except Exception:
            pass

    def _paint_check_button(self):
        try:
            self.btn_check.configure(
                state="disabled" if self._checking else "normal",
                text="Checking…" if self._checking else "Check now")
        except Exception:
            pass

    def _start_update_checks(self):
        """The background check. Never while testing: tests drive
        _check_updates_once with a fake answer instead."""
        if safety.testing():
            return
        threading.Thread(target=self._update_worker, daemon=True,
                         name=f"{APP_NAME} updates").start()

    def _update_worker(self):
        """Background thread: ask once the window has settled, then every
        UPDATE_EVERY seconds until a newer version turns up. Only touches the
        window through _ui()."""
        time.sleep(4)
        # Check now on About may find it first: then this loop is done too
        while not (self._update_info or self._check_updates_once()):
            time.sleep(update.UPDATE_EVERY)

    def _check_updates_once(self):
        """One check, from any thread. True once a newer release has been
        found (and handed to the window). With the switch off, GitHub is
        never even asked."""
        if self.settings.get("check_updates", True) is not True:
            return False
        info = update.release_info()
        if info and update.is_newer(info.get("version")):
            self._ui(lambda i=info: self._show_update(i))
            return True                      # one notice is enough
        # one quiet line on the Log page and in log.txt, so "did it even
        # check?" has an answer
        note = (f"update check: up to date (latest on GitHub is {info['version']})"
                if info else "update check: couldn't reach GitHub - will try later")
        self._ui(lambda n=note: (self.log(n), self._paint_upd(n)))
        return False

    def _check_now(self):
        """About's Check now: one check on its own thread, only from this
        click."""
        if self._checking:
            return
        if self._update_info:                # already found: ask again
            self._paint_upd()
            self._open_update()
            return
        if self.settings.get("check_updates", True) is not True:
            self._paint_upd("Turn on Check for updates first.", AMBER)
            return
        self._checking = True
        self._paint_check_button()

        def run():
            try:
                self._check_updates_once()
            except Exception:
                pass
            finally:
                self._ui(self._check_done)
        threading.Thread(target=run, daemon=True, name=f"{APP_NAME} check").start()

    def _check_done(self):
        self._checking = False
        self._paint_check_button()
        if self._update_info:
            return                           # _show_update has said it already
        try:
            text = self.lbl_upd.cget("text")
            colour = self.lbl_upd.cget("text_color")
        except Exception:
            return
        self._toast(text, {GREEN: "ok", AMBER: "warn"}.get(colour, "info"))

    def _show_update(self, info):
        """A newer release turned up (Tk thread)."""
        prev = self._update_info
        if prev and prev.get("version") == info.get("version"):
            return              # Check now and the background check both found it
        self._update_info = info
        self._update_asked = False
        self.log(f"update available: {APP_NAME} {info.get('version', '')}")
        self._paint_update_link()
        self._paint_upd()
        self._paint_badges()
        self._ask_update()

    def _open_update(self):
        """The sidebar link and About's line: ask again, the same way."""
        if not self._update_info:
            return
        self._update_asked = False
        self._ask_update()

    def _ask_update(self):
        """'v1.0.1 is available. Do you want to update?' Never during a run:
        that would end someone's AFK session, so it waits for the run to stop
        (see _update_tick). From source there's nothing to install, so it
        only says the new version is out."""
        info = self._update_info
        if not info or self._updating:
            return
        if self.running:
            self._update_pending = True
            return
        if self._update_asked:
            return
        self._update_asked = True
        self._update_pending = False
        version = info.get("version", "")
        if not update.frozen():
            self._toast(f"{APP_NAME} {version} is out · this copy runs from "
                        f"source, so it doesn't update itself", "info")
            return
        self._show_update_question(version)

    def _show_update_question(self, version):
        """The question as a card inside the window: never a Windows pop-up,
        so it can't take focus from Roblox or anything else."""
        if self._ask_box is None:
            box = ctk.CTkFrame(self, fg_color=CARD_HI, corner_radius=16,
                               border_width=1, border_color=ACCENT_DEEP)
            inner = ctk.CTkFrame(box, fg_color="transparent")
            inner.pack(padx=24, pady=(18, 16))
            self._ask_text = ctk.CTkLabel(inner, text="", text_color=TEXT,
                                          font=self.F(14), anchor="w",
                                          justify="left")
            self._ask_text.pack(fill="x")
            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", pady=(14, 0))
            ctk.CTkButton(row, text="Yes", width=84, height=30, corner_radius=10,
                          fg_color=ACCENT, hover_color=ACCENT_SOFT, text_color=INK,
                          font=self.F(12, semi=True),
                          command=lambda: self._answer_update("yes")
                          ).pack(side="right")
            self._ghost(row, "No", lambda: self._answer_update("no")
                        ).pack(side="right", padx=(0, 8))
            self._ask_box = box
        try:
            self._ask_text.configure(
                text=f"v{version} is available.\nDo you want to update?")
            self._ask_box.place(relx=0.5, rely=0.5, anchor="center")
            self._ask_box.lift()
        except Exception:
            pass

    def _update_question_shown(self) -> bool:
        try:
            return bool(self._ask_box is not None and self._ask_box.winfo_manager())
        except Exception:
            return False

    def _hide_update_question(self):
        try:
            if self._ask_box is not None:
                self._ask_box.place_forget()
        except Exception:
            pass

    def _answer_update(self, choice):
        """Yes or No on the question card."""
        self._hide_update_question()
        self._update_answer(choice)

    def _update_answer(self, choice):
        self.log(f"update question: {choice or 'not answered'}")
        if choice != "yes":
            self._toast("Okay · About has the update whenever you want it", "info")
            return
        if self.running:
            # Yes, then a run started before it got going: keep the Yes and
            # update the moment the run stops (see _update_tick)
            self._update_yes_pending = True
            self.log("update: will install when this run stops")
            return
        self._start_update()

    def _update_tick(self):
        """Once a second (from _tick): no question stays up during a run, and
        a question or a Yes that had to wait goes ahead once the run stops."""
        if self.running:
            self._upd_was_running = True
            if self._update_question_shown():
                self._hide_update_question()
                self._update_asked = False
                self._update_pending = True
            return
        if not self._upd_was_running:
            return
        self._upd_was_running = False
        if self._update_yes_pending:
            self._update_yes_pending = False
            self.after(1500, self._start_update)
        elif self._update_pending:
            self.after(1500, self._ask_update)

    def _start_update(self):
        """After a Yes: download, unpack, hand over and close. Only the built
        app does this; any other copy gets the download page instead."""
        info = self._update_info
        if self.running or self._updating or not info:
            return
        stopping = getattr(self, "_stopping", None)
        if stopping is not None and stopping.is_alive():
            self.after(1000, self._start_update)     # the last run is still ending
            return
        if not update.can_self_update(info):
            self.log("update: this copy can't update itself - opening the "
                     "download page")
            self._open_release_page()
            return
        self._updating = True
        self._hide_update_question()
        self._show_page("home")
        self.set_state(f"Updating to {info['version']}", "Downloading · 0%", ACCENT)
        threading.Thread(target=self._update_job, args=(info,), daemon=True,
                         name=f"{APP_NAME} update").start()

    def _update_job(self, info):
        """Worker thread: download, check, unpack, then hand over to the new
        version and close. Any failure: nothing changes."""
        title = f"Updating to {info['version']}"
        try:
            zip_path = os.path.join(update.update_dir(),
                                    f"MayhemMacro-v{info['version']}.zip")
            shown = [None]

            def progress(done, total):
                total = total or info.get("size") or 0
                if total:
                    pct = min(100, int(done * 100 / total))
                    key, text = pct, f"Downloading · {pct}% of {total / 1048576:.0f} MB"
                else:
                    mb = int(done / 1048576)
                    key, text = -1 - mb, f"Downloading · {mb} MB"
                if key != shown[0]:
                    shown[0] = key
                    self._ui(lambda t=text: self.set_state(title, t, ACCENT))
            if not update.download_update(info["zip"], info["sha256"], zip_path,
                                          progress):
                raise RuntimeError("the download didn't finish or didn't match "
                                   "its fingerprint on GitHub")
            self._ui(lambda: self.set_state(title, "Unpacking…", ACCENT))
            new_exe = update.stage_update(zip_path, update.staging_dir())
            try:
                os.remove(zip_path)
            except OSError:
                pass
            if not new_exe:
                raise RuntimeError(f"the download wasn't a {APP_NAME} build")
            update.hand_over(new_exe, update.install_dir(), os.getpid(), APP_VER)
            self._ui(lambda: self.log(f"update: handing over to {info['version']} "
                                      f"- {APP_NAME} will reopen by itself"))
            self._ui(self._close)
        except Exception as exc:
            self._ui(lambda e=str(exc): self._update_failed(e))

    def _update_failed(self, why):
        self._updating = False
        self.log(f"update failed: {why}", "warn")
        self.set_state("Update didn't work", "Nothing changed. The download page "
                                             "has the new version.", RED)
        self._toast("The update didn't work · nothing changed", "warn",
                    ("Download page", self._open_release_page))

    def _open_release_page(self):
        """The release page in the browser, only ever after a Yes or a click.
        The link is this repo's own (update.parse_release checks)."""
        page = (self._update_info or {}).get("page") or update.RELEASES_URL
        try:
            safety.guard("open the release page")
            os.startfile(page)
        except safety.SideEffectBlocked:
            self._toast("Opening the browser is off while testing", "info")
        except Exception as exc:
            self.log(f"couldn't open the release page: {exc}", "warn")

    def _after_update(self):
        """The first start after an update: say so, and tidy the update folder
        once the hand-over has had time to end. Any other start leaves that
        folder alone: a hand-over might be running from it right then."""
        try:
            old = update.take_updated_note()
        except Exception:
            old = None
        if not old:
            return
        self.log(f"updated: {APP_NAME} {old} -> {APP_VER}")
        self._toast(f"Updated to {APP_VER} · your settings and stats are as they were",
                    "ok")
        target = update.install_dir() if update.frozen() else None

        def tidy():
            time.sleep(20)
            try:
                update.tidy_after_update(target)
            except Exception:
                pass
        threading.Thread(target=tidy, daemon=True, name=f"{APP_NAME} tidy").start()

    # ======================================================= screen checks ==
    def _paint_res(self, sw, sh, pct):
        """The sidebar footer, About's Screen line and Settings > Display."""
        screen_ok = (sw, sh) == SUPPORTED_SCREEN
        scale_ok = pct == SUPPORTED_SCALING
        if screen_ok and scale_ok:
            text, colour = "1920×1080 at 100%", MUTED
        elif scale_ok:
            text, colour = f"{sw}×{sh} · tested on 1920×1080", AMBER
        elif screen_ok:
            text, colour = f"{pct}% scaling · tested at 100%", AMBER
        else:
            text, colour = f"{sw}×{sh} at {pct}% · tested on 1920×1080 at 100%", AMBER
        ok = screen_ok and scale_ok
        try:
            self.lbl_res.configure(text=text, text_color=colour)
            self.lbl_res_about.configure(text=f"{sw}×{sh} at {pct}%",
                                         text_color=SUBTLE if ok else AMBER)
            self.lbl_disp_val.configure(
                text=("✓  " if ok else "!  ") + f"{sw}×{sh} at {pct}%",
                text_color=GREEN if ok else AMBER)
            self.lbl_disp_note.configure(
                text="The setup it's tested on: reading the banner, the vote "
                     "timer and storage works best like this." if ok else
                "It's only tested on 1920×1080 at 100% scaling, so reading the "
                "game may miss things. Windows: Settings › System › Display.")
        except Exception:
            pass

    def _recheck_display(self):
        """Every few seconds: did the screen size or Windows scaling change?"""
        (sw, sh), pct = screen_size(), display_scaling()
        ok = (sw, sh) == SUPPORTED_SCREEN
        if ok == self._screen_ok and pct == self._scaling:
            return
        self._screen_ok, self._scaling = ok, pct
        self._paint_res(sw, sh, pct)
        if ok and pct == SUPPORTED_SCALING:
            self.log("screen check: 1920x1080 at 100% - all good now")
        else:
            self._warn_display()

    def _warn_display(self, feed=False):
        """A heads-up for each thing about this screen the screen reading
        isn't tested on. At open (the Log only) and on every Start."""
        (sw, sh), pct = screen_size(), display_scaling()
        self._scaling, self._screen_ok = pct, (sw, sh) == SUPPORTED_SCREEN
        if (sw, sh) != SUPPORTED_SCREEN:
            self.log(f"heads up: this screen is {sw}x{sh} - reading the game "
                     f"is only tested on 1920x1080", "warn")
            if feed:
                self._feed_add("screen", "!", "AMBER", "Screen isn't 1920×1080",
                               "reading the game may miss things")
        if pct != SUPPORTED_SCALING:
            self.log(f"heads up: Windows scaling is {pct}% - reading the game "
                     f"is only tested at 100% (Settings > System > Display "
                     f"> Scale)", "warn")
            if feed:
                self._feed_add("screen", "!", "AMBER", "Scaling isn't 100%",
                               "set it to 100% in Windows display settings")
        self._paint_res(sw, sh, pct)

    # ========================================================= live pieces ==
    def set_state(self, title, detail="", colour=None):
        if title != self._state_title:
            self._state_title = title
            self._state_since = time.time()
        self.lbl_state.configure(text=title)
        self.lbl_detail.configure(text=detail)
        self._state_detail = detail
        self._dot_colour = colour or MUTED
        self._paint_dot()
        self._paint_pill()

    def _status(self, text, colour):
        """RUNNING / IDLE. Only the dot shows it - the pill is worked out from
        the same state, so the two never disagree."""
        self._run_mode = text
        if text != "RUNNING":
            self._dot_colour = MUTED
        self._paint_dot()
        self._paint_pill()

    def _pulsing(self):
        return (self._run_mode == "RUNNING"
                and self._dot_colour in (GREEN, ACCENT, AMBER))

    def _paint_dot(self):
        try:
            self.dotc.itemconfigure(self._core, fill=self._dot_colour)
            if not self._pulsing():
                c = self._px(16) / 2
                self.dotc.coords(self._halo, c, c, c, c)
        except Exception:
            pass

    def _show_ready(self):
        self._paint_run()
        if not self.running:
            self.set_state("Ready", "Open Block Mayhem X and press F1, or hit "
                                    "Start.", MUTED)
        self._apply_topmost()
        self._paint_pill()
        self._paint_badges()

    def _paint_run(self):
        """One place that decides how the big button looks."""
        b = self.btn_run
        if self.running:
            # neutral, not a red slab - a 3-hour run should not look like an
            # error the whole time
            b.configure(text="Stop", glyph="■", fg_color=CARD_HI,
                        hover_color=RED_DIM, text_color=TEXT, glyph_color=RED,
                        border_color=LINE, key_fg=PANEL,
                        key_border=KEYCAP_LINE, key_text=SUBTLE, key="F2")
        else:
            b.configure(text="Start", glyph="▶", fg_color=ACCENT,
                        hover_color=ACCENT_SOFT, text_color=INK,
                        glyph_color=INK, border_color=ACCENT,
                        key_fg="#dd9119", key_border="#b9750e", key_text=INK, key="F1")

    # ---- motion: one clock for everything that moves ----
    def _motion(self):
        """Settings > Animations. Off = nothing slides, fades or breathes."""
        return self._ui_set("motion", True) is not False

    def _shimmer_mode(self):
        """What the header hairline shows: None, "run", or "amber" (paused,
        rejoining, or something went wrong)."""
        if self._run_mode == "RUNNING":
            if self._mode in ("paused", "rejoining") or self._dot_colour in (AMBER,
                                                                             RED):
                return "amber"
            return "run"
        return None

    def _anim_delay(self):
        """How soon the clock ticks again: 40 ms while the shimmer runs, 60
        while only a dot breathes, 300 when nothing moves (idle, minimised,
        or Animations off) - so an idle window costs next to nothing."""
        try:
            if not self._motion() or self.state() in ("iconic", "withdrawn"):
                return 300
        except Exception:
            return 300
        if self._shimmer_mode():
            return 40
        if self._pulsing() or self._pill_pulse:
            return 60
        return 300

    def _animate(self):
        """The one animation clock: the hero dot's halo, the pill's dot and
        the shimmer along the header line. Only moves and recolours canvas
        items made once at start-up - nothing is created here."""
        delay = 300
        try:
            delay = self._anim_delay()
            now = time.perf_counter()          # tests patch time.time
            live = delay < 300
            self._anim_halo(now if live and self._pulsing() else None)
            self._anim_pill(now if live and self._pill_pulse else None)
            self._anim_shimmer(now if live else None)
        except Exception:
            pass
        self._anim_delay_now = delay
        self.after(delay, self._animate)

    def _anim_halo(self, now):
        c = self._px(16) / 2
        if now is None:
            if "halo" not in self._anim_rest:
                self._anim_rest.add("halo")
                self.dotc.coords(self._halo, c, c, c, c)
            return
        self._anim_rest.discard("halo")
        period = 2.4 if self._dot_colour == AMBER else 1.6
        t = (now / period) % 1.0
        rad = self._px(4) + self._px(4) * t
        self.dotc.coords(self._halo, c - rad, c - rad, c + rad, c + rad)
        self.dotc.itemconfigure(
            self._halo, fill=blend(blend(self._dot_colour, CARD, 0.45), CARD,
                                   t))

    def _anim_pill(self, now):
        dot, fill = self._pill_colours
        if now is None:
            if "pill" not in self._anim_rest:
                self._anim_rest.add("pill")
                self.pill_dot.itemconfigure(self._pill_core, fill=dot)
            return
        self._anim_rest.discard("pill")
        k = 0.5 - 0.5 * math.cos(2 * math.pi * ((now / 1.6) % 1.0))
        self.pill_dot.itemconfigure(self._pill_core,
                                    fill=blend(dot, fill, 0.6 * k))

    def _anim_shimmer(self, now):
        """17 soft steps of a ramp, sliding along the 2px line; yellow while
        paused or rejoining."""
        hair = self.hair
        mode = self._shimmer_mode() if now is not None else None
        h = self._px(2)
        if mode is None:
            if "shim" not in self._anim_rest:
                self._anim_rest.add("shim")
                self._shim_mode = None
                for r in self._shim:
                    hair.coords(r, -99, 0, -98, h)
            return
        self._anim_rest.discard("shim")
        w = max(1, hair.winfo_width())
        if self._shim_mode != mode:            # recolour only on a change
            self._shim_mode = mode
            lo, hi = SHIMMER_AMBER if mode == "amber" else SHIMMER_RUN
            mid = (SHIMMER_RECTS - 1) / 2
            for i, r in enumerate(self._shim):
                t = 1 - abs(i - mid) / mid
                hair.itemconfigure(r, fill=blend(lo, hi, t * t))
        seg = self._px(10)
        band = seg * SHIMMER_RECTS
        speed = self._px(300)                  # px a second
        x = (now * speed) % (w + band) - band
        for i, r in enumerate(self._shim):
            hair.coords(r, x + i * seg, 0, x + (i + 1) * seg, h)

    def _show_playtime(self, secs, live=False):
        """The big number. None = never run; live = counting now; otherwise
        the last run, dimmed, so stopping does not wipe it to zero."""
        n = self._num
        if secs is None:
            self.lbl_play_cap.configure(text="RUN TIME")
            for lbl, t in zip(n, ("—", "", "", "")):
                lbl.configure(text=t)
            n[0].configure(text_color=MUTED)
            # a CTkProgressBar at 0 still draws a nub; hide it in the track
            self.bar_hour.configure(progress_color=HAIRLINE)
            self.bar_hour.set(0)
            self.lbl_play_sub.configure(text="Counts once you press Start")
            return
        s = int(secs)
        if s >= 3600:
            parts = (str(s // 3600), "h", f"{s % 3600 // 60:02d}", "m")
        else:
            parts = (str(s // 60), "m", f"{s % 60:02d}", "s")
        colour = TEXT if live else SUBTLE
        for lbl, t in zip(n, parts):
            lbl.configure(text=t)
        n[0].configure(text_color=colour)
        n[2].configure(text_color=colour)
        self.lbl_play_cap.configure(text="RUN TIME" if live else "LAST RUN")
        for v in self.val.values():
            key = "bump:%d" % id(v)
            if live and key in self._tweens:    # mid-flash: let it finish
                continue
            # stopped: a flash still going would end on the live colour
            job = self._tweens.pop(key, None)
            if job:
                try:
                    self.after_cancel(job[0])
                except Exception:
                    pass
            v.configure(text_color=colour)
        if live:
            self.bar_hour.configure(progress_color=ACCENT)
            self.bar_hour.set((s % 3600) / 3600.0)
            left = 60 - (s % 3600) // 60
            self.lbl_play_sub.configure(text=f"{left}m to {s // 3600 + 1}h")
        else:
            self.bar_hour.configure(progress_color=HAIRLINE)
            self.bar_hour.set(0)
            end = (self._last_run or {}).get("end", "")
            self.lbl_play_sub.configure(text=f"ended {end}" if end else "")

    def _paint_hud(self):
        """WORLD | NEXT VOTE | STORAGE on Home. The vote timer keeps counting
        down between reads (and rolls on to the next vote); a reading the
        engine hasn't refreshed for a while is greyed - it's a guess now."""
        now = time.monotonic()
        t = self._timer
        left = vote_left(t[0], t[1], now) if t else None
        fresh = bool(t) and now - t[1] <= HUD_FRESH and self.running
        world = self._hud_world
        st = self._storage
        full = bool(st) and st[0] >= st[1] > 0
        live = TEXT if self.running else SUBTLE
        sig = (world, clock_text(left), st, fresh, self.running)
        if sig == self._hud_painted:
            return
        self._hud_painted = sig
        try:
            self.hud_val["world"].configure(text=world or "—",
                                            text_color=live if world else MUTED)
            self.hud_val["vote"].configure(
                text=clock_text(left) if left is not None else "—",
                text_color=(ACCENT if fresh and left is not None and left <= 15
                            else live if fresh else MUTED))
            self.hud_val["storage"].configure(
                text=f"{st[0]}/{st[1]}" if st else "—",
                text_color=AMBER if full else live if st else MUTED)
        except Exception:
            pass

    def _tick(self):
        """Once a second, main thread: run time, stats, the hourly alert,
        the vote countdown, and a worker that died without saying so."""
        try:
            now = time.time()
            self._ticks = getattr(self, "_ticks", 0) + 1
            if self._ticks % 5 == 0:
                try:
                    self._recheck_display()
                except Exception:
                    pass
            self._update_bar(now)
            if self.running and self.session_start:
                self._show_playtime(now - self.session_start, live=True)
                plan = self.session.tick(now)
                if plan is not None:
                    self._apply_plan(plan)
                # A worker that died says nothing. Two ticks in a row, so an
                # engine that stopped itself has had its 'stopped' event read.
                eng = self._engine
                dead = (eng is not None and now - self._engine_t0 > 3
                        and not self._engine_alive(eng))
                self._dead_ticks = self._dead_ticks + 1 if dead else 0
                if self._dead_ticks >= 2:
                    self._dead_ticks = 0
                    self.session.engine_stopped(
                        "The run stopped unexpectedly. The Log page may say why.",
                        now, crashed=True)
                    self._stop_run(by_engine=True, why="the run part stopped")
            if self.running and self._state_since:
                self.lbl_since.configure(text="since " + time.strftime(
                    "%H:%M", time.localtime(self._state_since)))
            else:
                self.lbl_since.configure(text="")
            if self._page == "stats":
                self._paint_stats()
            self._paint_hud()
            self._paint_pill()
        except Exception:
            pass
        try:
            self._update_tick()
        except Exception:
            pass
        self.after(1000, self._tick)

    @staticmethod
    def _engine_alive(eng) -> bool:
        """False once the engine says it isn't running, or its worker thread
        has ended (a thread that died never gets to say so)."""
        thread = getattr(eng, "_thread", None)
        if isinstance(thread, threading.Thread) and not thread.is_alive():
            return False
        running = getattr(eng, "running", None)
        return True if running is None else bool(running)

    # ====================================================== start and stop ==
    def _hotkey(self):
        # keyboard's own thread: only queue the work for the Tk thread
        self._ui(self.toggle_run)

    def _hotkey_start(self):
        # keyboard's own thread: only queue the work for the Tk thread
        self._ui(lambda: None if self.running else self.toggle_run())

    def _hotkey_stop(self):
        # Straight to stop, past toggle_run's one-second debounce: an F1
        # pressed by mistake must be undone by the F2 right after it.
        self._ui(lambda: self._stop_run() if self.running else None)

    def _register_hotkey(self):
        """F1 starts, F2 stops: the keys that work while Roblox is in front.
        Never while testing: a global keyboard hook is a real side effect."""
        try:
            safety.guard("the F1/F2 hotkeys")
        except safety.SideEffectBlocked:
            return None
        try:
            import keyboard
            return (keyboard.add_hotkey("f1", self._hotkey_start),
                    keyboard.add_hotkey("f2", self._hotkey_stop))
        except Exception as exc:
            self.log(f"couldn't set up the F1/F2 keys ({exc}) - use the Start "
                     f"button", "warn")
            return None

    def toggle_run(self):
        if getattr(self, "_updating", False):
            # an update is downloading or handing over: the app closes and
            # reopens by itself in a moment
            self._toast(f"Updating · {APP_NAME} reopens by itself in a moment",
                        "info")
            return
        # Holding F1/F2 auto-repeats and a double-click comes twice: one toggle per second.
        now = time.monotonic()
        if now - getattr(self, "_last_toggle", -9.0) < 1.0:
            return
        self._last_toggle = now
        if self.running:
            self._stop_run()
            return
        if self._stopping is not None and self._stopping.is_alive():
            self._toast("Still stopping the last run · try again in a second",
                        "info")
            return
        seq = self._run_seq + 1
        try:
            eng = self._engine_factory(self._get_settings, self._emit_for(seq))
        except safety.SideEffectBlocked:
            self.log("Start is switched off in this copy (testing)", "warn")
            self._toast("Start is off while testing", "info")
            return
        except Exception as exc:
            self.log(f"couldn't start: {type(exc).__name__}: {exc}", "error")
            self._toast("Couldn't start · the Log page says why", "err")
            return
        self._run_seq = seq
        self._warn_display(feed=True)
        # (a saved link that can't work is warned about by the engine itself,
        # on its first tick, so it isn't said twice here)
        self.running = True
        self._engine = eng
        self._engine_t0 = time.time()
        self._dead_ticks = 0
        self._hud_world = None               # not known until the engine reads it
        self._bar_votes, self._bar_dens = [], []     # this run, for the session bar
        self._bar_found = []                         # collected blocks this run
        self._bar_spawns = []                        # rare spawns this run
        self._mode = "running"
        self._drop_pin()             # the pin would sit over Roblox
        if self.settings.get("keep_awake", True):
            keep_awake(True)
        self._paint_run()
        self.session_start = self._run_t0 = time.time()
        for v in self.val.values():
            v.configure(text="0", text_color=TEXT)
        self._status("RUNNING", GREEN)
        self.set_state("Starting", "Looking for Block Mayhem X…", GREEN)
        self.log("started - F2 stops it")
        hook = self.settings.get("discord") or {}
        if hook.get("webhook") and not hook.get("enabled"):
            self.log("Discord alerts are off, so nothing will be posted this run. Turn on "
                     "'Send alerts to Discord' on the Discord page.", "warn")
            self._toast("Discord alerts are off · turn them on on the Discord page", "warn")
        try:
            eng.start()
        except Exception as exc:
            self.log(f"couldn't start the run: {type(exc).__name__}: {exc}",
                     "error")
            self._engine = None
            self.running = False
            self._mode = None
            keep_awake(False)
            self._apply_topmost()
            self._paint_run()
            self.session_start = None
            self._status("IDLE", MUTED)
            self.set_state("Couldn't start", "The Log page says why.", RED)
            return
        try:
            self._apply_plan(self.session.begin(time.time()))
        except Exception:
            pass                      # stats and alerts never stop a run

    def _stop_run(self, by_engine=False, why=""):
        """Stop, F2, the engine stopping by itself, or its thread dying."""
        if not self.running:
            return
        eng, self._engine = self._engine, None
        now = time.time()
        ran = now - (self.session_start or now)
        self.running = False
        self._mode = None
        self._apply_topmost()         # the pin comes back once idle
        keep_awake(False)
        self._paint_run()
        self.session_start = None
        self._status("IDLE", MUTED)
        if eng is not None:
            # Engine.stop() waits up to 3 s for its thread - not on the Tk thread
            t = threading.Thread(target=self._stop_engine, args=(eng,),
                                 daemon=True, name=f"{APP_NAME} stop")
            self._stopping = t
            t.start()
        if by_engine:
            self.session.engine_stopped(why, now)
            self.log(f"the run stopped by itself{': ' + why if why else ''}",
                     "warn")
        else:
            self.log("stopped")
        self._last_run = {"secs": ran, "end": time.strftime("%H:%M")}
        self._show_playtime(ran, live=False)
        reason, detail = self.session.end_reason, self.session.end_detail
        c = self.session.counts
        title, colour = {"error": ("Stopped on an error", RED),
                         "stuck": ("Stopped: couldn't rejoin", RED)}.get(
            reason, ("Stopped", MUTED))
        summary = (f"Ran {span(ran)} · {c['rares']} rare{'s' if c['rares'] != 1 else ''}"
                   f" · {c['votes']} vote{'s' if c['votes'] != 1 else ''}"
                   f" · {c['rejoins']} rejoin{'s' if c['rejoins'] != 1 else ''}")
        if reason in ("error", "stuck") and detail:
            summary = detail
        elif by_engine and detail:
            summary = f"{detail} {summary}"
        self.set_state(title, summary, colour)
        seq = self._run_seq
        # a moment later, so events the engine queued just before it stopped
        # are counted first
        self.after(200, lambda: self._end_session(now, ran, seq))

    def _stop_engine(self, eng):
        try:
            eng.stop()
        except Exception as exc:
            self._ui(lambda e=f"{type(exc).__name__}: {exc}": self.log(
                f"the run didn't stop cleanly ({e})", "warn"))

    def _end_session(self, now, ran, seq, waited=0):
        if seq != self._run_seq:
            return                    # a new run started in the meantime
        t = self._stopping
        if t is not None and t.is_alive() and waited < 40:
            # Engine.stop() is still finishing (up to ~4 s): what it sends on
            # the way out (a held rare) comes before the stop alert
            self.after(100, lambda: self._end_session(now, ran, seq, waited + 1))
            return
        self._drain_events()
        try:
            plan = self.session.end(now, "stop", ran)
            if plan is not None:
                self._apply_plan(plan)
        except Exception:
            pass

    # ================================================ engine events -> UI ==
    def _on_event(self, seq, ev):
        """One event from the engine, on the Tk thread."""
        if not isinstance(ev, dict):
            return
        # A rare block the engine was still holding for a better read goes
        # out as it stops: it belongs to this run, whose end isn't sent yet.
        held_rare = (seq == self._run_seq and ev.get("type") == "rare"
                     and getattr(self.session, "live", False))
        if seq != self._run_seq or (not self.running and not held_rare):
            # a stopped run's last words: only its log lines are kept
            if ev.get("type") == "log" and ev.get("text"):
                self.log(str(ev.get("text")), str(ev.get("level") or "info"))
            return
        plan = self.session.handle(ev, time.time())
        self._apply_plan(plan)
        if plan.mode == "stopped":
            self._stop_run(by_engine=True, why=str(ev.get("why") or "").strip())

    def _apply_plan(self, plan: Plan):
        """Draw what plan_event decided. Session has already counted it and
        queued its alerts."""
        for level, text in plan.log:
            self.log(text, level)
        for row in plan.feed:
            self._feed_add(row.key, row.glyph, row.colour, row.title, row.sub)
        for key in plan.tiles:
            lbl = self.val.get(key)
            if lbl is not None:
                lbl.configure(text=str(self.session.counts.get(key, 0)))
                self._bump(lbl)
        if plan.voted:
            self._bar_votes.append(plan.voted)
        if plan.found:
            self._bar_found.append(plan.found)
        if plan.rare:
            self._bar_dens.append(plan.rare.get("den"))
            self._bar_spawns.insert(0, plan.rare)
            self._rares.insert(0, plan.rare)
            del self._rares[50:]
            self._paint_rares()
        if plan.hud:
            # the timer keeps counting down here between reads; storage and
            # the world show exactly what the engine still vouches for
            if plan.hud.get("timer") is not None:
                self._timer = (plan.hud["timer"], time.monotonic())
            self._storage = plan.hud.get("storage")
            self._hud_world = plan.hud.get("world")
            self._paint_hud()
        if plan.state and self.running:
            self._mode = plan.mode
            title, detail, colour = plan.state
            self.set_state(title, detail, _colour(colour))
        if plan.toast:
            self._toast(*plan.toast)

    # =============================================================== close ==
    def report_callback_exception(self, exc, val, tb):
        """A Tk callback raised: into log.txt (the exe has no console)."""
        write_log_line("window error: " + "".join(
            traceback.format_exception(exc, val, tb)).strip())

    def _close(self):
        was_running = self.running
        eng, self._engine = self._engine, None
        self.running = False
        if eng is not None:
            try:
                eng.stop()               # up to 3 s: the app is closing anyway
            except Exception:
                pass
        release_engine()
        self._drain_events()             # what the engine sent on its way out
        if self.session.live:
            # closing mid-run is a clean end: final stats and the alert
            try:
                now = time.time()
                self.session.end(now, "closed" if was_running else "stop",
                                 ran=now - self.session_start
                                 if self.session_start else None)
            except Exception:
                pass
        # Always: a Stop a moment ago left its 'Run stopped' alert queued (the
        # sender waits ~2 s to batch), and the process ends right after this.
        self.session.flush_sender(2.0)
        keep_awake(False)
        if self._hotkey_handle is not None:
            try:
                import keyboard
                for h in self._hotkey_handle:
                    keyboard.remove_hotkey(h)
            except Exception:
                pass
        # a page / size / place change from the last second is still waiting
        # on its timer: save it now (no-op when withdrawn/iconic)
        if self._win_save:
            try:
                self.after_cancel(self._win_save)
            except Exception:
                pass
            self._save_window()
        if self._save_job:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_settings()
        self.destroy()


def _colour(name: str) -> str:
    """A colour constant by NAME ('RED' -> '#f87171'); feed rows store names."""
    value = globals().get(str(name))
    return value if isinstance(value, str) and value.startswith("#") else SUBTLE


def main() -> None:
    # Started by an older version to finish an update (built app only): put
    # the new files in place, open the updated app and end. No window, and
    # no single-instance claim: the old app may still be closing.
    if update.finishing(sys.argv):
        try:
            update.finish_update(sys.argv[2], sys.argv[3], sys.argv[4],
                                 log=write_log_line)
        except Exception as exc:
            write_log_line(f"update failed: {type(exc).__name__}: {exc}")
        return
    # Its own taskbar id: from source, Windows would group the window under
    # python; as the app, it keeps the taskbar from showing an icon it cached
    # for an older build of the same .exe (and keeps it apart from GlassMacro).
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass
    if not claim_single_instance():
        return
    rotate_log()
    # The exe has no console: anything that crashes goes to log.txt instead
    # of nowhere.
    sys.excepthook = lambda t, v, tb: write_log_line(
        "crash: " + "".join(traceback.format_exception(t, v, tb)).strip())
    threading.excepthook = lambda a: write_log_line(
        f"thread {a.thread.name if a.thread else '?'} crashed: "
        + "".join(traceback.format_exception(a.exc_type, a.exc_value,
                                             a.exc_traceback)).strip())
    MayhemMacro().mainloop()


if __name__ == "__main__":
    # Opened with another Python (double-clicking this file): its packages
    # (text reading) live in the project's .venv, so hand over to that one.
    _here = os.path.dirname(os.path.abspath(__file__))
    _venv = os.path.join(_here, ".venv")
    _pyw = os.path.join(_venv, "Scripts", "pythonw.exe")
    if (not getattr(sys, "frozen", False) and os.path.exists(_pyw)
            and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(_venv)):
        import subprocess
        subprocess.Popen([_pyw, os.path.abspath(__file__)], cwd=_here)
    else:
        main()
