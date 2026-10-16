"""The SendInput layer: the only place MayhemMacro presses a key or moves the mouse.

Ported from GlassMacro's input section. SendInput, not PostMessage: Roblox reads
gameplay input through Raw Input, which only the focused window gets. The same
goes for the mouse, so the cursor is walked there in small RELATIVE steps -
SetCursorPos or an absolute move shifts the Windows cursor but not Roblox's.

Every public function here calls safety.guard("SendInput") before anything
else, so under MAYHEM_TESTING=1 nothing is ever sent. Every SendInput is
followed by clock.note_injected(), so the engine can tell the user's own input
apart from ours. And nothing is sent unless Roblox is the window in front at
that moment: keys sent with another window in front go into THAT window.
"""
import ctypes
import ctypes.wintypes as wintypes
import threading
import time
from typing import TYPE_CHECKING, Callable

from mayhem import safety

if TYPE_CHECKING:
    from mayhem.window import InputClock

INPUT_MOUSE, INPUT_KEYBOARD, INPUT_HARDWARE = 0, 1, 2
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008
SCAN_SPACE = 0x39

# Roblox drops instant taps at low FPS: the key has to be down for a frame or
# two. 0.15 s is enough even at a low frame rate.
SPACE_HOLD = 0.15
# Never hold a key longer than this, whatever a caller asks for: a stuck key
# would walk the player off somewhere (or into the user's chat).
MAX_HOLD = 1.0


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


# SendInput silently returns 0 when cbSize is not the real INPUT size, and
# nothing happens at all (a 48-byte INPUT is the classic mistake). Refuse to
# load rather than run a macro that quietly presses nothing.
INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
if ctypes.sizeof(INPUT) != INPUT_SIZE:
    raise ImportError(f"INPUT is {ctypes.sizeof(INPUT)} bytes, Windows wants {INPUT_SIZE}")

# Our own user32 handle, so setting argtypes here never changes the shared
# ctypes.windll.user32 that other modules call with plain ints.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND


# ------------------------------------------------------------- the clock ---
# `inputs.clock` is the one InputClock the engine shares with us. It is made on
# first use rather than at import, so importing this module never pulls in
# window.py (or fails because of it).
_clock_lock = threading.Lock()


def _clock() -> "InputClock":
    c = globals().get("clock")
    if c is None:
        with _clock_lock:
            c = globals().get("clock")
            if c is None:
                from mayhem.window import InputClock
                c = globals()["clock"] = InputClock()
    return c


def __getattr__(name: str):
    if name == "clock":
        return _clock()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ------------------------------------------------------------ low level ---
def _send(inp: INPUT) -> bool:
    """One SendInput call. True if Windows took it."""
    clock = _clock()        # first: if it can't be had, nothing is sent (no key left down)
    ok = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1
    clock.note_injected()
    return ok


def _key(scan: int, up: bool) -> INPUT:
    """A key event by SCAN code, wVk 0: Roblox can drop presses that only
    carry a virtual-key code."""
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD,
                 u=_UNION(ki=KEYBDINPUT(wVk=0, wScan=int(scan) & 0xFFFF,
                                        dwFlags=flags, time=0, dwExtraInfo=None)))


def _mouse(flags: int, dx: int = 0, dy: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE,
                 u=_UNION(mi=MOUSEINPUT(dx=int(dx), dy=int(dy), mouseData=0,
                                        dwFlags=flags, time=0, dwExtraInfo=None)))


def _cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def _same_hwnd(a: int | None, b: int | None) -> bool:
    # Window handles only use the low 32 bits, but one ctypes route hands them
    # back signed and another unsigned. Compare what actually identifies them.
    return bool(a) and bool(b) and (int(a) & 0xFFFFFFFF) == (int(b) & 0xFFFFFFFF)


def _roblox_hwnd() -> int:
    from mayhem.window import roblox_hwnd
    return roblox_hwnd()


def _roblox_front() -> int:
    """Roblox's window if it is the one in front right now, else 0."""
    h = _roblox_hwnd()
    if h and _same_hwnd(_user32.GetForegroundWindow(), h):
        return h
    return 0


def _still_front(hwnd: int) -> bool:
    return _same_hwnd(_user32.GetForegroundWindow(), hwnd)


# ------------------------------------------------------- the step maths ---
def _step(dx: int, dy: int) -> tuple[int, int]:
    """The next relative step towards a target dx, dy away; (0, 0) = there.

    Windows "enhance pointer precision" scales a delta by how fast it looks,
    so one big step lands nowhere near where it was asked to. Long moves go in
    chunks, and the last few pixels go a few at a time, where acceleration has
    nothing to apply.
    """
    if abs(dx) <= 1 and abs(dy) <= 1:
        return 0, 0
    far = max(abs(dx), abs(dy))
    step = 40 if far > 120 else (12 if far > 30 else 3)
    return max(-step, min(step, dx)), max(-step, min(step, dy))


def _walk(x: int, y: int, read_cursor: Callable[[], tuple[int, int]],
          move_rel: Callable[[int, int], bool], sleep: Callable[[float], None],
          tries: int) -> bool:
    """The move_to loop, with the cursor, the mouse and the sleep passed in.

    Re-reads the real cursor before every step rather than computing one jump,
    because pointer acceleration means the distance asked for is not the
    distance travelled. Stops early when move_rel says no (Roblox lost focus),
    and when the cursor stops getting any closer: Roblox has locked the mouse
    (first person, shift-lock), and every further step only spins the camera.
    """
    best, stale = None, 0
    for _ in range(tries):
        cx, cy = read_cursor()
        dx, dy = x - cx, y - cy
        sx, sy = _step(dx, dy)
        if (sx, sy) == (0, 0):
            return True
        far = max(abs(dx), abs(dy))
        if best is None or far < best:
            best, stale = far, 0
        else:
            stale += 1
            if stale >= STUCK_STEPS:
                break
        if not move_rel(sx, sy):
            return False
        sleep(0.010)
    cx, cy = read_cursor()
    return abs(cx - x) <= 3 and abs(cy - y) <= 3


# Roblox needs time to notice the cursor moved before the click lands. With only
# 20 ms between the two, it still believed the mouse was wherever it had been
# sitting and clicked THERE. (GlassMacro learned this on its weapon picker.)
MOVE_SETTLE = 0.14
# GlassMacro used 60 steps, which only just crosses a 1920 px screen at normal
# pointer speed. A slower Windows pointer speed shortens every step, so allow
# more - a locked mouse is caught by STUCK_STEPS long before this runs out.
MOVE_TRIES = 150
STUCK_STEPS = 6     # this many steps in a row without getting closer = give up


def _move_rel_in(hwnd: int) -> Callable[[int, int], bool]:
    """A relative move that only happens while `hwnd` is still in front."""
    def move(dx: int, dy: int) -> bool:
        if not _still_front(hwnd):
            return False
        return _send(_mouse(MOUSEEVENTF_MOVE, dx, dy))
    return move


def _hold(scan: int, hold: float) -> bool:
    """Key down, wait, key up. The key ALWAYS comes back up, even if Roblox lost
    focus meanwhile: a key left down reads as held in whatever is in front."""
    hwnd = _roblox_front()
    if not hwnd:
        return False
    if not _send(_key(scan, up=False)):
        return False
    try:
        time.sleep(min(max(0.0, float(hold)), MAX_HOLD))
    finally:
        _send(_key(scan, up=True))
    return True


# Keys for a clip hotkey (Medal etc.), by name: 'F4', 'Ctrl+F9', 'Alt+K'.
_MODS = {"CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12, "SHIFT": 0x10}


def parse_keys(text) -> list[int] | None:
    """'Ctrl+F4' -> [VK_CONTROL, VK_F4]. None unless it is modifiers plus
    exactly one F1-F24 / letter / digit key."""
    parts = [p.strip().upper() for p in str(text or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods, key = parts[:-1], parts[-1]
    if any(m not in _MODS for m in mods):
        return None
    if key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key)
    else:
        return None
    return [_MODS[m] for m in mods] + [vk]


def _vk_key(vk: int, up: bool) -> INPUT:
    """A key event that carries the virtual-key code (hotkey listeners like
    Medal watch for that), plus its scan code."""
    scan = _user32.MapVirtualKeyW(int(vk), 0) & 0xFFFF
    return INPUT(type=INPUT_KEYBOARD,
                 u=_UNION(ki=KEYBDINPUT(wVk=int(vk), wScan=scan,
                                        dwFlags=KEYEVENTF_KEYUP if up else 0,
                                        time=0, dwExtraInfo=None)))


def press_keys(text, hold: float = 0.08) -> bool:
    """Press a hotkey like 'F4' or 'Ctrl+F4' (for Medal's clip). Only with
    Roblox in front; every key always comes back up."""
    safety.guard("SendInput")
    vks = parse_keys(text)
    if not vks or not _roblox_front():
        return False
    down: list[int] = []
    try:
        for vk in vks:
            if not _send(_vk_key(vk, up=False)):
                return False
            down.append(vk)
        time.sleep(min(max(0.0, float(hold)), MAX_HOLD))
    finally:
        for vk in reversed(down):
            _send(_vk_key(vk, up=True))
    return True


# ---------------------------------------------------------------- public ---
def press_space(hold: float = SPACE_HOLD) -> bool:
    """Jump: Space by scan code (0x39), held `hold` seconds. False if nothing
    was sent because Roblox was not in front."""
    safety.guard("SendInput")
    return _hold(SCAN_SPACE, hold)


def tap_scan(scan: int, hold: float = 0.03) -> bool:
    """Press and release one key by scan code (W 0x11, A 0x1E, S 0x1F, D 0x20).
    False if nothing was sent because Roblox was not in front."""
    safety.guard("SendInput")
    return _hold(scan, hold)


def move_to(x: int, y: int) -> bool:
    """Walk the cursor to screen pixel x, y with RELATIVE moves.

    False if Roblox is not in front (or stops being), or the cursor never
    arrived - which is what happens while Roblox locks the mouse (first person,
    shift-lock), because then GetCursorPos never moves.
    """
    safety.guard("SendInput")
    hwnd = _roblox_front()
    if not hwnd:
        return False
    return _walk(int(x), int(y), _cursor_pos, _move_rel_in(hwnd), time.sleep, MOVE_TRIES)


def click_at(x: int, y: int, settle: float = 0.05) -> bool:
    """Move there for real, then left-click. False if nothing was clicked."""
    safety.guard("SendInput")
    hwnd = _roblox_front()
    if not hwnd:
        return False
    x, y = int(x), int(y)
    if not _walk(x, y, _cursor_pos, _move_rel_in(hwnd), time.sleep, MOVE_TRIES):
        return False
    time.sleep(MOVE_SETTLE)
    # Still Roblox, and the cursor still where we put it? Otherwise the click
    # would land on something nobody chose.
    cx, cy = _cursor_pos()
    if not _still_front(hwnd) or abs(cx - x) > 3 or abs(cy - y) > 3:
        return False
    if not _send(_mouse(MOUSEEVENTF_LEFTDOWN)):
        return False
    try:
        time.sleep(0.04)
    finally:
        _send(_mouse(MOUSEEVENTF_LEFTUP))       # never leave the button held
    time.sleep(0.04 + max(0.0, float(settle)))
    return True
