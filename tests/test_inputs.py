"""inputs.py: always guarded, and the move maths, without ever sending real input.

Nothing here can reach the real SendInput: every test that gets past the guard
first swaps the module's whole user32 handle for FakeUser32, and the shared
ctypes.windll.user32 SendInput is replaced by a trap that fails the test.
"""
import ctypes
import inspect
import sys
import types

import pytest

from mayhem import inputs
from mayhem.safety import SideEffectBlocked

ROBLOX = 0x1234
OTHER = 0x9999


class FakeUser32:
    """Stands in for user32. Records every call; moves a pretend cursor."""

    def __init__(self, fg: int = ROBLOX, cursor=(0, 0), gain: float = 1.0,
                 locked: bool = False, log: list | None = None):
        self.calls: list[str] = []
        self.sent: list[tuple] = []
        self.fg = fg
        self.cursor = list(cursor)
        self.gain = gain
        self.locked = locked
        self.log = log if log is not None else []
        self.switch_after: int | None = None      # foreground changes after N sends

    def SendInput(self, n, pinp, cb):
        self.calls.append("SendInput")
        inp = pinp._obj
        if inp.type == inputs.INPUT_KEYBOARD:
            k = inp.u.ki
            self.sent.append(("key", n, cb, k.wVk, k.wScan, k.dwFlags))
        else:
            m = inp.u.mi
            self.sent.append(("mouse", n, cb, m.dx, m.dy, m.dwFlags))
            if m.dwFlags & inputs.MOUSEEVENTF_MOVE and not self.locked:
                self.cursor[0] = min(1919, max(0, self.cursor[0] + round(m.dx * self.gain)))
                self.cursor[1] = min(1079, max(0, self.cursor[1] + round(m.dy * self.gain)))
        self.log.append("send")
        if self.switch_after is not None and len(self.sent) >= self.switch_after:
            self.fg = OTHER
        return 1

    def GetCursorPos(self, pp):
        self.calls.append("GetCursorPos")
        pp._obj.x, pp._obj.y = self.cursor
        return 1

    def GetForegroundWindow(self):
        self.calls.append("GetForegroundWindow")
        return self.fg

    def __getattr__(self, name):          # anything else would be a surprise
        raise AssertionError(f"unexpected user32 call: {name}")


class FakeClock:
    def __init__(self, log: list):
        self.notes = 0
        self.log = log

    def note_injected(self) -> None:
        self.notes += 1
        self.log.append("note")


@pytest.fixture
def sandbox(monkeypatch):
    """Fake user32, clock, Roblox lookup and sleep; trap the real SendInput."""
    log: list = []
    fake = FakeUser32(log=log)
    clock = FakeClock(log)
    real_calls: list = []
    lookups: list = []
    sleeps: list = []

    def trap(*a, **k):
        real_calls.append(a)
        raise AssertionError("the real SendInput was called")

    for name in ("SendInput", "keybd_event", "mouse_event", "SetCursorPos"):
        monkeypatch.setattr(ctypes.windll.user32, name, trap)
    monkeypatch.setattr(inputs, "_user32", fake)
    monkeypatch.setitem(inputs.__dict__, "clock", clock)

    def roblox_hwnd():
        lookups.append(1)
        return ROBLOX
    monkeypatch.setattr(inputs, "_roblox_hwnd", roblox_hwnd)

    def sleep(s):
        sleeps.append(s)
        log.append(("sleep", s))
    monkeypatch.setattr(inputs, "time", types.SimpleNamespace(sleep=sleep))
    return types.SimpleNamespace(user32=fake, clock=clock, real=real_calls,
                                 lookups=lookups, sleeps=sleeps, log=log)


@pytest.fixture
def unguarded(sandbox, monkeypatch):
    """Lets the code past the guard. Only ever on top of `sandbox`."""
    monkeypatch.setattr(inputs, "safety", types.SimpleNamespace(guard=lambda what: None))
    return sandbox


def _public_functions() -> dict:
    return {n: f for n, f in vars(inputs).items()
            if inspect.isfunction(f) and f.__module__ == inputs.__name__
            and not n.startswith("_")}


def _dummy_call(fn):
    args = [10 for p in inspect.signature(fn).parameters.values()
            if p.default is inspect.Parameter.empty]
    return fn(*args)


# ------------------------------------------------------------ structures ---
def test_input_struct_is_the_real_windows_size():
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "these sizes are for 64-bit Python"
    assert ctypes.sizeof(inputs.INPUT) == 40
    assert inputs.INPUT_SIZE == 40
    assert ctypes.sizeof(inputs.MOUSEINPUT) == 32
    assert ctypes.sizeof(inputs.KEYBDINPUT) == 24
    assert ctypes.sizeof(inputs.HARDWAREINPUT) == 8
    assert {f[0] for f in inputs._UNION._fields_} == {"mi", "ki", "hi"}


def test_own_user32_handle_not_the_shared_one():
    # argtypes are set on it, which must never leak into ctypes.windll.user32
    assert inputs._user32 is not ctypes.windll.user32


# ----------------------------------------------------------------- guard ---
def test_public_functions_are_the_contract():
    assert set(_public_functions()) == {"press_space", "tap_scan", "move_to", "click_at"}


@pytest.mark.parametrize("name", ["press_space", "tap_scan", "move_to", "click_at"])
def test_every_public_function_is_blocked_while_testing(sandbox, name):
    fn = _public_functions()[name]
    with pytest.raises(SideEffectBlocked):
        _dummy_call(fn)
    # guard() ran before ANYTHING else: no SendInput, not even a cursor read,
    # no Roblox lookup, nothing noted on the clock, no sleep
    assert sandbox.user32.calls == []
    assert sandbox.real == []
    assert sandbox.lookups == []
    assert sandbox.clock.notes == 0
    assert sandbox.sleeps == []


def test_guard_names_sendinput(sandbox, monkeypatch):
    seen: list[str] = []

    def guard(what):
        seen.append(what)
        raise SideEffectBlocked(what)
    monkeypatch.setattr(inputs, "safety", types.SimpleNamespace(guard=guard))
    for fn in _public_functions().values():
        with pytest.raises(SideEffectBlocked):
            _dummy_call(fn)
    assert seen == ["SendInput"] * 4
    assert sandbox.user32.calls == []


def test_explicit_calls_blocked_and_nothing_sent(sandbox):
    for call in (lambda: inputs.press_space(), lambda: inputs.press_space(0.5),
                 lambda: inputs.tap_scan(inputs.SCAN_SPACE), lambda: inputs.move_to(960, 540),
                 lambda: inputs.click_at(960, 540)):
        with pytest.raises(SideEffectBlocked):
            call()
    assert "SendInput" not in sandbox.user32.calls
    assert sandbox.real == []


# ------------------------------------------------------------ step maths ---
@pytest.mark.parametrize("dx, dy, want", [
    (0, 0, (0, 0)), (1, -1, (0, 0)), (-1, 1, (0, 0)),       # close enough
    (2, 0, (2, 0)), (0, -2, (0, -2)),                        # last pixels exactly
    (10, 0, (3, 0)), (-30, 5, (-3, 3)),                      # <= 30 away: 3 px
    (31, 0, (12, 0)), (120, -7, (12, -7)),                   # <= 120 away: 12 px
    (121, 0, (40, 0)), (-500, 300, (-40, 40)), (1919, 1079, (40, 40)),
    (500, 2, (40, 2)),                                       # each axis clamped alone
])
def test_step(dx, dy, want):
    assert inputs._step(dx, dy) == want


class Cursor:
    """A pretend cursor for _walk: gain < 1 = slow pointer, > 1 = acceleration."""

    def __init__(self, x: int, y: int, gain: float = 1.0, locked: bool = False):
        self.x, self.y = x, y
        self.gain, self.locked = gain, locked
        self.moves: list[tuple[int, int]] = []
        self.sleeps: list[float] = []

    def read(self) -> tuple[int, int]:
        return self.x, self.y

    def move(self, dx: int, dy: int) -> bool:
        self.moves.append((dx, dy))
        if not self.locked:
            self.x = min(1919, max(0, self.x + round(dx * self.gain)))
            self.y = min(1079, max(0, self.y + round(dy * self.gain)))
        return True


def _walk(c: Cursor, x: int, y: int, tries: int = inputs.MOVE_TRIES) -> bool:
    return inputs._walk(x, y, c.read, c.move, c.sleeps.append, tries)


def test_walk_already_there_sends_nothing():
    c = Cursor(500, 500)
    assert _walk(c, 501, 499)
    assert c.moves == []


def test_walk_across_the_whole_screen():
    c = Cursor(0, 0)
    assert _walk(c, 1919, 1079)
    assert abs(c.x - 1919) <= 1 and abs(c.y - 1079) <= 1
    assert all(abs(dx) <= 40 and abs(dy) <= 40 for dx, dy in c.moves)
    assert c.sleeps == [0.010] * len(c.moves)


def test_walk_goes_left_and_up_with_negative_steps():
    c = Cursor(1800, 1000)
    assert _walk(c, 100, 50)
    assert all(dx <= 0 and dy <= 0 for dx, dy in c.moves)


def test_walk_slow_pointer_speed_still_arrives():
    # half speed: 40 asked = 20 travelled. GlassMacro's 60 steps ran out here.
    c = Cursor(0, 540, gain=0.5)
    assert _walk(c, 1900, 540)
    assert not _walk(Cursor(0, 540, gain=0.5), 1900, 540, tries=60)


def test_walk_with_acceleration_overshoot_settles():
    c = Cursor(0, 0, gain=2.0)
    assert _walk(c, 777, 333)
    assert abs(c.x - 777) <= 3 and abs(c.y - 333) <= 3


def test_walk_gives_up_fast_when_the_mouse_is_locked():
    # Roblox in first person / shift-lock: the cursor never moves, and every
    # step would turn the camera. Give up after a few, not after 150.
    c = Cursor(960, 540, locked=True)
    assert not _walk(c, 100, 100)
    assert len(c.moves) <= inputs.STUCK_STEPS + 1


def test_walk_stops_when_roblox_loses_focus():
    c = Cursor(0, 0)
    calls: list = []

    def move(dx, dy):
        calls.append((dx, dy))
        return len(calls) < 3          # the user clicked away after two steps
    assert not inputs._walk(900, 500, c.read, move, c.sleeps.append, inputs.MOVE_TRIES)
    assert len(calls) == 3


# ------------------------------------- what gets sent (all into fakes) ---
def test_press_space_is_a_held_scan_code(unguarded):
    assert inputs.press_space() is True
    f = unguarded.user32
    assert f.sent == [("key", 1, 40, 0, 0x39, inputs.KEYEVENTF_SCANCODE),
                      ("key", 1, 40, 0, 0x39, inputs.KEYEVENTF_SCANCODE | inputs.KEYEVENTF_KEYUP)]
    # down, noted, held 0.15 s, up, noted
    assert unguarded.log == ["send", "note", ("sleep", 0.15), "send", "note"]
    assert unguarded.clock.notes == 2
    assert unguarded.real == []


def test_press_space_hold_is_capped(unguarded):
    inputs.press_space(hold=30)
    assert unguarded.sleeps == [inputs.MAX_HOLD]


def test_tap_scan_short_hold(unguarded):
    assert inputs.tap_scan(0x11) is True
    assert [s[4] for s in unguarded.user32.sent] == [0x11, 0x11]
    assert unguarded.sleeps == [0.03]


def test_key_always_comes_back_up(unguarded, monkeypatch):
    def boom(s):
        raise RuntimeError("stopped mid-press")
    monkeypatch.setattr(inputs, "time", types.SimpleNamespace(sleep=boom))
    with pytest.raises(RuntimeError):
        inputs.press_space()
    flags = [s[5] for s in unguarded.user32.sent]
    assert flags[-1] & inputs.KEYEVENTF_KEYUP


def test_nothing_sent_when_roblox_is_not_in_front(unguarded, monkeypatch):
    unguarded.user32.fg = OTHER
    assert inputs.press_space() is False
    assert inputs.tap_scan(0x39) is False
    assert inputs.move_to(10, 10) is False
    assert inputs.click_at(10, 10) is False
    monkeypatch.setattr(inputs, "_roblox_hwnd", lambda: 0)      # no Roblox at all
    unguarded.user32.fg = ROBLOX
    assert inputs.press_space() is False
    assert "SendInput" not in unguarded.user32.calls
    assert unguarded.clock.notes == 0


def test_window_handles_compare_signed_or_unsigned():
    assert inputs._same_hwnd(0xFFFF8001, -32767)
    assert not inputs._same_hwnd(0, 0)
    assert not inputs._same_hwnd(None, 5)


def test_move_to_sends_only_relative_moves(unguarded):
    unguarded.user32.cursor = [100, 900]
    assert inputs.move_to(960, 540) is True
    sent = unguarded.user32.sent
    assert sent and all(s[0] == "mouse" and s[5] == inputs.MOUSEEVENTF_MOVE for s in sent)
    assert not any(s[5] & inputs.MOUSEEVENTF_ABSOLUTE for s in sent)
    assert unguarded.clock.notes == len(sent)              # noted after EVERY SendInput


def test_click_at_moves_then_clicks_in_place(unguarded):
    unguarded.user32.cursor = [1500, 200]
    assert inputs.click_at(700, 800) is True
    sent = unguarded.user32.sent
    moves, buttons = sent[:-2], sent[-2:]
    assert all(s[5] == inputs.MOUSEEVENTF_MOVE for s in moves)
    assert buttons == [("mouse", 1, 40, 0, 0, inputs.MOUSEEVENTF_LEFTDOWN),
                       ("mouse", 1, 40, 0, 0, inputs.MOUSEEVENTF_LEFTUP)]
    assert unguarded.user32.cursor == [700, 800]
    assert inputs.MOVE_SETTLE in unguarded.sleeps


def test_click_at_does_not_click_when_the_mouse_is_locked(unguarded):
    unguarded.user32.locked = True
    unguarded.user32.cursor = [960, 540]
    assert inputs.click_at(200, 200) is False
    assert not any(s[5] & (inputs.MOUSEEVENTF_LEFTDOWN | inputs.MOUSEEVENTF_LEFTUP)
                   for s in unguarded.user32.sent)


def test_click_at_does_not_click_after_the_user_switches_window(unguarded):
    unguarded.user32.cursor = [0, 0]
    unguarded.user32.switch_after = 5          # alt-tab half way there
    assert inputs.click_at(1200, 900) is False
    assert len(unguarded.user32.sent) == 5
    assert not any(s[5] & inputs.MOUSEEVENTF_LEFTDOWN for s in unguarded.user32.sent)


# ------------------------------------------------------------- the clock ---
def test_clock_is_made_once_from_window_input_clock(monkeypatch):
    made: list = []

    class StubClock:
        def __init__(self):
            made.append(self)

        def note_injected(self) -> None:
            pass

    stub = types.ModuleType("mayhem.window")
    stub.InputClock = StubClock
    monkeypatch.setitem(sys.modules, "mayhem.window", stub)
    monkeypatch.delitem(inputs.__dict__, "clock", raising=False)
    try:
        first = inputs.clock
        assert isinstance(first, StubClock)
        assert inputs.clock is first and inputs._clock() is first
        assert len(made) == 1
    finally:
        inputs.__dict__.pop("clock", None)     # don't leave the stub behind


def test_unknown_module_attribute_still_raises():
    with pytest.raises(AttributeError):
        inputs.not_a_thing
