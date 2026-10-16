"""The Roblox window: finding it, where its picture is, and whether the user is
at the PC.

Everything here only LOOKS, except bring_to_front() and restore_front(), which
move the user's focus and so go through guard("focus") first. The rest is
cheap enough to call many times per tick.

Coordinates are real screen pixels. That needs the process to be DPI aware,
which CustomTkinter turns on when the first window is made; before that,
Windows answers in scaled pixels on a scaled screen.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import threading
import time
import weakref
from collections import deque
from typing import Callable

from PIL import Image, ImageGrab

from .safety import guard

ROBLOX_EXE = "robloxplayerbeta.exe"
ROBLOX_TITLE = "Roblox"
ROBLOX_CLASS = "WINDOWSCLIENT"      # the game window's class, whatever its title says

# Our own DLL handles with their own argtypes. Setting argtypes on the shared
# ctypes.windll.user32 changes them for every other module too (GlassMacro hit
# that), and without them a 64-bit HWND can get cut to 32 bits.
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_sh32 = ctypes.WinDLL("shell32", use_last_error=True)

HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
_WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", DWORD),
                ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _proto(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


_EnumWindows = _proto(_u32, "EnumWindows", BOOL, _WNDENUMPROC, wintypes.LPARAM)
_GetWindowTextW = _proto(_u32, "GetWindowTextW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_GetClassNameW = _proto(_u32, "GetClassNameW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_SHQueryUserNotificationState = _proto(_sh32, "SHQueryUserNotificationState", ctypes.c_long,
                                       ctypes.POINTER(ctypes.c_int))
_IsWindowVisible = _proto(_u32, "IsWindowVisible", BOOL, HWND)
_IsWindow = _proto(_u32, "IsWindow", BOOL, HWND)
_IsIconic = _proto(_u32, "IsIconic", BOOL, HWND)
_GetForegroundWindow = _proto(_u32, "GetForegroundWindow", HWND)
_GetWindowThreadProcessId = _proto(_u32, "GetWindowThreadProcessId", DWORD, HWND,
                                   ctypes.POINTER(DWORD))
_GetClientRect = _proto(_u32, "GetClientRect", BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_ClientToScreen = _proto(_u32, "ClientToScreen", BOOL, HWND, ctypes.POINTER(wintypes.POINT))
_GetSystemMetrics = _proto(_u32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_GetLastInputInfo = _proto(_u32, "GetLastInputInfo", BOOL, ctypes.POINTER(LASTINPUTINFO))
_SetForegroundWindow = _proto(_u32, "SetForegroundWindow", BOOL, HWND)
_BringWindowToTop = _proto(_u32, "BringWindowToTop", BOOL, HWND)
_ShowWindow = _proto(_u32, "ShowWindow", BOOL, HWND, ctypes.c_int)
_AttachThreadInput = _proto(_u32, "AttachThreadInput", BOOL, DWORD, DWORD, BOOL)
_keybd_event = _proto(_u32, "keybd_event", None, wintypes.BYTE, wintypes.BYTE, DWORD,
                      ctypes.c_size_t)

_GetTickCount = _proto(_k32, "GetTickCount", DWORD)
_GetCurrentThreadId = _proto(_k32, "GetCurrentThreadId", DWORD)
_OpenProcess = _proto(_k32, "OpenProcess", wintypes.HANDLE, DWORD, BOOL, DWORD)
_CloseHandle = _proto(_k32, "CloseHandle", BOOL, wintypes.HANDLE)
_QueryFullProcessImageNameW = _proto(_k32, "QueryFullProcessImageNameW", BOOL, wintypes.HANDLE,
                                     DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD))
_CreateToolhelp32Snapshot = _proto(_k32, "CreateToolhelp32Snapshot", wintypes.HANDLE, DWORD, DWORD)
_Process32FirstW = _proto(_k32, "Process32FirstW", BOOL, wintypes.HANDLE,
                          ctypes.POINTER(PROCESSENTRY32W))
_Process32NextW = _proto(_k32, "Process32NextW", BOOL, wintypes.HANDLE,
                         ctypes.POINTER(PROCESSENTRY32W))

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE = ctypes.c_void_p(-1).value
SW_RESTORE, SW_SHOWMINNOACTIVE = 9, 7
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002


# --------------------------------------------------------------- processes ---
def _processes() -> list[tuple[int, str]]:
    """(pid, exe name in lower case) for every running process. A toolhelp
    snapshot, not tasklist: no child process, so no console flash and nothing
    to launch."""
    out: list[tuple[int, str]] = []
    snap = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile.lower()))
            ok = _Process32NextW(snap, ctypes.byref(entry))
    finally:
        _CloseHandle(snap)
    return out


def roblox_pids() -> list[int]:
    """Process ids of every RobloxPlayerBeta.exe (any user)."""
    return [pid for pid, exe in _processes() if exe == ROBLOX_EXE]


def roblox_running() -> bool:
    """Any RobloxPlayerBeta.exe running, with or without a window."""
    try:
        return bool(roblox_pids())
    except Exception:
        return False


def _exe_of_pid(pid: int) -> str:
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = DWORD(len(buf))
            if _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            _CloseHandle(h)
    # Not allowed to open it (another user's, or elevated): the snapshot still
    # has its name.
    for p, exe in _processes():
        if p == pid:
            return exe
    return ""


def _exe_of_window(hwnd: int) -> str:
    pid = DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _exe_of_pid(pid.value) if pid.value else ""


# ------------------------------------------------------------------ window ---
def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip()


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW(hwnd, buf, 256)
    return buf.value


def _foreground() -> int:
    return _GetForegroundWindow() or 0


def _is_roblox(hwnd: int) -> bool:
    """A Roblox game window: owned by RobloxPlayerBeta.exe (so a browser tab
    or an Explorer folder called 'Roblox' doesn't count), with Roblox's own
    window class or the title 'Roblox'. Launchers can give the window another
    title (the game's name, the server's region), so the title alone isn't
    required."""
    if not hwnd or (_class(hwnd) != ROBLOX_CLASS and _title(hwnd) != ROBLOX_TITLE):
        return False
    return _exe_of_window(hwnd) == ROBLOX_EXE


def screen_size() -> tuple[int, int]:
    """The primary screen in real pixels."""
    return int(_user32_metrics(0)), int(_user32_metrics(1))


def _user32_metrics(i: int) -> int:
    return ctypes.windll.user32.GetSystemMetrics(i)


def roblox_hwnd() -> int:
    """The Roblox game window (visible, top-level), 0 if there is none. With
    several, the one in front wins, then one titled 'Roblox'."""
    found: list[int] = []

    def cb(h, _):
        if h and _IsWindowVisible(h) and _is_roblox(h):
            found.append(int(h))
        return True

    try:
        _EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        return 0
    return _pick_roblox(found, _foreground(), _title)


def _pick_roblox(found: list[int], fg: int, title: Callable[[int], str]) -> int:
    if not found:
        return 0
    if fg in found:
        return fg
    named = [h for h in found if title(h) == ROBLOX_TITLE]
    return (named or found)[0]


# SHQueryUserNotificationState: BUSY (a full-screen app), RUNNING_D3D_FULL_SCREEN
# (a full-screen game or video) and PRESENTATION_MODE. Windows holds back its
# own notifications then; the macro holds back its disruptive steps.
_QUNS_FULLSCREEN = (2, 3, 4)


def fullscreen_app_in_front() -> bool:
    """A program other than Roblox is full screen in front: a video, another
    game, a presentation. False when Windows won't say. Only looks."""
    state = ctypes.c_int(0)
    try:
        if _SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        if state.value not in _QUNS_FULLSCREEN:
            return False
        fg = _foreground()
        return not (fg and _is_roblox(fg))      # Roblox full screen is fine
    except Exception:
        return False


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and _foreground() == hwnd


def is_minimized(hwnd: int) -> bool:
    return bool(hwnd) and bool(_IsIconic(hwnd))


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The game picture (no title bar or borders) as screen (x, y, w, h).
    None when the window is gone or minimised."""
    if not hwnd or not _IsWindow(hwnd) or _IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    if not _GetClientRect(hwnd, ctypes.byref(r)):
        return None
    p = wintypes.POINT(0, 0)
    if not _ClientToScreen(hwnd, ctypes.byref(p)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    return int(p.x), int(p.y), int(w), int(h)


def to_screen(rect: tuple[int, int, int, int], x: float, y: float) -> tuple[int, int]:
    """A point in client pixels (OCR boxes, vote options) as a screen point."""
    return rect[0] + round(x), rect[1] + round(y)


def on_primary(rect: tuple[int, int, int, int]) -> bool:
    """True if the whole rect is on the main screen."""
    x, y, w, h = rect
    return x >= 0 and y >= 0 and x + w <= _GetSystemMetrics(0) and y + h <= _GetSystemMetrics(1)


def grab(rect: tuple[int, int, int, int]) -> Image.Image:
    """A picture of that screen area, RGB.

    Raises OSError when Windows won't hand over the screen (PC locked, a UAC
    prompt up): treat that as "can't see right now", never as fatal.
    """
    x, y, w, h = rect
    # Only the main screen unless Roblox sits elsewhere: grabbing every
    # monitor costs more and is rarely needed.
    return ImageGrab.grab(bbox=(x, y, x + w, y + h),
                          all_screens=not on_primary(rect)).convert("RGB")


# -------------------------------------------------------------- user input ---
def seconds_since_input() -> float:
    """Seconds since the last keyboard/mouse input of ANY kind, ours included.
    0 if Windows won't say, so an unknown answer never looks like 'away'."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not _GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    # Both are GetTickCount values, which wrap after 49.7 days.
    return ((_GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


# Input that lands this soon after one of ours is taken to BE ours. Windows
# only keeps the time of the latest input, so it can't tell them apart itself.
INJECT_GRACE = 1.5
# Our SendInput is stamped a moment BEFORE note_injected() runs (a held key is
# pressed 0.15 s before it is let go), so the window opens a little early.
INJECT_EARLY = 0.5

# Every clock alive, so the ALT tap in bring_to_front() can tell them all.
_clocks: "weakref.WeakSet[InputClock]" = weakref.WeakSet()


class InputClock:
    """Tells the user's input apart from the macro's own.

    inputs.py calls note_injected() after every SendInput. Each look at the
    last input time then sorts it: inside an injection window it was ours,
    otherwise it was the user's. `now` and `input_age` are swappable for tests.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic,
                 input_age: Callable[[], float] = seconds_since_input):
        self._now = now
        self._age = input_age
        self._lock = threading.Lock()
        self._injected: deque[float] = deque(maxlen=64)
        self._last_user: float | None = None
        # Until a real input has been seen, count the user as last active when
        # the clock was made: that is app start, when someone clicked it.
        self._born = now()
        _clocks.add(self)

    def note_injected(self) -> None:
        with self._lock:
            self._injected.append(self._now())

    def _ours(self, t: float) -> bool:
        return any(i - INJECT_EARLY <= t <= i + INJECT_GRACE for i in self._injected)

    def _sample(self) -> None:
        now = self._now()
        try:
            age = max(0.0, float(self._age()))
        except Exception:
            age = 0.0
        t = now - age
        if not self._ours(t) and (self._last_user is None or t > self._last_user):
            self._last_user = t

    def user_idle_seconds(self) -> float:
        """Seconds since the last input that was NOT the macro's own."""
        with self._lock:
            self._sample()
            last = self._born if self._last_user is None else self._last_user
            return max(0.0, self._now() - last)


def _note_injected_everywhere() -> None:
    for clock in list(_clocks):
        clock.note_injected()


# ------------------------------------------------------------------- focus ---
# What bring_to_front() changed, so restore_front() can undo exactly that.
_front_lock = threading.Lock()
_front = {"prev": 0, "roblox": 0, "was_min": False}


def _wait_front(hwnd: int, timeout: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while True:
        if _foreground() == hwnd:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _set_foreground(hwnd: int) -> bool:
    """SetForegroundWindow that works from a background program.

    Windows only lets the program that got the latest input change the front
    window, so tap ALT first. If that is not enough,
    join the input queues of both windows for a moment (AttachThreadInput).
    Never Alt+Tab: that can land on any window.
    """
    _keybd_event(VK_MENU, 0, 0, 0)
    _keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    _note_injected_everywhere()
    _SetForegroundWindow(hwnd)
    if _wait_front(hwnd, 0.25):
        return True
    me = _GetCurrentThreadId()
    attached: list[int] = []
    try:
        for other in {_GetWindowThreadProcessId(_foreground(), None),
                      _GetWindowThreadProcessId(hwnd, None)}:
            if other and other != me and _AttachThreadInput(me, other, True):
                attached.append(other)
        _BringWindowToTop(hwnd)
        _SetForegroundWindow(hwnd)
    finally:
        for other in attached:
            _AttachThreadInput(me, other, False)
    return _wait_front(hwnd)


def bring_to_front(hwnd: int) -> bool:
    """Put Roblox in front (restoring it if minimised) and remember the user's
    window for restore_front(). True when Roblox is in front afterwards."""
    guard("focus")
    if not hwnd or not _IsWindow(hwnd):
        return False
    with _front_lock:
        fg = _foreground()
        if fg == hwnd:                          # already there: nothing to undo later
            _front.update(prev=0, roblox=hwnd, was_min=False)
            return True
        was_min = bool(_IsIconic(hwnd))
        _front.update(prev=fg, roblox=hwnd, was_min=was_min)
        if was_min:
            _ShowWindow(hwnd, SW_RESTORE)
        return _set_foreground(hwnd)


def _restore_target(hwnd: int, prev: int, roblox: int, fg: int,
                    is_roblox: Callable[[int], bool]) -> int | None:
    """Which window restore_front() puts in front. None = leave everything
    alone (something other than Roblox is in front already: the user is back),
    0 = nothing to put back."""
    if fg and fg != roblox and not is_roblox(fg):
        return None
    # Called twice, or without bring_to_front(): a Roblox hwnd must never turn
    # into "put Roblox in front".
    if not hwnd or hwnd == roblox or is_roblox(hwnd):
        return prev
    return hwnd


def restore_front(hwnd: int) -> bool:
    """Put back the window that was in front before bring_to_front(hwnd).

    `hwnd` is the Roblox window that was raised (0 works too). Passing any
    other window puts THAT one in front instead. If the user has already
    clicked somewhere else, nothing is touched. Roblox goes back to minimised
    if it was. True when the user's window is in front afterwards.
    """
    guard("focus")
    with _front_lock:
        prev, roblox, was_min = _front["prev"], _front["roblox"], _front["was_min"]
        _front.update(prev=0, roblox=0, was_min=False)
        fg = _foreground()
        target = _restore_target(hwnd, prev, roblox, fg, _is_roblox)
        if target is None:
            return True                         # the user moved on by themselves: leave it
        ok = False
        if target and _IsWindow(target):
            ok = fg == target or _set_foreground(target)
        if was_min and roblox and _IsWindow(roblox) and _foreground() != roblox:
            _ShowWindow(roblox, SW_SHOWMINNOACTIVE)
        return ok
