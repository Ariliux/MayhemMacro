"""Summon workers: click the game's Summon button again and again, right where
the cursor is.

The player opens Workers in Roblox, hovers the Roll button and presses the roll
key (F8 unless it was changed on Settings). roll() then left-clicks at that
spot every ROLL_EVERY seconds until one of these:

- the count is reached (1, 5, 10, 25, 50 or 100); "all" has no count and
  keeps clicking until one of the others;
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
PAUSE_STEP = 0.05           # how often the stop event is looked at while waiting

COUNTS = (1, 5, 10, 25, 50, 100)        # the choices on Settings, plus All
DEFAULT_COUNT = paths.DEFAULT_SETTINGS["roll_count"]
COUNT_MAX = paths.ROLL_COUNT_MAX        # a hand-edited roll_count above this is junk


@dataclass
class RollDeps:
    """Everything roll() touches, swappable in tests."""
    click: Callable[[int, int], bool]           # left-click at a screen point, False = didn't
    cursor: Callable[[], tuple[int, int]]       # where the cursor is (screen pixels)
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class RollResult:
    rolls: int                      # clicks that went through
    why: str                        # done | stopped | moved | front

    @property
    def line(self) -> str:
        """The one Activity / Log line about it."""
        return result_line(self)


# -------------------------------------------------------------- the count ---
def parse_count(value) -> int | None:
    """A roll_count setting as a number of rolls; None means all (until
    stopped). Anything that isn't a sensible count is the default, 10."""
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
    return f"{n} summon" if n == 1 else f"{n} summons"


_ZERO = {"stopped": "Didn't summon: stopped right away.",
         "moved": "Didn't summon: the mouse moved.",
         "front": "Didn't summon: Roblox isn't in front."}


def result_line(r: RollResult) -> str:
    if r.why == "done":
        return f"Summoned workers {r.rolls} time{'' if r.rolls == 1 else 's'}."
    if r.rolls == 0 and r.why in _ZERO:
        return _ZERO[r.why]
    after = _rolls(r.rolls)
    return {"stopped": f"Stopped summoning after {after}.",
            "moved": f"Stopped: you moved the mouse after {after}.",
            "front": f"Stopped: Roblox isn't in front after {after}.",
            }.get(r.why, f"Stopped summoning after {after}.")


# ------------------------------------------------------------- the loop ---
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
    """Click where the cursor is now, `count` times ('all': until stopped),
    one click every ROLL_EVERY seconds. Returns how many clicks went
    through and why it stopped. Runs on its own thread; never raises for a
    stop, only for a broken dependency."""
    limit = parse_count(count)
    spot = deps.cursor()
    rolls = 0
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
def real_deps() -> RollDeps:
    """The real mouse and cursor. Refused while testing, before anything is
    imported."""
    safety.guard("roll workers")
    from mayhem import inputs
    return RollDeps(click=lambda x, y: inputs.click_at(x, y, settle=0.0),
                    cursor=inputs._cursor_pos)


def release() -> None:
    """At exit: nothing to free any more (kept so the app's exit code stays simple)."""
