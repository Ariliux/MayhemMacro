"""The run loop, driven tick by tick with fakes for everything that has a side effect.

No test here sends a key, moves the mouse, changes focus, kills or opens
Roblox, or touches the network: the window, inputs, rejoin, log tail, clock
and sleep are all fakes, and time only moves when a test moves it. The fake
OCR answers by WHICH crop it is handed (each HUD region has its own size), so
a test scripts "the timer says 00:12" or "the vote screen shows Void and Lava"
directly. A few tests at the end run the real Windows OCR on the real fixture
frames (they skip when the frames or an OCR language are missing).
"""
import ast
import copy
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import replace

import pytest
from PIL import Image

from mayhem import PLACE_ID, engine as engine_mod, hud, paths, rejoin
from mayhem.engine import Deps, Engine
from mayhem.ocr import Ocr, OcrLine, OcrWord
from mayhem.robloxlog import LogTail, parse_line

FRAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "frames")
CLIENT_RECT = (0, 23, 1920, 1009)           # the client inside the 1920x1080 fixture frames
CLIENT = CLIENT_RECT[2:]
OTHER_GAME = "15532962292"
PRIVATE = "https://www.roblox.com/games/75270037454060/Block-Mayhem-X?privateServerLinkCode=12345678"
PRIVATE_URI = f"roblox://placeID={PLACE_ID}&linkCode=12345678"
SHARE = "https://www.roblox.com/share?code=abc123def456&type=Server"
SHARE_URI = "roblox://navigation/share_links?code=abc123def456&type=Server"
RAW = f"roblox://placeId={PLACE_ID}&linkCode=12345678"     # tried as saved, then PRIVATE_URI
NEW_LOG = "C:\\fake\\logs\\0.742_20261009T020000Z_Player_B{}_last.log"


# ------------------------------------------------------------- log lines ---
def line(msg: str, chan: str = "FLog::Output") -> str:
    return f"2026-10-09T02:00:00.000Z,100.000000,2f98,6 [{chan}] {msg}"


def JOIN(place: str = PLACE_ID) -> str:
    return line(f"! Joining game 'abc' place {place} at 10.0.0.1")


ENTERED = line("Entered play session.", "FLog::SessionTransitionFSM")
LEFT = line("Tearing down.", "FLog::SessionTransitionFSM")
LOADED = line("\u2705 | CLIENT: GuiController module loaded in 0.07653s!", "FLog::CreatorOutput")


def DC(reason: int = 285) -> str:
    return line(f"Sending disconnect with reason: {reason}", "FLog::Network")


def SONG(sound: str) -> str:
    return line(f"Warning: Could not find song group for sound {sound}", "FLog::CreatorWarning")


def CHROMA(block: str) -> str:
    return line(f"[Chromatic] Thread stopped for block: {block}_chromatic after 10000 iterations",
                "FLog::CreatorOutput")


def IDLE(secs: float) -> str:
    return line(f"Player has been idle for {secs} seconds", "FLog::CreatorOutput")


def mmss(secs: int) -> str:
    return f"{secs // 60:02d}:{secs % 60:02d}"


# ------------------------------------------------------------------ fakes ---
class FakeClock:
    """deps.now, deps.sleep and the InputClock in one. The user was last at
    the PC an hour ago unless a test says otherwise."""

    def __init__(self):
        self.t = 10_000.0
        self.last_input = self.t - 3600
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs

    def user_idle_seconds(self) -> float:
        return max(0.0, self.t - self.last_input)

    def note_injected(self) -> None:
        pass

    def touch(self) -> None:
        """The user moves the mouse right now."""
        self.last_input = self.t


class FakeWindow:
    def __init__(self, order: list):
        self.order = order
        self.hwnd = 100
        self.fg = True
        self.minimized = False
        self.rect = CLIENT_RECT
        self.frame: Image.Image | None = None      # a full 1920x1080 screen, for real OCR
        self._blank = Image.new("RGB", CLIENT)
        self.grabs = 0
        self.looks = 0
        self.grab_error: Exception | None = None
        self.hwnd_error: Exception | None = None
        self.front_ok = True
        self.fullscreen = False                    # a full-screen video or game in front

    def fullscreen_app_in_front(self) -> bool:
        return self.fullscreen

    def roblox_hwnd(self) -> int:
        self.looks += 1
        if self.hwnd_error:
            raise self.hwnd_error
        return self.hwnd

    def roblox_running(self) -> bool:
        return bool(self.hwnd)

    def is_foreground(self, h: int) -> bool:
        return bool(h) and h == self.hwnd and self.fg

    def is_minimized(self, h: int) -> bool:
        return bool(h) and self.minimized

    def client_rect(self, h: int):
        return None if not h or self.minimized else self.rect

    def grab(self, rect):
        self.grabs += 1
        if self.grab_error:
            raise self.grab_error
        if self.frame is not None:
            x, y, w, h = rect
            return self.frame.crop((x, y, x + w, y + h))
        return self._blank

    def bring_to_front(self, h: int) -> bool:
        self.order.append("front")
        if self.front_ok:
            self.fg = True
        return self.front_ok

    def restore_front(self, h: int) -> bool:
        self.order.append("restore")
        self.fg = False
        return True


def _region_size(name: str) -> tuple[int, int]:
    w, h = CLIENT
    x0, y0, x1, y1 = hud.REGIONS[name]
    return round(x1 * w) - round(x0 * w), round(y1 * h) - round(y0 * h)


_SIZES = {_region_size(name): name for name in hud.REGIONS}
_SIZES[CLIENT] = "client"
assert len(_SIZES) == len(hud.REGIONS) + 1        # every crop has its own size


class FakeOcr:
    """Answers by which crop it is shown: script[region] is a list of lines
    (str or OcrLine) or a callable returning one."""
    available = True
    error = ""

    def __init__(self):
        self.script: dict = {}
        self.reads: list[str] = []
        self.fail: Exception | None = None

    def read(self, img: Image.Image, scale: float = 2.0) -> list[OcrLine]:
        region = _SIZES.get(img.size, "?")
        self.reads.append(region)
        if self.fail:
            raise self.fail
        got = self.script.get(region, [])
        if callable(got):
            got = got()
        return [x if isinstance(x, OcrLine) else OcrLine(x, ()) for x in got]


class FakeInputs:
    def __init__(self, window: FakeWindow, order: list):
        self.window = window
        self.order = order
        self.calls: list[tuple] = []
        self.ok = True
        self.space_error: Exception | None = None

    def press_space(self, hold: float = 0.15) -> bool:
        self.calls.append(("space",))
        self.order.append("space")
        if self.space_error:
            raise self.space_error
        return self.ok and self.window.fg          # like the real one: only into Roblox

    def tap_scan(self, scan: int, hold: float = 0.03) -> bool:
        self.calls.append(("scan", scan))
        self.order.append("scan")
        return self.ok and self.window.fg

    def click_at(self, x: int, y: int, settle: float = 0.05) -> bool:
        self.calls.append(("click", x, y))
        self.order.append("click")
        return self.ok and self.window.fg

    def move_to(self, x: int, y: int) -> bool:
        self.calls.append(("move", x, y))
        return True

    def spaces(self) -> list:
        return [c for c in self.calls if c[0] == "space"]

    def clicks(self) -> list:
        return [c for c in self.calls if c[0] == "click"]


class FakeRejoin:
    """Real link parsing (it's pure), recorded kill/launch."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[tuple] = []
        self.launch_error: Exception | None = None
        self.on_kill = None

    def deep_links(self, link: str) -> list[str]:
        return rejoin.deep_links(link)

    def validate_link(self, link: str):
        return rejoin.validate_link(link)

    def kill_roblox(self) -> int:
        self.calls.append(("kill", self.clock.t))
        if self.on_kill:
            self.on_kill()
        return 1

    def launch(self, uri: str) -> None:
        self.calls.append(("launch", self.clock.t, uri))
        if self.launch_error:
            raise self.launch_error

    def launches(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "launch"]

    def launch_times(self) -> list[float]:
        return [c[1] for c in self.calls if c[0] == "launch"]


class FakeTail(LogTail):
    """The real LogTail state rules, fed with lines instead of a file."""

    def __init__(self):
        super().__init__(logdir=os.path.join(tempfile.gettempdir(), "mayhem-no-such-logs"))
        self.path = "C:\\fake\\logs\\0.742_20261009T010000Z_Player_A_last.log"
        self.queue: list = []
        self.primes = 0
        self.poll_error: Exception | None = None

    def prime(self) -> None:
        self.primes += 1
        self._primed = True

    def poll(self):
        if self.poll_error:
            raise self.poll_error
        out = []
        queue, self.queue = self.queue, []
        for item in queue:
            if isinstance(item, str):           # Roblox started a new log file
                self.path, self._pos = item, 0
                self._reset()
                continue
            ev = replace(item, log=os.path.basename(self.path))
            self._apply(ev)
            out.append(ev)
        return out

    def feed(self, *lines: str) -> None:
        for ln in lines:
            ev = parse_line(ln)
            assert ev is not None, ln
            self.queue.append(ev)

    def switch(self, path: str) -> None:
        self.queue.append(path)

    def in_game(self) -> None:
        self.place, self.entered, self.loaded = PLACE_ID, True, True


class Rig:
    """An Engine wired to fakes. Anti-AFK is off unless a test turns it on:
    with the user away for an hour its fallback would jump on every first tick."""

    def __init__(self, **settings):
        self.order: list[str] = []
        self.clock = FakeClock()
        self.window = FakeWindow(self.order)
        self.ocr = FakeOcr()
        self.inputs = FakeInputs(self.window, self.order)
        self.rejoin = FakeRejoin(self.clock)
        self.tail = FakeTail()
        self.tail.in_game()
        self.pictures: list[tuple[Image.Image, str]] = []
        self.settings = copy.deepcopy(paths.DEFAULT_SETTINGS)
        self.settings["anti_afk"] = False
        self.settings["afk_variety"] = False     # plain jumps unless a test asks
        self.settings["open_pinned"] = False     # no start-of-run setup unless a test asks
        self.settings["center_cursor"] = False
        self.settings["start_jump"] = False
        self.settings["close_esc_menu"] = False   # no Esc presses unless a test asks
        self.settings["front_hourly"] = False     # no window raising unless a test asks
        self.settings.update(settings)
        self.events: list[dict] = []
        self.deps = Deps(window=self.window, ocr=self.ocr, inputs=self.inputs,
                         rejoin=self.rejoin, tail=self.tail, clock=self.clock,
                         now=self.clock.now, sleep=self.clock.sleep, save_picture=self.save)
        self.engine = Engine(lambda: self.settings, self.events.append, self.deps)

    def save(self, img: Image.Image, tag: str) -> str:
        self.pictures.append((img, tag))
        return f"{tag}.jpg"

    def start(self) -> "Rig":
        self.engine.start(thread=False)
        return self

    def tick(self, n: int = 1, dt: float = engine_mod.TICK) -> None:
        for _ in range(n):
            self.clock.t += dt
            self.engine.tick()

    def run(self, secs: float) -> None:
        self.tick(int(round(secs / engine_mod.TICK)))

    def run_until(self, cond, limit: float = 4000.0) -> None:
        end = self.clock.t + limit
        while not cond():
            assert self.clock.t < end, "timed out"
            self.tick()

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]

    def logs(self, level: str | None = None) -> list[str]:
        return [e["text"] for e in self.of("log") if level is None or e["level"] == level]

    def states(self) -> list[tuple[str, str]]:
        return [(e["state"], e["why"]) for e in self.of("state")]

    def rejoins(self) -> list[tuple[str, int]]:
        return [(e["phase"], e["try"]) for e in self.of("rejoin")]


def vote_lines(*names: str, y: float = 480, votes: dict | None = None,
               countdown: int | None = None) -> list[OcrLine]:
    """Vote cards in one row, 600 px apart, like the real screen: the map name
    (centre x = 380 + 600 * i, centre y = y + 20), its VOTE button and its
    'N Votes' count under it, and the countdown above the cards."""
    out = []
    if countdown is not None:
        out.append(OcrLine(str(countdown), (OcrWord(str(countdown), 942, 235, 30, 32),)))
    for i, n in enumerate(names):
        x = 300 + 600 * i
        k = (votes or {}).get(n, 0)
        count = ("O" if k == 0 else str(k), "Vote" if k == 1 else "Votes")
        out += [OcrLine(n, (OcrWord(n, x, y, 160, 40),)),
                OcrLine("OTE", (OcrWord("OTE", x + 40, y + 50, 80, 50),)),
                OcrLine(" ".join(count), (OcrWord(count[0], x + 50, y + 130, 12, 14),
                                          OcrWord(count[1], x + 66, y + 130, 50, 14)))]
    return out


class VoteScreen:
    """A vote that shows up when the timer runs out, its countdown ticking.
    With react=True a click on a card adds a vote to that card's count."""

    def __init__(self, r: Rig, names: tuple[str, ...], in_secs: float, last: float = 20.0,
                 react: bool = True):
        self.r, self.names, self.react = r, names, react
        self.at = r.clock.t + in_secs
        self.until = self.at + last
        r.ocr.script["timer"] = self.timer
        r.ocr.script["client"] = self.client

    def up(self) -> bool:
        return self.at <= self.r.clock.t < self.until

    def timer(self) -> list[str]:
        left = self.at - self.r.clock.t
        return [mmss(math.ceil(left))] if left > 0 else []     # covered once the vote is up

    def client(self) -> list[OcrLine]:
        if not self.up():
            return []
        votes: dict[str, int] = {}
        if self.react:
            rx = self.r.window.rect[0]
            for _, x, _y in self.r.inputs.clicks():
                for i, n in enumerate(self.names):
                    if 300 + 600 * i <= x - rx <= 460 + 600 * i:
                        votes[n] = 1
        return vote_lines(*self.names, votes=votes,
                          countdown=max(0, math.ceil(self.until - self.r.clock.t)))


# ------------------------------------------------------------ run control ---
def test_start_primes_the_tail_and_reports_running():
    r = Rig().start()
    assert r.tail.primes == 1
    assert r.states() == [("running", "Starting")]
    r.tick()
    assert r.states()[-1] == ("running", "In Block Mayhem X")
    assert r.engine.running


def test_tick_before_start_does_nothing():
    r = Rig()
    r.tick(3)
    assert r.events == [] and r.window.looks == 0


def test_stop_reports_stopped_once_and_later_ticks_do_nothing():
    r = Rig().start()
    r.tick()
    r.engine.stop()
    r.engine.stop()
    assert r.states()[-1] == ("stopped", "Stopped")
    assert [s for s in r.states() if s[0] == "stopped"] == [("stopped", "Stopped")]
    n, looks = len(r.events), r.window.looks
    r.tick(5)
    assert len(r.events) == n and r.window.looks == looks
    assert not r.engine.running


def test_worker_thread_ticks_until_stop_and_can_start_again():
    r = Rig()
    r.engine.start()
    t = r.engine._thread
    assert t is not None and t.daemon and t.name == "MayhemMacro engine"
    deadline = time.monotonic() + 5
    while r.window.looks < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    r.engine.stop()
    assert r.window.looks >= 2
    assert not t.is_alive()
    # A second run on the same engine starts fresh.
    r.engine.start()
    assert r.engine._thread is not t and r.engine.running
    r.engine.stop()
    assert not r.engine._thread.is_alive()
    assert [s for s, _ in r.states()].count("stopped") == 2


def test_states_follow_the_window():
    r = Rig().start()
    r.tick()
    r.window.fg = False
    r.tick()
    r.window.minimized = True
    r.tick()
    r.window.hwnd = 0
    r.tick()
    assert r.states() == [("running", "Starting"), ("running", "In Block Mayhem X"),
                          ("paused", "Roblox is behind other windows"),
                          ("paused", "Roblox is minimised"),
                          ("waiting", "Roblox isn't open")]


def test_joining_another_game_mid_run_stops_the_run():
    # The macro only launches Block Mayhem X, so another game was the user's
    # choice: stop instead of killing it later for a rejoin.
    r = Rig().start()
    r.tail.feed(JOIN(OTHER_GAME), ENTERED)
    r.tick()
    assert not r.engine.running
    assert r.states()[-1] == ("stopped", "You joined another game, so I stopped.")
    r.run(120)
    assert r.rejoin.calls == []


def test_a_run_started_while_roblox_is_in_another_game_never_closes_it():
    # F1 is also another macro's Start key: pressed for that one while Roblox
    # is in some other game, this run must stop, not kill that game later.
    r = Rig()
    r.tail.place = OTHER_GAME
    r.start()
    r.tick()
    assert not r.engine.running
    assert r.states()[-1] == ("stopped", "Roblox is in another game, so I stopped. Join "
                                         "Block Mayhem X, then start again.")
    r.run(150)
    assert r.rejoin.calls == []


def test_a_stale_other_game_in_the_log_with_roblox_closed_still_rejoins():
    # The last log was another game, but Roblox isn't open: nothing to protect.
    r = Rig()
    r.tail.place = OTHER_GAME
    r.window.hwnd = 0
    r.start()
    r.run(25)
    assert r.engine.running
    assert r.rejoin.launches() == [rejoin.PUBLIC_LINK]


def test_leaving_to_the_home_screen_while_at_the_pc_stops_the_run():
    r = Rig().start()
    r.clock.touch()
    r.tail.feed("2026-10-09T02:00:00.000Z,1.0,1,6 [FLog::SessionTransitionFSM] Entered app session.")
    r.tick()
    assert not r.engine.running
    assert r.states()[-1] == ("stopped", "You left Block Mayhem X, so I stopped.")


# ------------------------------------------------------------- robustness ---
def test_a_failing_step_is_logged_once_and_the_rest_still_runs():
    r = Rig(anti_afk=True).start()
    r.window.grab_error = ValueError("bad grab")
    r.tail.feed(IDLE(700.0))
    r.tick(6)
    errors = r.logs("error")
    assert len(errors) == 1
    assert "reading the screen" in errors[0] and "ValueError: bad grab" in errors[0]
    assert len(r.inputs.spaces()) == 1                  # anti-AFK still ran
    r.window.grab_error = ValueError("another thing")
    r.tick(2)
    assert len(r.logs("error")) == 2                    # a different problem is new news


def test_tick_never_raises_even_when_everything_breaks():
    r = Rig(anti_afk=True)
    r.engine.get_settings = lambda: 1 / 0
    r.start()
    r.tail.poll_error = OSError("log gone")
    r.window.hwnd_error = RuntimeError("no windows")
    r.tick(4)                                            # must not raise
    errors = r.logs("error")
    assert any("settings" in e for e in errors)
    assert any("Roblox log" in e for e in errors)
    assert any("Roblox window" in e for e in errors)
    assert len(errors) == 3
    assert r.engine.running


def test_a_broken_listener_does_not_stop_the_run():
    r = Rig()
    calls = []

    def emit(ev):
        calls.append(ev)
        raise RuntimeError("UI is gone")

    r.engine = Engine(lambda: r.settings, emit, r.deps)
    r.engine.start(thread=False)
    r.tick(3)
    assert calls and r.engine.running and r.engine.state == "running"


def test_junk_settings_fall_back_to_the_defaults():
    r = Rig(anti_afk=True, anti_afk_after_min="ten", rare_min=None, auto_vote="yes",
            vote_maps="Lava", vote_click_offset="far").start()
    r.tail.feed(IDLE(605.0))
    r.tick()
    assert len(r.inputs.spaces()) == 1                  # default 10 minutes
    r.ocr.script["banner"] = ["Opal has spawned! (1 / 10,000)"]
    r.tick(2)
    assert r.of("rare") == []                           # default rare_min 100,000
    assert r.logs("error") == []


@pytest.mark.parametrize("setting, secs", [(1, 120), (2, 120), (10, 600), (15, 900), (30, 900),
                                           (7.0, 420), (True, 600), ("x", 600), (float("nan"), 600)])
def test_afk_after_is_clamped_to_2_to_15_minutes(setting, secs):
    assert engine_mod.afk_after({"anti_afk_after_min": setting}) == secs


# ------------------------------------------------------------ chat finds ---
FIND = "SomePlayer has collected a Inverted Resonance with a 1 / 1,000,000 chance!"


def test_a_collect_already_in_the_chat_at_start_is_not_a_new_find():
    r = Rig()
    r.tail.player = "SomePlayer"
    r.ocr.script["chat"] = [FIND]
    r.start()
    r.run(10)
    assert r.of("collected") == []
    r.ocr.script["chat"] = [FIND, FIND.replace("Inverted Resonance", "Sporedelic")
                            .replace("1,000,000", "100,000")]
    r.run(3)
    assert [(e["block"], e["variant"]) for e in r.of("collected")] == [("Sporedelic", "normal")]


def test_your_own_find_counts_when_ocr_misreads_a_letter_of_your_name():
    r = Rig()
    r.tail.player = "RealPlayerOne"
    r.start()
    r.tick()                                            # the first read only learns
    r.ocr.script["chat"] = [
        "ReaIPIayerOne has collected a Sporedelic with a 1 / 100,000 chance!",
        "OtherKid22 has collected a Inverted Opal with a 1 / 100,000 chance!"]
    r.run(3)
    assert [e["block"] for e in r.of("collected")] == ["Sporedelic"]


# --------------------------------------------------------------- anti-AFK ---
def test_jump_when_the_idle_line_reaches_the_threshold():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(570.5))
    r.tick()
    assert r.inputs.spaces() == []
    r.tail.feed(IDLE(600.5))
    r.tick()
    assert len(r.inputs.spaces()) == 1
    assert r.of("anti_afk") == [{"type": "anti_afk", "idle": 600.5, "how": "jump"}]
    assert any("Jumped so Roblox won't kick you" in t for t in r.logs("info"))


@pytest.mark.parametrize("what", ["user_busy", "behind", "minimized", "other_game", "switch_off"])
def test_no_jump_unless_roblox_in_front_in_bmx_and_user_idle(what):
    r = Rig(anti_afk=what != "switch_off").start()
    if what == "user_busy":
        r.clock.touch()                                 # 0.5 s idle at the tick
    elif what == "behind":
        r.window.fg = False
    elif what == "minimized":
        r.window.fg, r.window.minimized = False, True
    elif what == "other_game":
        r.tail.feed(JOIN(OTHER_GAME), ENTERED)
    r.tail.feed(IDLE(700.0))
    r.tick()
    assert r.inputs.spaces() == []
    assert r.of("anti_afk") == []


def test_user_idle_must_be_at_least_5_seconds():
    r = Rig(anti_afk=True).start()
    r.clock.touch()
    r.tail.feed(IDLE(700.0))
    r.tick(9)                                           # 4.5 s idle
    assert r.inputs.spaces() == []
    r.tick()                                            # 5.0 s idle
    assert len(r.inputs.spaces()) == 1


@pytest.mark.parametrize("minutes, below, at", [(1, 115.0, 125.0), (30, 890.0, 905.0),
                                                (10, 590.0, 605.0), (4, 230.0, 245.0)])
def test_threshold_setting_is_clamped(minutes, below, at):
    r = Rig(anti_afk=True, anti_afk_after_min=minutes).start()
    r.tail.feed(IDLE(below))
    r.tick()
    assert r.inputs.spaces() == []
    r.tail.feed(IDLE(at))
    r.tick()
    assert len(r.inputs.spaces()) == 1


def test_an_idle_line_from_before_the_users_last_input_is_ignored():
    r = Rig(anti_afk=True).start()
    r.window.fg = False
    r.tail.feed(IDLE(650.0))                            # Roblox behind: 650 < 15 min, nothing
    r.tick()
    r.window.fg = True
    r.clock.t += 0.2
    r.clock.touch()                                     # the user clicks into Roblox
    r.run(10)                                           # then walks away
    assert r.inputs.spaces() == []


def test_one_jump_per_idle_line_and_a_lower_number_confirms_it():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(605.0))
    r.tick()
    r.run(100)                                          # no new idle line
    assert len(r.inputs.spaces()) == 1
    r.tail.feed(IDLE(125.0))                            # Roblox's idle clock restarted
    r.tick()
    assert len(r.inputs.spaces()) == 1
    assert "The anti-AFK jump worked: Roblox's idle timer reset." in r.logs("info")


def test_a_jump_that_did_not_land_is_reported_and_tried_again():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(605.0))
    r.tick()
    r.run(30)
    r.tail.feed(IDLE(635.0))                            # still counting up
    r.tick()
    assert len(r.inputs.spaces()) == 2
    assert any("didn't reach Roblox" in t for t in r.logs("warn"))


def test_an_idle_line_written_just_before_the_jump_is_not_a_failure():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(605.0))
    r.tick()
    r.tail.feed(IDLE(606.0))                            # read 0.5 s after the jump
    r.tick()
    r.run(40)
    assert len(r.inputs.spaces()) == 1
    assert r.logs("warn") == []
    r.tail.feed(IDLE(122.0))
    r.tick()
    assert "The anti-AFK jump worked: Roblox's idle timer reset." in r.logs("info")


def test_no_jump_too_soon_after_a_failed_press():
    r = Rig(anti_afk=True).start()
    r.inputs.ok = False
    r.tail.feed(IDLE(605.0))
    r.tick()
    r.tail.feed(IDLE(615.0))
    r.run(20)
    assert len(r.inputs.spaces()) == 1                  # AFK_RETRY = 30 s
    assert r.of("anti_afk") == []
    assert sum("Couldn't press Space" in t for t in r.logs("warn")) == 1


def test_fallback_jump_when_the_game_prints_no_idle_lines():
    r = Rig(anti_afk=True).start()                      # user away an hour, no idle lines
    r.tick()
    assert len(r.inputs.spaces()) == 1
    assert r.of("anti_afk")[0]["how"] == "jump"
    r.run(599)
    assert len(r.inputs.spaces()) == 1
    r.run(1.5)
    assert len(r.inputs.spaces()) == 2                  # every anti_afk_after_min of no input


def test_no_fallback_while_idle_lines_keep_coming():
    r = Rig(anti_afk=True).start()
    for secs in range(130, 600, 30):                    # Roblox says < 10 min each time
        r.tail.feed(IDLE(float(secs)))
        r.run(30)
    assert r.inputs.spaces() == []


def test_no_fallback_while_the_user_is_around():
    r = Rig(anti_afk=True).start()
    r.clock.last_input = r.clock.t - 400                # away 400 s < 10 min
    r.tick()
    assert r.inputs.spaces() == []


def test_behind_other_windows_warns_once_and_touches_nothing():
    r = Rig(anti_afk=True).start()
    r.window.fg = False
    r.tail.feed(IDLE(905.0))
    r.tick()
    r.tail.feed(IDLE(935.0))
    r.tick()
    warn = "Roblox is behind other windows, you may get kicked (it will rejoin)."
    assert r.logs("warn").count(warn) == 1
    assert r.order == [] and r.inputs.calls == []


def test_behind_under_15_minutes_does_nothing():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg = False
    r.tail.feed(IDLE(880.0))
    r.tick()
    assert r.order == [] and r.logs("warn") == []


def test_bring_to_front_jump_when_the_user_is_away():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg = False
    r.tail.feed(IDLE(905.0))
    r.tick()
    assert r.order == ["front", "space", "restore"]
    assert r.of("anti_afk") == [{"type": "anti_afk", "idle": 905.0, "how": "front_jump"}]
    assert engine_mod.FRONT_SETTLE in r.clock.sleeps
    assert r.engine._busy is None


def test_bring_to_front_waits_for_the_user_to_be_away():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg = False
    r.clock.last_input = r.clock.t - 30                 # at the PC 30 s ago
    r.tail.feed(IDLE(905.0))
    r.tick()
    assert r.order == []
    assert "Roblox is behind other windows, you may get kicked (it will rejoin)." in r.logs("warn")


def test_bring_to_front_waits_while_a_full_screen_app_is_in_front():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg = False
    r.window.fullscreen = True                          # a film, no mouse for an hour
    r.tail.feed(IDLE(905.0))
    r.run(20)
    assert r.order == []
    r.window.fullscreen = False
    r.tick()
    assert r.order == ["front", "space", "restore"]


def test_bring_to_front_works_for_a_minimised_roblox_without_idle_lines():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg, r.window.minimized = False, True       # away an hour, no idle lines
    r.tick()
    assert r.order == ["front", "space", "restore"]


def test_the_users_window_comes_back_even_if_the_jump_breaks():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg = False
    r.inputs.space_error = RuntimeError("SendInput broke")
    r.tail.feed(IDLE(905.0))
    r.tick()
    assert r.order == ["front", "space", "restore"]
    assert r.engine._busy is None
    assert any("SendInput broke" in t for t in r.logs("error"))


def test_bring_to_front_failing_is_reported_once():
    r = Rig(anti_afk=True, afk_bring_to_front=True).start()
    r.window.fg, r.window.front_ok = False, False
    r.tail.feed(IDLE(905.0))
    r.tick()
    r.run(31)
    r.tail.feed(IDLE(940.0))
    r.tick()
    assert r.order == ["front", "front"]                # no restore: nothing was moved
    assert r.inputs.calls == []
    assert sum("Couldn't bring Roblox to the front" in t for t in r.logs("warn")) == 1


# ----------------------------------------------------------------- screen ---
@pytest.mark.parametrize("fg, minimized, bmx, reads", [
    (True, False, True, True), (False, False, True, False),
    (False, True, True, False), (True, False, False, False)])
def test_screen_is_read_only_with_roblox_in_front_in_bmx(fg, minimized, bmx, reads):
    r = Rig(auto_rejoin=False).start()
    r.window.fg, r.window.minimized = fg, minimized
    if not bmx:
        r.tail.feed(JOIN(OTHER_GAME), ENTERED)
    r.tick(4)
    assert (r.window.grabs > 0) == reads
    assert (len(r.ocr.reads) > 0) == reads


def test_hud_event_with_timer_storage_and_world():
    r = Rig().start()
    r.ocr.script["timer"] = ["08:31"]
    r.ocr.script["storage"] = ["215 / 350"]
    r.tail.feed(SONG("Dreamland"))
    r.tick()
    assert r.of("hud")[-1] == {"type": "hud", "timer": 511, "storage": [215, 350],
                               "world": "Dreamland"}
    r.ocr.script["timer"] = []
    r.run(3)
    assert r.of("hud")[-1]["timer"] == 508              # counts down between readings


def test_top_hud_read_every_2_s_and_every_half_second_when_low():
    r = Rig(auto_vote=False).start()
    r.ocr.script["timer"] = ["05:00"]
    r.ocr.script["storage"] = ["10/350"]
    r.run(10)
    slow = r.ocr.reads.count("storage")
    r.ocr.reads.clear()
    r.ocr.script["timer"] = ["00:14"]
    r.run(2)                                            # the first read sees it low
    r.ocr.reads.clear()
    r.run(10)
    fast = r.ocr.reads.count("storage")
    assert slow == 5 * 2                                # two agreeing reads, 5 times
    assert fast == 20 * 2


def test_world_from_world_music_lines():
    r = Rig().start()
    r.tail.feed(SONG("Arctic"))
    r.tick()
    r.tail.feed(SONG("Arctic"))                         # the same music again
    r.tick()
    r.tail.feed(SONG("Dreamland"))
    r.tick()
    worlds = [t for t in r.logs("info") if t.startswith("World is now")]
    assert worlds == ["World is now Arctic.", "World is now Dreamland."]
    assert r.engine.world == "Dreamland"


def test_world_is_forgotten_when_the_timer_restarts_unless_music_came_after():
    r = Rig(auto_vote=False).start()
    r.tail.feed(SONG("Dreamland"))
    r.ocr.script["timer"] = ["00:40"]
    r.tick()
    r.ocr.script["timer"] = ["09:55"]                   # a vote went by
    r.run(3)
    assert r.engine.world is None and r.of("hud")[-1]["world"] is None
    # Next cycle: Arctic's music starts after the vote, so it is the new world.
    r.ocr.script["timer"] = ["00:30"]
    r.run(3)
    r.tail.feed(SONG("Arctic"))
    r.tick()
    r.ocr.script["timer"] = ["09:58"]
    r.run(3)
    assert r.engine.world == "Arctic"


def test_a_new_servers_world_music_counts_after_the_world_banner():
    r = Rig(auto_vote=False).start()
    r.ocr.script["banner"] = ["World voting concluded! The new world is Arctic."]
    r.tick()
    assert r.engine.world == "Arctic"
    r.ocr.script["banner"] = []
    r.run(120)
    r.tail.feed(JOIN(), ENTERED, LOADED, SONG("Dreamland"))     # rejoined: another world
    r.tick()
    assert r.engine.world == "Dreamland"


def test_storage_full_for_60_s_alerts_once_until_it_drops():
    r = Rig().start()
    r.ocr.script["storage"] = ["350/350"]
    r.run(59)
    assert r.of("storage_full") == []
    r.run(2)
    assert r.of("storage_full") == [{"type": "storage_full", "n": 350, "m": 350}]
    r.run(120)
    assert len(r.of("storage_full")) == 1
    r.ocr.script["storage"] = ["349/350"]
    r.run(3)
    r.ocr.script["storage"] = ["351/350"]
    r.run(61)
    assert len(r.of("storage_full")) == 2
    assert any("Storage is full (351/350)" in t for t in r.logs("warn"))


def test_storage_full_is_not_alerted_again_after_a_rejoin():
    r = Rig().start()
    r.ocr.script["storage"] = ["350/350"]
    r.run(61)
    assert len(r.of("storage_full")) == 1
    for _ in range(2):
        r.tail.feed(JOIN(), ENTERED, LOADED)            # a new server, still full
        r.run(70)
    assert len(r.of("storage_full")) == 1
    r.ocr.script["storage"] = ["10/350"]                # room again: the next full is news
    r.run(3)
    r.ocr.script["storage"] = ["350/350"]
    r.run(61)
    assert len(r.of("storage_full")) == 2


def test_storage_full_for_less_than_60_s_does_not_alert():
    r = Rig().start()
    r.ocr.script["storage"] = ["350/350"]
    r.run(50)
    r.ocr.script["storage"] = ["120/350"]
    r.run(30)
    r.ocr.script["storage"] = ["350/350"]
    r.run(50)
    assert r.of("storage_full") == []


def test_cant_see_the_screen_is_a_warning_not_a_crash():
    r = Rig().start()
    r.window.grab_error = OSError("screen grab failed")
    r.run(5)
    assert r.logs("warn") == ["Can't see the Roblox window right now (is the PC locked?)."]
    assert r.logs("error") == [] and r.engine.running


# ------------------------------------------------------------ rare blocks ---
RESONANCE = "Resonance has spawned! (1 / 100,000)"


def test_banner_becomes_a_rare_event_with_a_picture():
    r = Rig(save_pictures=True, picture_min=1).start()
    r.ocr.script["banner"] = [RESONANCE]
    r.tick()
    [ev] = r.of("rare")
    assert {k: ev[k] for k in ("block", "denominator", "variant", "source", "ping")} == \
        {"block": "Resonance", "denominator": 100000, "variant": "normal", "source": "banner",
         "ping": False}
    assert isinstance(ev["picture"], Image.Image) and ev["picture"].size == CLIENT
    assert [tag for _, tag in r.pictures] == ["rare"]
    assert "Rare block: Resonance spawned (1 in 100,000)!" in r.logs("info")


@pytest.mark.parametrize("text, ping_min, ping", [
    ("Voidrinyx has spawned! (1 / 1,000,000)", 1_000_000, True),
    ("Voidrinyx has spawned! (1 / 1,000,000)", 5_000_000, False),
    (RESONANCE, 100_000, True),
])
def test_ping_from_rare_ping_min(text, ping_min, ping):
    r = Rig(rare_ping_min=ping_min).start()
    r.ocr.script["banner"] = [text]
    r.tick()
    assert r.of("rare")[0]["ping"] is ping


def test_below_rare_min_is_only_logged():
    r = Rig().start()
    r.ocr.script["banner"] = ["Opal has spawned! (1 / 10,000)"]
    r.tick()
    assert r.of("rare") == [] and r.pictures == []
    assert "Opal spawned (1 in 10,000)." in r.logs("info")
    r2 = Rig(rare_min=5000).start()
    r2.ocr.script["banner"] = ["Opal has spawned! (1 / 10,000)"]
    r2.tick()
    assert r2.of("rare")[0]["denominator"] == 10000


@pytest.mark.parametrize("always, alerts", [(True, True), (False, False)])
def test_variants_alert_always_when_switched_on(always, alerts):
    # Chromatic Opal is 1 in 250,000 (Opal 10,000 x 25): under a 1-in-1M line
    r = Rig(alert_variants_always=always, rare_min=1_000_000).start()
    r.ocr.script["banner"] = ["Chromatic Opal has spawned! (1 / 250,000)"]
    r.tick()
    rares = r.of("rare")
    assert bool(rares) is alerts
    if alerts:
        assert rares[0]["block"] == "Opal" and rares[0]["variant"] == "chromatic"


def test_the_same_banner_alerts_once_within_30_s():
    r = Rig().start()
    r.ocr.script["banner"] = [RESONANCE]
    r.run(10)                                           # up for 10 s
    r.ocr.script["banner"] = []
    r.run(20)
    r.ocr.script["banner"] = ["e) " + RESONANCE]        # back within 30 s, read a bit noisier
    r.run(2)
    assert len(r.of("rare")) == 1
    r.ocr.script["banner"] = []
    r.run(31)
    r.ocr.script["banner"] = [RESONANCE]
    r.run(1)
    assert len(r.of("rare")) == 2


def test_banner_without_its_number_waits_for_a_better_read():
    r = Rig().start()
    r.ocr.script["banner"] = ["Mystery has spawned!"]
    r.run(1.5)
    assert r.of("rare") == []
    r.ocr.script["banner"] = ["Mystery has spawned! (1 / 250,000)"]
    r.run(1)
    [ev] = r.of("rare")
    assert ev["denominator"] == 250000


def test_banner_that_never_shows_a_number_still_alerts_without_ping():
    r = Rig().start()
    r.ocr.script["banner"] = ["Mystery has spawned!"]
    r.run(5)
    [ev] = r.of("rare")
    assert ev["block"] == "Mystery" and ev["denominator"] is None and ev["ping"] is False


@pytest.mark.parametrize("then", ["behind", "join", "stop", "disconnect"])
def test_a_banner_waiting_for_a_better_read_is_never_lost(then):
    r = Rig().start()
    r.ocr.script["banner"] = ["Mystery has spawned!"]          # no number: waits
    r.tick()
    assert r.of("rare") == []
    r.ocr.script["banner"] = []
    if then == "behind":
        r.window.fg = False                             # alt-tabbed to check Discord
        r.tick()
    elif then == "join":
        r.tail.feed(JOIN(), ENTERED, LOADED)
        r.tick()
    elif then == "stop":
        r.engine.stop()
    else:
        r.tail.feed(DC(277))
        r.tick()
    [ev] = r.of("rare")
    assert ev["block"] == "Mystery" and ev["denominator"] is None


def test_a_held_log_song_goes_out_when_the_run_stops():
    r = Rig().start()
    r.tail.feed(SONG("lucid_song"))
    r.tick()
    r.engine.stop()
    assert [e["source"] for e in r.of("rare")] == ["log_song"]
    assert r.states()[-1] == ("stopped", "Stopped")


def test_banners_read_during_the_vote_scan_get_the_same_checks():
    r = Rig().start()
    r.ocr.script["timer"] = ["00:04"]
    r.tick()                                            # the vote scan starts
    r.ocr.script["timer"] = []
    y = 0.80 * CLIENT[1]
    r.ocr.script["client"] = [OcrLine("Reljc has spawned! (1 / 600", (
        OcrWord("Reljc", 700, y, 90, 28), OcrWord("has", 800, y, 50, 28),
        OcrWord("spawned!", 860, y, 120, 28), OcrWord("(1", 990, y, 30, 28),
        OcrWord("/", 1025, y, 10, 28), OcrWord("600", 1040, y, 50, 28)))]
    r.run(3)
    assert r.engine._vote is not None
    [ev] = r.of("rare")
    assert (ev["block"], ev["denominator"]) == ("Relic", 600_000)


def test_known_block_without_a_number_uses_the_table():
    r = Rig().start()
    r.ocr.script["banner"] = ["SNAKEBLOCK has spawned!"]
    r.tick()
    assert r.of("rare")[0]["denominator"] == 505000


def test_banners_that_are_not_blocks_are_not_logged():
    r = Rig().start()
    r.ocr.script["banner"] = ["The original Block Mayhem was my very first game!"]
    r.run(4)
    assert not any(t.startswith("Banner:") for t in r.logs("info"))


@pytest.mark.parametrize("raw, name, den, ping", [
    ("snakeblock", "SNAKEBLOCK", 505000, False), ("voidrinyx", "Voidrinyx", 1000000, True),
    ("sporedelic", "Sporedelic", 100000, False), ("lucid", "Lucid", 250000, False)])
def test_log_song_alerts_with_the_table_rarity(raw, name, den, ping):
    r = Rig().start()
    r.window.fg = False                                 # no banner can come: no wait
    r.tail.feed(SONG(f"{raw}_song"))
    r.tick()
    [ev] = r.of("rare")
    assert ev == {"type": "rare", "block": name, "denominator": den, "variant": "normal",
                  "source": "log_song", "ping": ping, "picture": None, "world": None}


def test_log_song_waits_briefly_for_the_banner_and_they_merge():
    r = Rig().start()
    r.tail.feed(SONG("snakeblock_song"))
    r.tick()
    assert r.of("rare") == []
    r.ocr.script["banner"] = ["SNAKEBLOCK has spawned! (1 / 505,000)"]
    r.run(6)
    [ev] = r.of("rare")
    assert ev["source"] == "banner" and ev["block"] == "SNAKEBLOCK"


def test_log_song_with_no_banner_alerts_after_the_wait():
    r = Rig().start()
    r.tail.feed(SONG("lucid_song"))
    r.tick()
    r.run(2.5)
    assert r.of("rare") == []
    r.run(1)
    assert [e["source"] for e in r.of("rare")] == ["log_song"]


def test_banner_within_60_s_of_the_log_alert_is_the_same_block():
    snake = "SNAKEBLOCK has spawned! (1 / 505,000)"
    r = Rig().start()
    r.window.fg = False
    r.tail.feed(SONG("snakeblock_song"))
    r.tick()
    r.window.fg = True
    r.ocr.script["banner"] = [snake]
    r.run(5)
    assert [e["source"] for e in r.of("rare")] == ["log_song"]
    r.ocr.script["banner"] = []
    r.run(60)
    r.ocr.script["banner"] = [snake]
    r.run(2)
    assert [e["source"] for e in r.of("rare")] == ["log_song", "banner"]


def test_below_threshold_banner_also_quiets_its_log_line():
    r = Rig(rare_min=1_000_000).start()
    r.ocr.script["banner"] = ["Lucid has spawned! (1 / 50,000)"]
    r.tick()
    r.tail.feed(SONG("lucid_song"))                     # unknown rarity on its own
    r.run(5)
    assert r.of("rare") == []


def test_a_chromatic_banner_right_after_a_normal_one_is_its_own_alert():
    r = Rig().start()
    r.ocr.script["banner"] = ["Vexen has spawned! (1 / 25,000)"]
    r.run(6)
    r.ocr.script["banner"] = []
    r.run(10)
    r.ocr.script["banner"] = ["Chromatic Vexen has spawned! (1 / 625,000)"]
    r.run(6)
    [ev] = r.of("rare")
    assert (ev["block"], ev["variant"], ev["denominator"], ev["source"]) == \
        ("Vexen", "chromatic", 625_000, "banner")


def test_a_normal_spawn_between_does_not_make_the_late_chromatic_note_a_second_alert():
    r = Rig().start()
    r.ocr.script["banner"] = ["Chromatic Vexen has spawned! (1 / 625,000)"]
    r.tick()
    r.ocr.script["banner"] = []
    r.run(120)
    r.ocr.script["banner"] = ["Vexen has spawned! (1 / 25,000)"]   # below rare_min
    r.tick()
    r.ocr.script["banner"] = []
    r.run(120)
    r.tail.feed(CHROMA("vexen"))
    r.tick()
    assert [(e["variant"], e["source"]) for e in r.of("rare")] == [("chromatic", "banner")]


def test_chromatic_log_line_is_a_late_variant_alert():
    r = Rig().start()
    r.tail.feed(CHROMA("vexen"))
    r.tick()
    [ev] = r.of("rare")
    assert ev == {"type": "rare", "block": "Vexen", "denominator": None, "variant": "chromatic",
                  "source": "log_chromatic", "ping": False, "picture": None, "world": None}


def test_chromatic_nil_and_switched_off_variants_only_log():
    r = Rig().start()
    r.tail.feed(CHROMA("nil"))
    r.tick()
    assert r.of("rare") == []
    assert "A chromatic block was on your server (Roblox didn't say which)." in r.logs("info")
    r2 = Rig(alert_variants_always=False).start()
    r2.tail.feed(CHROMA("vexen"))
    r2.tick()
    assert r2.of("rare") == []


def test_chromatic_log_after_its_banner_is_not_a_second_alert():
    r = Rig().start()
    r.ocr.script["banner"] = ["Chromatic Vexen has spawned! (1 / 2,500,000)"]
    r.tick()
    r.ocr.script["banner"] = []
    r.run(300)                                          # the note comes minutes later
    r.tail.feed(CHROMA("vexen"))
    r.tick()
    assert [e["source"] for e in r.of("rare")] == ["banner"]


def test_rare_lines_from_another_game_are_ignored():
    r = Rig(auto_rejoin=False).start()
    r.tail.feed(JOIN(OTHER_GAME), ENTERED, SONG("voidrinyx_song"), CHROMA("vexen"), IDLE(900.0))
    r.tick(10)
    assert r.of("rare") == []


# ------------------------------------------------------------------- vote ---
def test_vote_clicks_the_highest_ranked_option_in_screen_pixels():
    r = Rig(vote_maps=["Lava", "Void"]).start()
    VoteScreen(r, ("Void", "Ocean", "Lava"), in_secs=20)
    r.run(30)
    # Lava is the third name: centre (300 + 1200 + 80, 480 + 20) in the client,
    # and the client starts 23 px down the screen.
    assert r.inputs.clicks() == [("click", 1580, 588)]
    assert r.of("vote") == [{"type": "vote", "options": ["Void", "Ocean", "Lava"],
                             "chose": "Lava", "ok": True}]
    assert r.pictures == []                           # a vote that worked: no picture
    assert "Voted for Lava." in r.logs("info")
    assert r.engine._busy is None


@pytest.mark.parametrize("ranking", [[], ["Glitch", "Baseplate"]])
def test_vote_clicks_the_first_option_when_nothing_ranked_is_there(ranking):
    r = Rig(vote_maps=ranking).start()
    VoteScreen(r, ("Void", "Ocean", "Lava"), in_secs=8)
    r.run(15)
    assert r.inputs.clicks() == [("click", 380, 588)]
    assert r.of("vote")[0]["chose"] == "Void"


def test_vote_click_offset_moves_the_click():
    r = Rig(vote_click_offset=[10, -20]).start()
    VoteScreen(r, ("Cave", "Arctic"), in_secs=8)
    r.run(15)
    assert r.inputs.clicks() == [("click", 390, 568)]


def test_vote_is_not_looked_for_until_the_timer_is_low():
    r = Rig().start()
    vs = VoteScreen(r, ("Void", "Ocean"), in_secs=60)
    r.run(54)                                           # 6 s left
    assert "client" not in r.ocr.reads
    r.run(1.5)
    assert "client" in r.ocr.reads
    assert vs.at > r.clock.t                            # looking before it shows up


def test_vote_reclicks_once_when_nothing_changed():
    # The countdown and other cards change every second: only the chosen
    # card's own vote count says whether the click counted.
    r = Rig().start()
    VoteScreen(r, ("Void", "Ocean"), in_secs=8, react=False)
    r.run(25)
    assert len(r.inputs.clicks()) == 2
    assert r.of("vote")[0]["ok"] is True
    assert any("may not have counted" in t for t in r.logs("warn"))
    assert "Voted for Void." in r.logs("info")


def test_vote_counts_a_click_by_the_chosen_cards_vote_count():
    r = Rig(vote_maps=["Ocean"]).start()
    vs = VoteScreen(r, ("Void", "Ocean"), in_secs=8, react=False)

    def screen():
        lines = vs.client()
        if r.inputs.clicks() and lines:            # only Void's count goes up (someone else)
            lines = [ln for ln in lines if ln.text != "O Votes" or ln.words[0].x > 600] + \
                vote_lines("Void", votes={"Void": 1})[2:3]
        return lines
    r.ocr.script["client"] = screen
    r.run(25)
    assert len(r.inputs.clicks()) == 2                  # Ocean's own count never moved
    assert any("may not have counted" in t for t in r.logs("warn"))


def test_vote_does_not_reclick_when_the_screen_reacted():
    r = Rig().start()
    VoteScreen(r, ("Void", "Ocean"), in_secs=8, react=True)
    r.run(25)
    assert len(r.inputs.clicks()) == 1
    assert r.logs("warn") == []


def test_vote_waits_while_the_user_is_at_the_pc():
    r = Rig().start()
    vs = VoteScreen(r, ("Void", "Ocean"), in_secs=8)
    while r.clock.t < vs.at + 3:
        r.clock.touch()
        r.tick()
    assert r.inputs.clicks() == []
    assert any("you're using the PC" in t for t in r.logs("info"))
    r.run(8)
    assert len(r.inputs.clicks()) == 1                  # 5 s after the last touch
    assert r.of("vote")[0]["ok"] is True


def test_no_click_when_the_user_never_left_the_pc():
    r = Rig().start()
    vs = VoteScreen(r, ("Void", "Ocean"), in_secs=8, last=10)
    while r.clock.t < vs.until + 2:
        r.clock.touch()
        r.tick()
    assert r.inputs.clicks() == []
    assert r.of("vote") == [{"type": "vote", "options": ["Void", "Ocean"], "chose": "Void",
                             "ok": False}]
    assert "Didn't vote this time: you were using the PC." in r.logs("info")


def test_vote_search_starts_when_a_low_timer_disappears():
    r = Rig().start()
    r.ocr.script["timer"] = ["00:12"]
    r.tick()
    r.ocr.script["timer"] = []                          # the vote screen covers the HUD
    r.tick()
    r.ocr.reads.clear()
    r.tick()
    assert "client" in r.ocr.reads
    r.ocr.script["client"] = vote_lines("Lava", "Void")
    r.tick()
    assert r.inputs.clicks() == [("click", 380, 588)]


def test_no_vote_screen_in_45_s_ends_the_search_and_frees_the_lock():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(130.0))                            # Roblox idle lines are coming
    VoteScreen(r, (), in_secs=6)                        # the timer runs out, no vote screen
    r.run(10)
    assert r.engine._busy == "vote"
    r.tail.feed(IDLE(700.0))
    r.tick()
    assert r.inputs.spaces() == []                      # no jump during a vote search
    r.run(45)
    assert "Didn't see a vote screen this time." in r.logs("info")
    assert r.of("vote") == []
    assert r.engine._busy is None
    assert len(r.inputs.spaces()) == 1                  # the jump waited for the lock


def test_vote_search_with_roblox_behind_ends_quietly():
    r = Rig().start()
    VoteScreen(r, ("Void", "Ocean"), in_secs=6)
    r.run_until(lambda: r.engine._vote is not None, limit=10)
    r.window.fg = False                                 # the user switched away
    r.run(50)
    assert r.engine._vote is None and r.engine._busy is None
    assert not any("vote" in t.lower() for t in r.logs())
    assert r.of("vote") == [] and r.inputs.calls == []


def test_auto_vote_off_never_looks_for_the_vote():
    r = Rig(auto_vote=False).start()
    VoteScreen(r, ("Void", "Ocean"), in_secs=8)
    r.run(30)
    assert r.inputs.clicks() == [] and not any("Vote screen" in x for x in r.logs("info"))


def test_one_vote_search_per_vote():
    r = Rig().start()
    VoteScreen(r, ("Void", "Ocean"), in_secs=8, last=60)    # stays up a long time
    r.run(80)
    assert len(r.of("vote")) == 1 and r.pictures == []


def test_world_change_after_a_vote_mentions_the_vote():
    r = Rig(vote_maps=["Arctic"]).start()
    VoteScreen(r, ("Arctic", "Void"), in_secs=8, last=4)
    r.tail.feed(SONG("Dreamland"))
    r.run_until(lambda: r.of("vote"))
    assert r.engine.world is None                       # it was Dreamland before the vote
    r.tail.feed(SONG("Arctic"))
    r.tick()
    assert "World is now Arctic. You voted Arctic." in r.logs("info")
    assert r.engine.world == "Arctic"


# ----------------------------------------------------------------- rejoin ---
def _launched_and_rejoined(r: Rig, n: int) -> None:
    r.tail.switch(NEW_LOG.format(n))
    r.tail.feed(JOIN(), ENTERED, LOADED)
    r.tick()


def test_disconnect_waits_for_the_user_to_be_away_then_rejoins():
    r = Rig(private_server_link=PRIVATE).start()
    r.clock.touch()
    r.tail.feed(DC(285))
    r.tick()
    assert "Disconnected from the server (code 285)." in r.logs("warn")
    assert any(t.startswith("You got disconnected (code 285). I'll rejoin once you've been away")
               for t in r.logs("warn"))
    assert r.states()[-1] == ("waiting", "Waiting for you to step away to rejoin")
    r.run(59)
    assert r.rejoin.calls == []
    r.run(1)
    assert [c[0] for c in r.rejoin.calls] == ["kill", "launch"]
    assert r.rejoin.launches() == [PRIVATE_URI]
    assert r.of("rejoin") == [{"type": "rejoin", "phase": "start", "try": 1,
                               "why": "You got disconnected (code 285)"}]
    assert r.states()[-1] == ("rejoining", "Rejoining (try 1 of 5)")


def test_no_rejoin_while_a_full_screen_app_is_in_front():
    # A full-screen video or a gamepad game doesn't move the mouse: the user
    # is there all the same, so Roblox isn't closed and relaunched over it.
    r = Rig().start()
    r.window.fullscreen = True
    r.tail.feed(DC(277))
    r.run(300)
    assert r.rejoin.calls == []
    assert r.states()[-1] == ("waiting", "Waiting for you to step away to rejoin")
    r.window.fullscreen = False
    r.tick()
    assert [c[0] for c in r.rejoin.calls] == ["kill", "launch"]


def test_rejoin_succeeds_on_a_new_log_with_join_entered_then_loaded():
    r = Rig(private_server_link=PRIVATE).start()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.switch(NEW_LOG.format(1))
    r.tail.feed(JOIN(), ENTERED)
    r.tick()
    assert "Joined the server, waiting for the game to load." in r.logs("info")
    assert r.rejoins() == [("start", 1)]
    r.tail.feed(LOADED)
    r.tick()
    assert r.rejoins() == [("start", 1), ("ok", 1)]
    assert r.states()[-1] == ("running", "In Block Mayhem X")
    assert r.engine._busy is None and r.engine._rj is None
    r.run(120)
    assert len(r.rejoin.launches()) == 1                # nothing more to do


def test_a_join_in_the_old_log_does_not_count():
    r = Rig().start()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.feed(JOIN(), ENTERED, LOADED)                # same file: not the new client
    r.tick()
    assert r.rejoins() == [("start", 1)]
    r.run(120)
    assert r.rejoins()[-1] == ("fail", 1)
    assert r.of("rejoin")[-1]["why"] == "Roblox didn't get into the server within 2 minutes."


def test_rejoin_backs_off_rotates_links_and_gives_up_after_5():
    r = Rig(private_server_link=RAW).start()
    r.tail.feed(DC(277))
    r.run_until(lambda: not r.engine.running, limit=3000)
    times = r.rejoin.launch_times()
    gaps = [round(b - a) for a, b in zip(times, times[1:])]
    # 120 s waiting for the join each time, then 30 / 60 / 120 / 300 s.
    assert gaps == [150, 180, 240, 420]
    assert r.rejoin.launches() == [RAW, PRIVATE_URI, RAW, PRIVATE_URI, RAW]
    assert r.rejoins() == [("start", 1), ("fail", 1), ("start", 2), ("fail", 2),
                           ("start", 3), ("fail", 3), ("start", 4), ("fail", 4),
                           ("start", 5), ("fail", 5), ("gave_up", 5)]
    assert [e.get("wait") for e in r.of("rejoin") if e["phase"] == "fail"] == \
        [30.0, 60.0, 120.0, 300.0, None]
    why = "Couldn't get back into Block Mayhem X after 5 tries in a row."
    assert r.of("rejoin")[-1]["why"] == why
    assert r.states()[-1] == ("stopped", why)
    r.run(600)
    assert len(r.rejoin.launches()) == 5


def test_no_roblox_window_for_20_s_rejoins():
    r = Rig().start()
    r.window.hwnd = 0
    r.run(20)
    assert r.rejoin.calls == []
    r.tick()
    assert r.rejoin.launches() == [rejoin.PUBLIC_LINK]   # no link saved: public server
    assert r.of("rejoin")[0]["why"] == "Roblox closed"


@pytest.mark.parametrize("lines, why", [
    ([LEFT], "You left Block Mayhem X")])
def test_out_of_bmx_for_60_s_rejoins(lines, why):
    r = Rig().start()
    r.tail.feed(*lines)
    r.run(60)                                           # out since the first tick: 59.5 s
    assert r.rejoin.calls == []
    r.tick()
    assert r.of("rejoin")[0]["why"] == why
    assert len(r.rejoin.launches()) == 1


def test_back_in_the_game_by_itself_cancels_the_rejoin():
    r = Rig().start()
    r.clock.touch()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.feed(JOIN(), ENTERED, LOADED)                # the user pressed Reconnect
    r.tick()
    assert "You're back in Block Mayhem X, so no rejoin is needed." in r.logs("info")
    r.run(120)
    assert r.rejoin.calls == [] and r.of("rejoin") == []


def test_auto_rejoin_off_only_waits():
    r = Rig(auto_rejoin=False).start()
    r.tail.feed(DC(277))
    r.run(200)
    assert r.rejoin.calls == []
    assert r.logs("warn").count("You got disconnected (code 277). Auto rejoin is off, so I'm "
                                "just waiting.") == 1
    assert r.states()[-1] == ("waiting", "Disconnected")


def test_at_most_6_rejoins_an_hour():
    r = Rig().start()
    for n in range(6):
        r.tail.feed(DC(277))
        r.tick()
        _launched_and_rejoined(r, n)
    assert r.rejoins().count(("ok", 1)) == 6
    r.tail.feed(DC(277))
    r.run(60)
    assert len(r.rejoin.launches()) == 6
    assert any("Rejoined 6 times in the last hour" in t for t in r.logs("warn"))
    assert r.states()[-1] == ("waiting", "Rejoined a lot this hour, waiting before the next try")
    r.clock.t += 3600
    r.tick()
    assert len(r.rejoin.launches()) == 7


def test_game_that_never_finishes_loading_is_a_failed_try():
    r = Rig().start()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.switch(NEW_LOG.format(1))
    r.tail.feed(JOIN(), ENTERED)
    r.tick()
    r.run(60)
    assert r.rejoins() == [("start", 1), ("fail", 1)]
    assert r.of("rejoin")[-1]["why"] == "The game didn't finish loading within a minute."
    # "Trying again in 30 s" is kept: the rejoin isn't dropped as "you're back".
    assert r.engine._rj is not None
    assert "You're back in Block Mayhem X, so no rejoin is needed." not in r.logs("info")
    r.run(31)
    assert r.rejoins()[-1] == ("start", 2)
    assert len(r.rejoin.launches()) == 2


def test_a_reconnect_by_hand_gets_time_to_load_before_a_retry():
    r = Rig().start()
    r.clock.touch()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.feed(JOIN(), ENTERED)                        # Reconnect, still loading
    r.run(30)
    assert r.rejoin.calls == [] and r.engine._rj is not None
    r.tail.feed(LOADED)
    r.tick()
    assert r.engine._rj is None
    assert "You're back in Block Mayhem X, so no rejoin is needed." in r.logs("info")
    r.run(120)
    assert r.rejoin.calls == []


def test_disconnect_while_joining_fails_the_try_at_once():
    r = Rig().start()
    r.tail.feed(DC(277))
    r.tick()
    r.tail.switch(NEW_LOG.format(1))
    r.tail.feed(JOIN(), DC(267))
    r.tick()
    assert r.rejoins() == [("start", 1), ("fail", 1)]
    assert r.of("rejoin")[-1]["why"] == "You got disconnected while joining."


def test_a_launch_that_raises_is_a_failed_try():
    r = Rig().start()
    r.rejoin.launch_error = OSError("no roblox:// handler")
    r.tail.feed(DC(277))
    r.tick()
    assert r.rejoins() == [("start", 1), ("fail", 1)]
    assert r.of("rejoin")[-1]["wait"] == 30.0
    assert any("opening Roblox" in t and "no roblox:// handler" in t for t in r.logs("error"))
    assert r.engine._busy is None


def test_a_failed_launch_never_puts_the_link_in_the_log():
    r = Rig(private_server_link=PRIVATE).start()
    # os.startfile names the file it failed on, and here that file is the link.
    r.rejoin.launch_error = OSError(2, "No handler", PRIVATE_URI)
    assert "12345678" in str(r.rejoin.launch_error)
    r.tail.feed(DC(277))
    r.tick()
    errors = r.logs("error")
    assert len(errors) == 1 and "No handler: '<link>'" in errors[0]
    assert not any("12345678" in str(e) for e in r.events)


@pytest.mark.parametrize("text, want", [
    (f"[WinError 1155] no handler: '{PRIVATE_URI}'", "[WinError 1155] no handler: '<link>'"),
    (f"failed on {SHARE}", "failed on <link>"),
    ("bad privateServerLinkCode=12345678&x=1", "bad privateServerLinkCode=…&x=1"),
    ("linkCode=abc code=zzz9", "linkCode=… code=…"),
    ("plain text stays", "plain text stays"),
])
def test_error_text_is_scrubbed_of_links_and_codes(text, want):
    assert engine_mod._scrub(text) == want


def test_stop_between_kill_and_launch_never_opens_roblox():
    r = Rig().start()
    r.rejoin.on_kill = r.engine.stop
    r.tail.feed(DC(277))
    r.tick()
    assert [c[0] for c in r.rejoin.calls] == ["kill"]
    assert not r.engine.running and r.states()[-1] == ("stopped", "Stopped")


def test_stopped_while_waiting_for_the_user_never_rejoins():
    r = Rig().start()
    r.clock.touch()
    r.tail.feed(DC(277))
    r.tick()
    r.engine.stop()
    r.run(120)
    assert r.rejoin.calls == []


def test_rejoin_ends_a_vote_search_and_blocks_input():
    r = Rig(anti_afk=True).start()
    r.tail.feed(IDLE(130.0))
    VoteScreen(r, ("Void", "Ocean"), in_secs=6)
    r.run(2)
    assert r.engine._busy == "vote"
    r.tail.feed(DC(277), IDLE(900.0))
    r.tick()
    assert r.engine._busy == "rejoin" and r.engine._vote is None
    assert len(r.rejoin.launches()) == 1
    r.run(10)
    assert r.inputs.calls == []                         # no jump, no vote click while rejoining
    assert r.of("vote") == []


def test_disconnected_with_auto_rejoin_off_blocks_input():
    r = Rig(anti_afk=True, auto_rejoin=False).start()
    r.tail.feed(DC(277), IDLE(900.0))
    VoteScreen(r, ("Void", "Ocean"), in_secs=6)        # whatever is on screen, don't click it
    r.run(20)
    assert r.inputs.calls == []


def test_bad_link_warns_at_start_without_showing_it():
    hook = "https://discord.com/api/webhooks/123/secret-token"
    r = Rig(private_server_link=hook).start()
    r.tick()
    warns = r.logs("warn")
    assert any("isn't from roblox.com" in t and "public server" in t for t in warns)
    assert not any("discord" in e.get("text", "") or "secret" in e.get("text", "")
                   for e in r.events)


def test_missing_ocr_is_explained_at_start():
    r = Rig()
    r.ocr.available = False
    r.ocr.error = "Windows has no text-reading language installed."
    r.start()
    r.tick()
    assert any(t.startswith("Can't read the screen. Windows has no text-reading language")
               for t in r.logs("warn"))


# --------------------------------------------------------- files and deps ---
def test_save_picture_writes_into_the_test_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MAYHEM_DATA_DIR", str(tmp_path))
    name = engine_mod.save_picture(Image.new("RGB", (64, 32), "red"), "vote")
    assert name and re.fullmatch(r"\d{8}_\d{6}_\d{3}_vote\.jpg", name)
    path = tmp_path / "events" / name
    assert path.is_file()
    with Image.open(path) as img:
        assert img.size == (64, 32)


def test_save_picture_never_touches_the_real_folder(monkeypatch):
    monkeypatch.delenv("MAYHEM_DATA_DIR", raising=False)
    assert engine_mod.save_picture(Image.new("RGB", (8, 8)), "rare") is None


def test_trim_pictures_keeps_the_newest_and_only_touches_its_own(tmp_path):
    ours = [tmp_path / f"20261009_0200{i:02d}_000_vote.jpg" for i in range(6)]
    for i, p in enumerate(ours):
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    other = tmp_path / "my notes.txt"
    other.write_text("keep me")
    engine_mod.trim_pictures(str(tmp_path), keep=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        sorted([p.name for p in ours[3:]] + ["my notes.txt"])


def test_default_deps_wire_the_real_modules_without_doing_anything():
    from mayhem import inputs, window
    d = engine_mod.default_deps()
    assert d.window is window and d.inputs is inputs and d.rejoin is rejoin
    assert isinstance(d.ocr, Ocr) and isinstance(d.tail, LogTail)
    assert d.clock is inputs.clock and d.save_picture is engine_mod.save_picture
    assert d.now is time.monotonic


def test_engine_never_imports_network_or_process_modules():
    with open(engine_mod.__file__, encoding="utf-8") as fh:
        src = fh.read()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names} | {(node.module or "").split(".")[0]}
    assert not names & {"discord", "urllib", "socket", "http", "subprocess", "requests",
                        "ctypes", "stats"}


# ------------------------------------------------- real OCR on real frames ---
@pytest.fixture(scope="module")
def real_ocr():
    if not os.path.isdir(FRAMES):
        pytest.skip("fixture frames are not here")
    ocr = Ocr()
    if not ocr.available:
        pytest.skip(f"no Windows OCR: {ocr.error}")
    yield ocr
    ocr.close()


def _frame_rig(real_ocr, name: str) -> Rig:
    path = os.path.join(FRAMES, name)
    if not os.path.isfile(path):
        pytest.skip(f"{name} is missing")
    r = Rig()
    with Image.open(path) as img:
        r.window.frame = img.convert("RGB")
    r.deps.ocr = real_ocr
    return r.start()


def test_real_frame_resonance_banner_timer_and_storage(real_ocr):
    r = _frame_rig(real_ocr, "215059.jpg")
    r.tick()
    [ev] = r.of("rare")
    assert (ev["block"], ev["denominator"], ev["variant"], ev["source"]) == \
        ("Resonance", 100000, "normal", "banner")
    assert ev["picture"].size == CLIENT
    hud_ev = r.of("hud")[-1]
    assert hud_ev["timer"] == 511 and hud_ev["storage"] == [215, 350]


def test_real_frame_necromancer_banner_is_logged_not_alerted(real_ocr):
    r = _frame_rig(real_ocr, "215112.jpg")
    r.tick()
    assert r.of("rare") == []
    assert not any("Necromancer" in t for t in r.logs("info"))   # worker banners are noise now
    hud_ev = r.of("hud")[-1]
    assert hud_ev["timer"] == 499 and hud_ev["storage"] == [216, 350]


def test_real_gameplay_frame_is_not_taken_for_a_vote(real_ocr):
    r = _frame_rig(real_ocr, "215157.jpg")
    r.tick()
    assert r.of("hud")[-1]["timer"] == 453
    r.engine._start_vote(r.clock.t)                     # look for a vote on a normal frame
    r.tick(3)
    assert r.engine._vote is not None and not r.engine._vote.seen
    assert r.inputs.clicks() == [] and r.pictures == []


def test_anti_afk_variety_mixes_jumps_double_jumps_and_clicks():
    import random
    r = Rig(anti_afk=True, afk_variety=True).start()
    hows = []
    for seed in range(40):
        r.engine.rng = random.Random(seed)
        r.engine._afk_tried_at = -1e9
        r.engine._jump_at = -1e9
        r.tail.feed(IDLE(700.0 + seed))
        r.tick()
        evs = r.of("anti_afk")
        if evs:
            hows.append(evs[-1]["how"])
    assert {"jump", "double_jump", "click_jump"} <= set(hows)
    for _, x, y in r.inputs.clicks():                    # always inside the Roblox picture
        rx, ry, rw, rh = r.window.client_rect(r.window.hwnd)
        assert rx < x < rx + rw and ry < y < ry + rh


def test_pictures_only_for_rares_at_your_setting_and_problems():
    r = Rig(rare_min=1_000_000, save_pictures=True, picture_min=1).start()
    r.ocr.script["banner"] = ["Inverted Relic has spawned! (1 / 6,000,000)"]
    r.tick()
    r.ocr.script["banner"] = ["Chromatic Opal has spawned! (1 / 250,000)"]   # alerts (variant) but under 1M
    r.run(40)
    assert [tag for _, tag in r.pictures] == ["rare"]


def test_medal_clip_after_a_rare_spawn_at_your_setting():
    from mayhem import inputs as I
    assert I.parse_keys("F4") == [0x73] and I.parse_keys("ctrl+f9") == [0x11, 0x78]
    assert I.parse_keys("Win+F4") is None and I.parse_keys("") is None and I.parse_keys("F25") is None
    r = Rig(medal_clip=True, medal_key="F4", rare_min=1_000_000, medal_min=1_000_000).start()
    pressed = []
    r.inputs.press_keys = lambda key: pressed.append(key) or True
    r.ocr.script["banner"] = ["Opal has spawned! (1 / 10,000)"]          # under your setting: no clip
    r.run(5)
    assert pressed == []
    r.ocr.script["banner"] = ["Inverted Relic has spawned! (1 / 6,000,000)"]
    r.run(5)
    assert pressed == []                                                # not yet: 10 s after
    r.run(int(engine_mod.MEDAL_DELAY / engine_mod.TICK) + 2)
    assert pressed == ["F4"]
    assert any("Medal" in t for t in r.logs("info"))


@pytest.mark.parametrize("text, logged", [
    ("F agmint Frenzy Duplicated your Aqua loc f!", False),
    ("Fragment Frenzy Duplicated your Rare block!", False),
    ("z u Ilcatea your N", False),
    ("Void King has activated Ritual and generated a Rare block!", False),
    ("Kin has and generat d R", False),
    ("Domh•tor •ctiv•ted Golden Touch'", False),
    ("The worker summoning pool has refreshed!", False),
    ("World voting concluded! The new world is Dreamland,", False),
    ("A meteor shower is happening now!", False),     # no banner lines at all now
])
def test_only_clean_unknown_banners_are_logged(text, logged):
    r = Rig().start()
    r.ocr.script["banner"] = [text]
    r.tick()
    assert any(text[:20] in t for t in r.logs("info")) is logged


def test_no_pictures_are_saved_unless_switched_on():
    r = Rig(rare_min=100_000).start()
    r.ocr.script["banner"] = ["Inverted Relic has spawned! (1 / 6,000,000)"]
    r.run(5)
    assert r.of("rare") and r.pictures == []                 # alerted, nothing saved to files


def test_medal_clips_only_at_its_own_bar():
    r = Rig(medal_clip=True, rare_min=100_000).start()           # medal_min defaults to 10M
    pressed = []
    r.inputs.press_keys = lambda key: pressed.append(key) or True
    r.ocr.script["banner"] = ["Inverted Relic has spawned! (1 / 6,000,000)"]   # alerts, but under 10M
    r.run(5)
    assert r.of("rare") and pressed == []
    r.ocr.script["banner"] = ["Beyond has spawned! (1 / 10,000,000)"]
    r.run(30)
    assert pressed == ["F4"]


def test_esc_menu_left_open_gets_closed_when_you_are_away():
    r = Rig(close_esc_menu=True).start()
    r.ocr.script["client"] = ["Resume", "Reset Character", "Leave"]   # the menu, no top bar
    r.run(20)
    escs = [c for c in r.inputs.calls if c[0] == "scan" and c[1] == 0x01] or \
        [c for c in r.inputs.calls if c and 0x01 in c[1:2]]
    assert escs, r.inputs.calls[:5]
    assert any("Roblox menu" in t for t in r.logs("info"))


def test_esc_menu_is_left_alone_while_you_play():
    r = Rig(close_esc_menu=True).start()
    r.ocr.script["client"] = ["Resume", "Reset Character", "Leave"]
    for _ in range(20):
        r.clock.touch()
        r.tick()
    assert ("scan", 0x01) not in r.inputs.calls


def test_the_vote_starts_from_the_vote_screen_even_when_the_timer_was_never_read():
    r = Rig().start()
    r.ocr.script["timer"] = []                       # the round timer was never seen
    r.ocr.script["vote_title"] = ["Pick what map should be chosen next!"]
    r.run(6)
    assert r.engine._vote is not None


def test_no_vote_search_from_other_screens():
    r = Rig().start()
    r.ocr.script["timer"] = []
    r.ocr.script["vote_title"] = ["Loading Void...", "There are currently 5 different worlds"]
    r.run(6)
    assert r.engine._vote is None


def test_roblox_is_brought_to_the_front_at_the_start():
    r = Rig(front_hourly=True)
    r.window.fg = False                              # Discord in front
    r.start()
    r.tick()
    assert "front" in r.window.order and r.window.fg
    assert any("to the front" in t for t in r.logs("info"))


def test_covered_roblox_comes_back_once_you_are_away():
    r = Rig(front_hourly=True).start()
    r.tick()                                         # in front at the start: nothing
    assert "front" not in r.window.order
    r.window.fg = False                              # Discord pops over it
    r.clock.touch()                                  # you're typing somewhere else
    r.tick()
    assert "front" not in r.window.order
    r.clock.t += engine_mod.FRONT_IDLE + 1
    r.tick()
    assert "front" in r.window.order and r.window.fg


def test_front_is_left_alone_when_switched_off():
    r = Rig(front_hourly=False)
    r.window.fg = False
    r.start()
    r.run(5)
    assert "front" not in r.window.order


def test_play_is_pressed_when_a_run_starts_on_the_title_screen():
    r = Rig().start()
    r.ocr.script["timer"] = []                       # the title screen hides the round timer
    r.ocr.script["client"] = [OcrLine("PLAY", (OcrWord("PLAY", 300, 900, 120, 40),))]
    r.run(4)
    assert any("Pressed Play" in t for t in r.logs("info"))


def test_play_is_not_looked_for_while_the_round_timer_shows():
    r = Rig().start()
    r.ocr.script["timer"] = ["05:00"]
    r.ocr.script["client"] = [OcrLine("Play", (OcrWord("Play", 300, 900, 120, 40),))]
    r.run(int(engine_mod.PLAY_HUD_GONE / engine_mod.TICK) + 10)    # timer on screen the whole time
    assert not any("Pressed Play" in t for t in r.logs("info"))


def test_play_is_pressed_on_a_title_screen_long_after_the_start():
    """A rejoin that 'loaded' before the title screen came up: PLAY waits."""
    r = Rig().start()
    r.run(20)                                        # in the game, timer seen
    r.ocr.script["timer"] = []                       # title screen: no round timer
    r.ocr.script["client"] = [OcrLine("PIAY", (OcrWord("PIAY", 300, 900, 120, 40),))]
    r.run(int(engine_mod.PLAY_HUD_GONE / engine_mod.TICK) + 10)
    assert any("Pressed Play" in t for t in r.logs("info"))


def test_a_rare_seen_only_in_the_chat_is_clipped_too():
    r = Rig(medal_clip=True, medal_key="F4", medal_min=10_000_000).start()
    pressed = []
    r.inputs.press_keys = lambda key: pressed.append(key) or True
    r.run(2)                                         # the first chat read only learns what's there
    r.ocr.script["chat"] = ["Player1 has collected a Primorddk• with a 1 / 111,111,111 chance!"]
    r.run(int(engine_mod.MEDAL_DELAY / engine_mod.TICK) + 6)
    assert pressed == ["F4"]
    assert any("Collected Primordial (1 in 111,111,111)" in t for t in r.logs("info"))


def test_junk_banner_names_are_not_rares():
    r = Rig().start()
    r.ocr.script["banner"] = ["D has spawned!"]
    r.run(4)
    assert not r.of("rare")


def test_rare_pictures_are_saved_only_for_10m_and_rarer():
    r = Rig(save_pictures=True, rare_min=100_000).start()
    r.ocr.script["banner"] = ["Relic has spawned! (1 / 600,000)"]
    r.run(4)
    assert not [s for s in r.pictures if s[1] == "rare"]
    r.ocr.script["banner"] = ["Inverted Ignityx has spawned! (1 / 100,000,000)"]
    r.run(4)
    assert [s for s in r.pictures if s[1] == "rare"]


def test_a_spawn_named_in_the_roblox_log_alerts_with_its_variant():
    """Works with the screen off: Roblox logs broken textures of new blocks."""
    from mayhem import robloxlog
    ev = robloxlog.parse_line(
        '2026-10-24T04:51:00.889Z,5847.889160,7340,6 [FLog::Error] Asset (Image) "https://x" '
        'load failed in Workspace.Blocks.ignityx_chromatic.Base.core.core vfx.vfx.corefire.Texture: x')
    assert ev is not None and (ev.kind, ev.value) == ("block_spawn", "ignityx_chromatic")
    assert robloxlog.parse_line("2026-10-24T04:51:00.889Z,1,2,6 [FLog::Error] Workspace.Blocks.Board.Main.List") is not None


def test_a_log_named_spawn_becomes_a_rare_with_no_screen_at_all():
    r = Rig(rare_min=1_000_000).start()
    r.window.fg = False                                  # screen useless: only the log
    r.tail.feed(line('Asset (Image) "https://x" load failed in '
                     'Workspace.Blocks.ignityx_chromatic.Base.core.core vfx: x', "FLog::Error"))
    r.tail.feed(line('Asset (Image) "https://x" load failed in '
                     'Workspace.Blocks.ignityx_chromatic.Base.core.core vfx: y', "FLog::Error"))
    r.run(int(engine_mod.LOG_HOLD / engine_mod.TICK) + 4)
    [ev] = r.of("rare")
    assert (ev["block"], ev["variant"], ev["denominator"]) == ("Ignityx", "chromatic", 250_000_000)
    r.tail.feed(line("x in Workspace.Blocks.Board.Main.List", "FLog::Error"))   # not a block
    r.run(10)
    assert len(r.of("rare")) == 1
