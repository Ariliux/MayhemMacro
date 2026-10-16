"""window.py: only the parts that are safe to run on a real PC.

Allowed for real: roblox_hwnd() and roblox_running() (they only look), and
GetLastInputInfo. Never focus, move or capture a real window here. The focus
functions must refuse under MAYHEM_TESTING before touching anything, and
tripwires make sure of it.
"""
import pytest
from PIL import Image

from mayhem import window
from mayhem.safety import SideEffectBlocked
from mayhem.window import InputClock


class FakeTime:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeInput:
    """Plays GetLastInputInfo: the last input (anyone's) happened at `at`."""

    def __init__(self, clock: FakeTime, at: float):
        self.clock, self.at = clock, at

    def __call__(self) -> float:
        return self.clock.t - self.at


def make(start: float = 1000.0, last_input: float = 990.0):
    now = FakeTime(start)
    inp = FakeInput(now, last_input)
    return now, inp, InputClock(now=now, input_age=inp)


# --------------------------------------------------------------- InputClock ---
def test_user_input_counts():
    now, inp, clock = make(1000.0, 990.0)
    assert clock.user_idle_seconds() == pytest.approx(10.0)
    now.t = 1030.0
    assert clock.user_idle_seconds() == pytest.approx(40.0)
    inp.at = 1029.0                                # the user moved the mouse
    assert clock.user_idle_seconds() == pytest.approx(1.0)


def test_our_own_input_is_ignored():
    now, inp, clock = make(1000.0, 940.0)
    assert clock.user_idle_seconds() == pytest.approx(60.0)
    now.t = 1001.0
    clock.note_injected()                          # the macro jumped
    inp.at = 1000.85                               # stamped just before note_injected
    assert clock.user_idle_seconds() == pytest.approx(61.0)
    now.t = 1002.4
    inp.at = 1002.4                                # still within 1.5 s: ours
    assert clock.user_idle_seconds() == pytest.approx(62.4)


def test_input_after_the_grace_is_the_users():
    now, inp, clock = make(1000.0, 900.0)
    clock.note_injected()
    now.t = 1001.6
    inp.at = 1001.6                                # 1.6 s after ours: a real person
    assert clock.user_idle_seconds() == pytest.approx(0.0)
    now.t = 1010.0
    assert clock.user_idle_seconds() == pytest.approx(8.4)


def test_old_user_input_survives_many_injections():
    now, inp, clock = make(1000.0, 980.0)
    clock.user_idle_seconds()                      # the engine looks every tick
    for k in range(100):                           # a long walk of tiny mouse steps
        now.t = 1000.0 + k * 0.01
        clock.note_injected()
        inp.at = now.t
    now.t += 1.0
    assert clock.user_idle_seconds() == pytest.approx(now.t - 980.0)


def test_unknown_input_counts_from_creation():
    now = FakeTime(500.0)
    inp = FakeInput(now, 500.0)
    clock = InputClock(now=now, input_age=inp)
    clock.note_injected()                          # the only input so far is ours
    now.t = 530.0
    assert clock.user_idle_seconds() == pytest.approx(30.0)


def test_unreadable_input_time_means_user_is_here():
    now = FakeTime(100.0)

    def broken() -> float:
        raise OSError("no")

    clock = InputClock(now=now, input_age=broken)
    now.t = 400.0
    assert clock.user_idle_seconds() == 0.0


def test_injection_is_shared_with_every_clock():
    now1, inp1, c1 = make(1000.0, 900.0)
    now2, inp2, c2 = make(1000.0, 900.0)
    c1.user_idle_seconds(), c2.user_idle_seconds()
    window._note_injected_everywhere()             # what the ALT tap does
    for now, inp, c in ((now1, inp1, c1), (now2, inp2, c2)):
        now.t = inp.at = 1000.5
        assert c.user_idle_seconds() == pytest.approx(100.5)


def test_default_clock_needs_no_arguments():
    clock = InputClock()                           # how inputs.py makes it
    assert clock.user_idle_seconds() >= 0.0


# ----------------------------------------------------- read-only real calls ---
def test_seconds_since_input_is_a_number():
    assert window.seconds_since_input() >= 0.0


def test_roblox_lookups_are_consistent():
    h = window.roblox_hwnd()
    assert isinstance(h, int) and h >= 0
    running = window.roblox_running()
    assert isinstance(running, bool)
    if h:                                          # a Roblox window means a Roblox process
        assert running
    assert all(isinstance(p, int) for p in window.roblox_pids())


def test_no_window_answers():
    assert window.client_rect(0) is None
    assert window.is_foreground(0) is False
    assert window.is_minimized(0) is False


@pytest.mark.parametrize("cls, title, exe, want", [
    ("WINDOWSCLIENT", "Roblox", "robloxplayerbeta.exe", True),
    # a launcher renamed the window (the game's name, the server's region)
    ("WINDOWSCLIENT", "Block Mayhem X - US East", "robloxplayerbeta.exe", True),
    ("WINDOWSCLIENT", "", "robloxplayerbeta.exe", True),
    ("SomeOtherClass", "Roblox", "robloxplayerbeta.exe", True),
    ("Chrome_WidgetWin_1", "Roblox", "chrome.exe", False),           # a browser tab
    ("CabinetWClass", "Roblox", "explorer.exe", False),              # a folder
    ("WINDOWSCLIENT", "Roblox", "robloxstudiobeta.exe", False),
    ("SomeOtherClass", "Block Mayhem X", "robloxplayerbeta.exe", False),
])
def test_roblox_window_is_found_by_process_and_class_not_only_title(monkeypatch, cls, title,
                                                                    exe, want):
    monkeypatch.setattr(window, "_class", lambda h: cls)
    monkeypatch.setattr(window, "_title", lambda h: title)
    monkeypatch.setattr(window, "_exe_of_window", lambda h: exe)
    assert window._is_roblox(777) is want
    assert window._is_roblox(0) is False


def test_several_roblox_windows_front_one_first_then_the_titled_one():
    titles = {1: "Block Mayhem X", 2: "Roblox", 3: "Roblox"}
    assert window._pick_roblox([1, 2, 3], 3, titles.get) == 3
    assert window._pick_roblox([1, 2, 3], 99, titles.get) == 2
    assert window._pick_roblox([1], 99, titles.get) == 1
    assert window._pick_roblox([], 1, titles.get) == 0


@pytest.mark.parametrize("state, fg_roblox, want", [
    (5, False, False),       # QUNS_ACCEPTS_NOTIFICATIONS: a normal desktop
    (1, False, False),       # QUNS_NOT_PRESENT: locked or screen saver, the user is away
    (2, False, True),        # QUNS_BUSY: a full-screen app
    (3, False, True),        # QUNS_RUNNING_D3D_FULL_SCREEN: a full-screen game or video
    (4, False, True),        # QUNS_PRESENTATION_MODE
    (3, True, False),        # ...but that full-screen window is Roblox itself
])
def test_fullscreen_app_in_front(monkeypatch, state, fg_roblox, want):
    def query(ptr):
        ptr._obj.value = state
        return 0
    monkeypatch.setattr(window, "_SHQueryUserNotificationState", query)
    monkeypatch.setattr(window, "_foreground", lambda: 4242)
    monkeypatch.setattr(window, "_is_roblox", lambda h: fg_roblox)
    assert window.fullscreen_app_in_front() is want


def test_fullscreen_check_failing_says_no(monkeypatch):
    monkeypatch.setattr(window, "_SHQueryUserNotificationState", lambda ptr: -2147467259)
    assert window.fullscreen_app_in_front() is False

    def broken(ptr):
        raise OSError("no shell")
    monkeypatch.setattr(window, "_SHQueryUserNotificationState", broken)
    assert window.fullscreen_app_in_front() is False


def test_fullscreen_check_for_real_only_looks():
    assert isinstance(window.fullscreen_app_in_front(), bool)


# -------------------------------------------------------------- math helpers ---
def test_to_screen_adds_the_client_origin():
    rect = (0, 23, 1920, 1009)
    assert window.to_screen(rect, 0, 0) == (0, 23)
    assert window.to_screen(rect, 100.4, 200.6) == (100, 224)
    assert window.to_screen((-1920, 10, 1920, 1080), 960, 540) == (-960, 550)


def test_on_primary(monkeypatch):
    monkeypatch.setattr(window, "_GetSystemMetrics", lambda i: (1920, 1080)[i])
    assert window.on_primary((0, 23, 1920, 1009))
    assert not window.on_primary((-1920, 0, 1920, 1080))
    assert not window.on_primary((100, 100, 1920, 1009))


def test_grab_asks_for_exactly_the_client(monkeypatch):
    asked = {}

    def fake_grab(bbox=None, all_screens=False, **_):
        asked.update(bbox=bbox, all_screens=all_screens)
        return Image.new("RGBA", (bbox[2] - bbox[0], bbox[3] - bbox[1]))

    monkeypatch.setattr(window.ImageGrab, "grab", fake_grab)
    monkeypatch.setattr(window, "_GetSystemMetrics", lambda i: (1920, 1080)[i])
    img = window.grab((0, 23, 1920, 1009))
    assert asked == {"bbox": (0, 23, 1920, 1032), "all_screens": False}
    assert img.mode == "RGB" and img.size == (1920, 1009)
    window.grab((-1920, 0, 1920, 1080))            # on another monitor
    assert asked["all_screens"] is True


# -------------------------------------------------------------------- focus ---
R, USER, OTHER = 111, 222, 333          # made-up handles: Roblox, the user's window, another


def _roblox_only(h: int) -> bool:
    return h == R


@pytest.mark.parametrize("hwnd, prev, roblox, fg, want", [
    (R, USER, R, R, USER),          # the normal case: Roblox raised, put the user's window back
    (0, USER, R, R, USER),          # 0 means "the one you raised"
    (R, 0, R, R, 0),                # Roblox was already in front: nothing to put back
    (R, 0, 0, R, 0),                # called twice: never "put Roblox in front"
    (R, USER, R, OTHER, None),      # the user clicked something else meanwhile: leave it
    (OTHER, USER, R, R, OTHER),     # an explicit other window goes in front instead
    (R, USER, R, 0, USER),          # nothing in front for a moment: still restore
])
def test_restore_target(hwnd, prev, roblox, fg, want):
    assert window._restore_target(hwnd, prev, roblox, fg, _roblox_only) == want


@pytest.fixture
def tripwires(monkeypatch):
    """Fail loudly if anything that changes focus or sends a key is reached."""
    def boom(*_a, **_k):
        raise AssertionError("a real focus change was attempted in a test")

    for name in ("_SetForegroundWindow", "_BringWindowToTop", "_ShowWindow",
                 "_AttachThreadInput", "_keybd_event", "_set_foreground"):
        monkeypatch.setattr(window, name, boom)


@pytest.mark.parametrize("fn", [window.bring_to_front, window.restore_front])
def test_focus_is_guarded(fn, tripwires):
    with pytest.raises(SideEffectBlocked, match="focus"):
        fn(12345)
    with pytest.raises(SideEffectBlocked):
        fn(0)
