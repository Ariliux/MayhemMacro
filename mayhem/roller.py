"""Roll workers: click the game's Roll button again and again, right where the
cursor is.

The player opens Workers in Roblox, hovers the Roll button and presses the roll
key (F8 unless it was changed on Settings). roll() then left-clicks at that
spot every ROLL_EVERY seconds until one of these:

- the count is reached (1, 5, 10, 25, 50 or 100), or, with "all", the gem
  counter stops going down (GEM_STALE_CHECKS reads in a row, one every
  GEM_CHECK_EVERY clicks) or the game says "not enough";
- the roll key is pressed again (the stop event);
- the player moves the mouse more than MOVED_PX away from the spot: they took
  over;
- a click doesn't go through (inputs.click_at says False): Roblox isn't the
  window in front any more.

Everything with a side effect comes in RollDeps, so the tests drive it with
fakes. real_deps() is the real mouse and screen and is refused while testing;
every click still goes through inputs.click_at, which calls safety.guard first.
"""
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from mayhem import paths, safety

ROLL_EVERY = 0.4            # seconds from one click to the next
MOVED_PX = 20               # the mouse this far from the spot = the player took over
GEM_CHECK_EVERY = 3         # "all": read the gem counter after this many clicks
GEM_STALE_CHECKS = 3        # ...and stop once this many reads in a row found no drop
PAUSE_STEP = 0.05           # how often the stop event is looked at while waiting

COUNTS = (1, 5, 10, 25, 50, 100)        # the choices on Settings, plus All
DEFAULT_COUNT = paths.DEFAULT_SETTINGS["roll_count"]
COUNT_MAX = paths.ROLL_COUNT_MAX        # a hand-edited roll_count above this is junk


@dataclass(frozen=True)
class Gems:
    """One look at the gem counter."""
    value: int | None               # as read ('1.1K' -> 1100), None if unreadable
    not_enough: bool = False        # the game says "not enough" somewhere


@dataclass
class RollDeps:
    """Everything roll() touches, swappable in tests."""
    click: Callable[[int, int], bool]           # left-click at a screen point, False = didn't
    cursor: Callable[[], tuple[int, int]]       # where the cursor is (screen pixels)
    read_gems: Callable[[], Gems]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class RollResult:
    rolls: int                      # clicks that went through
    why: str                        # done | stopped | moved | front | gems | blind

    @property
    def line(self) -> str:
        """The one Activity / Log line about it."""
        return result_line(self)


# -------------------------------------------------------------- the count ---
def parse_count(value) -> int | None:
    """A roll_count setting as a number of rolls; None means all (until out
    of gems). Anything that isn't a sensible count is the default, 10."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "all":
            return None
        value = int(text) if text.isdigit() else None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= COUNT_MAX:
        return value
    return DEFAULT_COUNT


def count_text(value) -> str:
    """The count as the Settings menu shows it: '10' or 'All'."""
    n = parse_count(value)
    return "All" if n is None else str(n)


def count_setting(text) -> int | str:
    """A Settings menu choice as it is saved: 10, or the word 'all'."""
    n = parse_count(text)
    return "all" if n is None else n


def count_choices(current) -> list[str]:
    """The Settings menu: 1, 5, 10, 25, 50, 100 and All, plus a hand-edited
    count in its place so the menu can show it."""
    n = parse_count(current)
    numbers = sorted(set(COUNTS) | ({n} if n is not None else set()))
    return [str(v) for v in numbers] + ["All"]


def _rolls(n: int) -> str:
    return f"{n} roll" if n == 1 else f"{n} rolls"


_ZERO = {"stopped": "Didn't roll: stopped right away.",
         "moved": "Didn't roll: the mouse moved.",
         "front": "Didn't roll: Roblox isn't in front.",
         "gems": "Didn't roll: you're out of gems.",
         "blind": "Didn't roll: couldn't read your gems."}


def result_line(r: RollResult) -> str:
    if r.why == "done":
        return f"Rolled workers {r.rolls} time{'' if r.rolls == 1 else 's'}."
    if r.rolls == 0 and r.why in _ZERO:
        return _ZERO[r.why]
    after = _rolls(r.rolls)
    return {"stopped": f"Stopped rolling after {after}.",
            "moved": f"Stopped: you moved the mouse after {after}.",
            "front": f"Stopped: Roblox isn't in front after {after}.",
            "gems": f"Stopped: out of gems after {after}.",
            "blind": f"Stopped: couldn't read your gems after {after}.",
            }.get(r.why, f"Stopped rolling after {after}.")


# ------------------------------------------------------------- the loop ---
class _GemWatch:
    """'All' mode: out of gems once the counter hasn't gone down for
    GEM_STALE_CHECKS reads in a row (an unreadable read counts as no drop)."""

    def __init__(self):
        self.lowest: int | None = None
        self.stale = 0

    def check(self, gems: Gems, first: bool = False) -> str | None:
        """None to keep going, else why to stop ('gems' or 'blind')."""
        if gems.not_enough:
            return "gems"
        v = gems.value
        if v is not None and (self.lowest is None or v < self.lowest):
            self.lowest, self.stale = v, 0
            return None
        if first:                   # before any click: nothing could have dropped yet
            return None
        self.stale += 1
        if self.stale >= GEM_STALE_CHECKS:
            return "gems" if self.lowest is not None else "blind"
        return None


def _moved(at: tuple[int, int], spot: tuple[int, int]) -> bool:
    return math.hypot(at[0] - spot[0], at[1] - spot[1]) > MOVED_PX


def _pause(until: float, deps: RollDeps, stop: threading.Event) -> None:
    """Wait until `until`, in short steps, so a stop is noticed at once."""
    while not stop.is_set():
        left = until - deps.now()
        if left <= 0:
            return
        deps.sleep(min(left, PAUSE_STEP))


def roll(count, deps: RollDeps, stop: threading.Event) -> RollResult:
    """Click where the cursor is now, `count` times ('all': until out of
    gems), one click every ROLL_EVERY seconds. Returns how many clicks went
    through and why it stopped. Runs on its own thread; never raises for a
    stop, only for a broken dependency."""
    limit = parse_count(count)
    spot = deps.cursor()
    watch = _GemWatch() if limit is None else None
    rolls = 0
    if watch is not None:
        why = watch.check(deps.read_gems(), first=True)
        if why:
            return RollResult(rolls, why)
    while True:
        if stop.is_set():
            return RollResult(rolls, "stopped")
        if _moved(deps.cursor(), spot):
            return RollResult(rolls, "moved")
        started = deps.now()
        if not deps.click(*spot):
            if stop.is_set():
                return RollResult(rolls, "stopped")
            return RollResult(rolls, "moved" if _moved(deps.cursor(), spot) else "front")
        rolls += 1
        if limit is not None and rolls >= limit:
            return RollResult(rolls, "done")
        if watch is not None and rolls % GEM_CHECK_EVERY == 0:
            why = watch.check(deps.read_gems())
            if why:
                return RollResult(rolls, why)
        _pause(started + ROLL_EVERY, deps, stop)


# ------------------------------------------------------------ the hotkey ---
class PressGate:
    """Turns a hotkey's key-down events into presses.

    Holding a key repeats its key-down many times a second, and the keyboard
    library calls the hotkey for every one. Only the first is a press, until
    the key has come back up - or nothing came for QUIET seconds, in case a
    key-up was missed. down() and up() may come from the keyboard library's
    thread.
    """
    QUIET = 1.0

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self._now = now
        self._up = True
        self._last = -math.inf
        self._lock = threading.Lock()

    def down(self) -> bool:
        """A key-down came. True if it is a new press."""
        with self._lock:
            t = self._now()
            fresh = self._up or t - self._last >= self.QUIET
            self._last, self._up = t, False
            return fresh

    def up(self) -> None:
        with self._lock:
            self._up = True


# ------------------------------------------------------------ the real one ---
_ocr: list = []


def real_deps() -> RollDeps:
    """The real mouse, cursor and screen. Refused while testing, before
    anything is imported or made."""
    safety.guard("roll workers")
    from mayhem import hud, inputs, window
    from mayhem.ocr import Ocr
    if not _ocr:
        _ocr.append(Ocr())
    ocr = _ocr[0]

    def read_gems() -> Gems:
        # Only while Roblox is in front and not minimised, like every other
        # screen read.
        h = inputs._roblox_front()
        if not h or window.is_minimized(h):
            return Gems(None)
        rect = window.client_rect(h)
        if not rect:
            return Gems(None)
        try:
            img = window.grab(rect)
        except OSError:                 # locked PC, a UAC prompt: can't see
            return Gems(None)
        return Gems(hud.read_gems(ocr, img), hud.not_enough_shown(ocr, img))

    return RollDeps(click=lambda x, y: inputs.click_at(x, y, settle=0.0),
                    cursor=inputs._cursor_pos, read_gems=read_gems)


def release() -> None:
    """At exit: free the OCR engine's event loop, if one was made."""
    for o in _ocr:
        try:
            o.close()
        except Exception:
            pass
