"""Render every MayhemMacro page to PNGs, sandboxed and invisible.

    .venv\\Scripts\\python.exe tests\\render_ui.py [outdir] [state ...]

- MAYHEM_TESTING=1, MAYHEM_NO_SEND=1 and a fresh temp MAYHEM_DATA_DIR per
  state, all set before the app is imported: nothing reaches the real
  %LOCALAPPDATA%\\MayhemMacro, Discord, the keyboard hook or Roblox.
- The run is a FakeEngine: Start "runs" it, and events go in through the
  app's own emit() -> queue -> Tk path, exactly like the real engine's.
- The window is shown at alpha 0 with WS_EX_NOACTIVATE | WS_EX_TRANSPARENT |
  WS_EX_LAYERED, pushed to the bottom of the z-order, and shown with
  'wm state normal' (never 'deiconify', which forces focus). It is invisible,
  click-through and never meant to be active; a watchdog thread still checks
  every 3 ms and, if a window of THIS process ever gets the foreground,
  hands it straight back and reports it.
- Pictures come from PrintWindow(PW_RENDERFULLCONTENT), not the screen.
"""
import ctypes
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()),
                           "mayhem_renders")
OUT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    and os.path.isdir(os.path.dirname(os.path.abspath(sys.argv[1]))) else DEFAULT_OUT
WANT = sys.argv[2:] or None
os.makedirs(OUT, exist_ok=True)

os.environ["MAYHEM_TESTING"] = "1"
os.environ["MAYHEM_NO_SEND"] = "1"
os.environ["MAYHEM_DATA_DIR"] = tempfile.mkdtemp(prefix="mayhem-render-")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

u32 = ctypes.windll.user32
REAL_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "MayhemMacro")
REAL_BEFORE = os.path.exists(REAL_DATA)

# ---- focus watchdog: our window must never be the foreground one ----
TOOK: list[tuple[float, str]] = []           # (when, the step it happened in)
STEP = ["start"]
_last = [u32.GetForegroundWindow()]


def ours(h) -> bool:
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return bool(h) and pid.value == os.getpid()


def give_back() -> None:
    h = u32.GetForegroundWindow()
    if not h:
        return
    if not ours(h):
        _last[0] = h
        return
    TOOK.append((time.monotonic(), STEP[0]))
    if _last[0] and u32.IsWindow(_last[0]):
        u32.SetForegroundWindow(_last[0])     # only ever the user's own window back


def _watch() -> None:
    while True:
        try:
            give_back()
        except Exception:
            pass
        time.sleep(0.003)            # Tk can grab it while the first root is made


threading.Thread(target=_watch, daemon=True).start()

import tkinter                               # noqa: E402
import mayhemmacro as M                       # noqa: E402

assert M.safety.testing(), "MAYHEM_TESTING must be on"


class FakeEngine:
    """Stands in for mayhem.engine.Engine: start/stop/running, no thread,
    no input, no screen reading. The harness emits the events itself."""
    last = None

    def __init__(self, get_settings, emit):
        self.get_settings, self.emit = get_settings, emit
        self.running = False
        FakeEngine.last = self

    def start(self):
        self.running = True
        self.emit({"type": "state", "state": "running", "why": "Starting"})   # as Engine.start()

    def stop(self):
        self.running = False

    def tick(self):
        pass


def boot(settings=None, stats=None):
    """A fresh app on a fresh temp data folder, hidden before it is ever
    shown."""
    data = tempfile.mkdtemp(prefix="mayhem-render-")
    os.environ["MAYHEM_DATA_DIR"] = data
    if settings is not None:
        with open(os.path.join(data, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
    if stats is not None:
        with open(os.path.join(data, "stats.json"), "w", encoding="utf-8") as fh:
            json.dump(stats, fh)
    STEP[0] = "construct"
    app = M.MayhemMacro(engine_factory=FakeEngine)
    give_back()
    STEP[0] = "hide"
    app.withdraw()                           # CTk: never deiconify on update()
    app.attributes("-alpha", 0.0)
    app.update_idletasks()
    h = u32.GetParent(app.winfo_id()) or app.winfo_id()
    # WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
    u32.SetWindowLongW(h, -20, u32.GetWindowLongW(h, -20)
                       | 0x08000000 | 0x20 | 0x80000)
    # 'wm state normal' shows it WITHOUT raising or focusing (deiconify does both)
    STEP[0] = "show"
    tkinter.Tk.wm_state(app, "normal")
    # HWND_BOTTOM, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    u32.SetWindowPos(h, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    give_back()
    STEP[0] = "page"
    return app, data


def close(app):
    """The app's own close path, then its timers cancelled: one thread runs
    every Tk interpreter's timers, so a dead window's jobs would fire inside
    the next one ('invalid command name')."""
    destroy = app.destroy
    app.destroy = lambda: None                # _close ends with destroy()
    try:
        app._close()
    except Exception as exc:
        print("  close failed:", exc)
    try:
        for job in app.tk.splitlist(app.tk.call("after", "info")):
            app.tk.call("after", "cancel", job)
    except Exception:
        pass
    destroy()


def pump(app, n=20):
    for _ in range(n):
        app.update()
        give_back()
        time.sleep(0.03)


def grab(app, path):
    STEP[0] = "grab"
    pump(app)
    gdi = ctypes.windll.gdi32
    hwnd = u32.GetParent(app.winfo_id()) or app.winfo_id()

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    rc = RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.r - rc.l, rc.b - rc.t
    hdc = u32.GetWindowDC(hwnd)
    mem = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    gdi.SelectObject(mem, bmp)
    u32.PrintWindow(hwnd, mem, 2)            # PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(w * h * 4)

    class BMI(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("w", ctypes.c_int32),
                    ("h", ctypes.c_int32), ("planes", ctypes.c_uint16),
                    ("bits", ctypes.c_uint16), ("comp", ctypes.c_uint32),
                    ("x", ctypes.c_uint32 * 5)]
    bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0)
    gdi.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    from PIL import Image
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert(
        "RGB").save(path)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    u32.ReleaseDC(hwnd, hdc)
    return w, h


# ---- what a run looks like ----
def _log(level, text):
    return {"type": "log", "level": level, "text": text}


# What mayhem/engine.py sends during a run: a typed event, then its own
# plain-English Log line (copied from engine.py's wording).
RUN_EVENTS = [
    {"type": "state", "state": "running", "why": "In Block Mayhem X"},
    {"type": "hud", "timer": 511, "storage": [215, 350], "world": None},
    _log("info", "World is now Overworld."),
    {"type": "hud", "timer": 510, "storage": [215, 350], "world": "Overworld"},
    {"type": "rare", "block": "Resonance", "denominator": 100000, "variant": "normal",
     "source": "banner", "ping": False, "picture": None, "world": "Overworld"},
    _log("info", "Rare block: Resonance spawned (1 in 100,000)!"),
    {"type": "anti_afk", "idle": 612.0, "how": "jump"},
    _log("info", "Jumped so Roblox won't kick you for being idle (10 min)."),
    {"type": "vote", "options": ["Overworld", "Dreamland", "Lava"], "chose": "Dreamland",
     "ok": True},
    _log("info", "Voted for Dreamland."),
    _log("info", "World is now Dreamland. You voted Dreamland."),
    {"type": "hud", "timer": 598, "storage": [229, 350], "world": "Dreamland"},
    {"type": "rare", "block": "SNAKEBLOCK", "denominator": 505000, "variant": "normal",
     "source": "log_song", "ping": False, "picture": None, "world": "Dreamland"},
    _log("info", "Rare block: SNAKEBLOCK appeared (1 in 505,000)!"),
    _log("warn", "Disconnected from the server (code 277)."),
    {"type": "state", "state": "waiting", "why": "Disconnected"},
    _log("warn", "You got disconnected (code 277). I'll rejoin once you've been away "
                 "from the PC for a minute."),
    {"type": "rejoin", "phase": "start", "try": 1, "why": "You got disconnected (code 277)"},
    _log("info", "Rejoining (try 1 of 5)..."),
    {"type": "state", "state": "rejoining", "why": "Rejoining (try 1 of 5)"},
    _log("info", "Opened Roblox, waiting for it to join."),
    _log("info", "Joined the server, waiting for the game to load."),
    {"type": "rejoin", "phase": "ok", "try": 1, "why": "You got disconnected (code 277)"},
    _log("info", "Back in Block Mayhem X."),
    {"type": "state", "state": "running", "why": "In Block Mayhem X"},
    {"type": "rare", "block": "Relic", "denominator": 50000, "variant": "inverted",
     "source": "banner", "ping": False, "picture": None, "world": "Dreamland"},
    _log("info", "Rare block: Inverted Relic spawned (1 in 50,000)!"),
    {"type": "anti_afk", "idle": 605.0, "how": "jump"},
    _log("info", "Jumped so Roblox won't kick you for being idle (10 min)."),
    _log("info", "The anti-AFK jump worked: Roblox's idle timer reset."),
    {"type": "anti_afk", "idle": 610.0, "how": "jump"},
    _log("info", "Jumped so Roblox won't kick you for being idle (10 min)."),
    {"type": "hud", "timer": 511, "storage": [350, 350], "world": "Dreamland"},
    {"type": "storage_full", "n": 350, "m": 350},
    _log("warn", "Storage is full (350/350). New blocks won't fit until you make room."),
]


def fake_run(app, events=RUN_EVENTS, hours=2, mins=14):
    STEP[0] = "run"
    app.toggle_run()
    eng = FakeEngine.last
    assert eng is not None and eng.running and app.running
    for ev in events:
        eng.emit(ev)
    pump(app, 10)
    # an older start time, so the run time reads like a real session
    app.session_start = time.time() - (hours * 3600 + mins * 60)
    app._toast_hide()
    pump(app, 4)
    return eng


def sample_stats():
    """A believable stats.json: three weeks of evenings."""
    today = datetime.date.today()
    hours = [0, 2.5, 3.1, 0, 4.2, 5.2, 1.0, 0, 2.2, 3.8, 0.5, 4.6, 2.9, 1.4,
             3.3, 0, 2.0]
    days = {(today - datetime.timedelta(days=len(hours) - 1 - i)).isoformat():
            h * 3600 for i, h in enumerate(hours) if h}
    total = sum(days.values())
    return {"v": 1, "total_secs": total, "runs": 23, "longest_secs": 5.2 * 3600,
            "longest_on": (today - datetime.timedelta(days=11)).isoformat(),
            "rares": 41, "votes": 212, "anti_afk": 388, "rejoins": 9,
            "rejoin_fails": 2, "errors": 1, "storage_full": 6, "unclean_ends": 1,
            "first_run": (today - datetime.timedelta(days=16)).isoformat() + " 18:02",
            "last_run": today.isoformat() + " 17:40", "days": days, "current": None}


# A made-up share link and webhook with the right shape only. Never real.
FAKE_LINK = "https://www.roblox.com/share?code=1f2e3d4c5b6a7980aabbccddeeff0011&type=Server"
FAKE_HOOK = ("https://discord.com/api/webhooks/123456789012345678/"
             + "Fake-Token_" + "x" * 57)
SET_VOTES = {"vote_maps": ["Dreamland", "Arctic", "Lava"]}

STATES = {}


def state(fn):
    STATES[fn.__name__] = fn
    return fn


def _page(key, settings=None, stats=None, run=False):
    app, data = boot(settings, stats)
    if run:
        fake_run(app)
    app._show_page(key)
    return app, data


@state
def home_ready():
    return _page("home")


@state
def home_running():
    return _page("home", settings=SET_VOTES, run=True)


@state
def home_stopped():
    app, data = _page("home", run=True)
    app.toggle_run()
    pump(app, 12)
    return app, data


@state
def page_activity():
    return _page("activity", run=True)


@state
def page_activity_rares():
    app, data = _page("activity", run=True)
    app.seg_feed.set("Rares")
    app._set_feed_filter("Rares")
    return app, data


@state
def page_alerts():
    return _page("alerts", run=True)


@state
def page_vote_empty():
    return _page("vote")


@state
def page_vote():
    return _page("vote", settings=SET_VOTES)


@state
def page_server():
    return _page("server")


@state
def page_server_saved():
    return _page("server", settings={"private_server_link": FAKE_LINK})


@state
def page_discord():
    return _page("discord")


@state
def page_discord_saved():
    """The masked view of a saved link - in memory only, never written."""
    app, data = _page("discord")
    app._save_settings = lambda *a, **k: None      # nothing reaches the sandbox
    cfg = app._hook_settings()
    cfg["webhook"], cfg["enabled"] = M.D.normalize_hook(FAKE_HOOK), True
    app._paint_hook()
    return app, data


@state
def page_stats():
    return _page("stats", stats=sample_stats())


@state
def page_stats_empty():
    return _page("stats")


@state
def page_log():
    app, data = _page("log", run=True)
    eng = FakeEngine.last
    eng.emit(_log("error", "Something went wrong while reading the screen (OSError: "
                           "screen grab failed). The macro keeps going."))
    eng.emit(_log("info", 'Banner: "Necromancer has activated Ritual and generated a '
                          'Epic block!"'))
    pump(app, 6)
    return app, data


@state
def page_settings():
    return _page("settings")


@state
def page_about():
    return _page("about")


@state
def rail():
    """Narrow window: the sidebar folds to the 56px icon rail."""
    app, data = _page("home", run=True)
    app.geometry("780x560")
    pump(app, 6)
    return app, data


@state
def smallest():
    app, data = _page("home", run=True)
    mw, mh = tkinter.Tk.wm_minsize(app)
    app.geometry(f"{mw}x{mh}")
    pump(app, 6)
    return app, data


@state
def interactions():
    """Every page handler the pictures don't reach, called directly: no real
    clicks, no clipboard, no pin (topmost), no network. Each result is
    checked, then the Discord page is drawn as it ends up."""
    app, data = _page("server")
    sent = []

    class NoPost:                             # Send test without a sender
        def __init__(self, url, log=None):
            sent.append(url)
            self._hurry = False

        def post_now(self, payload, picture=None):
            return "blocked"
    M.D.WebhookSender = NoPost
    for e in (app.e_link, app.e_hook, app.e_uid, app.e_log):
        e.focus_set = lambda: None            # never move the keyboard focus
    s = app.settings
    # Server: save, mask, change, clear; the AFK minutes stepper
    app._entry_set(app.e_link, FAKE_LINK)
    app._link_validate()
    app._link_save()
    assert s["private_server_link"] == FAKE_LINK
    assert "1f2e3d4c5b6a7980" not in app.e_link.get()       # shown masked
    app._link_change()
    app._link_clear()
    assert s["private_server_link"] == ""
    app._entry_set(app.e_link, "https://example.com/not-roblox")
    app._link_validate()
    assert app.btn_link_a.cget("state") == "disabled"
    for _ in range(20):
        app._afk_step(1)
    assert s["anti_afk_after_min"] == 15
    app._afk_step(-1)
    assert s["anti_afk_after_min"] == 14
    # World vote: tick, rank, untick
    for name in ("Void", "Lava", "Arctic"):
        app.chk_map[name].select()
        app._map_ticked(name)
    app._map_move("Arctic", -1)
    app.chk_map["Void"].deselect()
    app._map_ticked("Void")
    assert s["vote_maps"] == ["Arctic", "Lava"]
    # Alerts: the rarity menus
    app._rarity_picked("rare_min", "1 in 1,000,000")
    assert s["rare_min"] == 1_000_000
    app.sw_storage.deselect()
    app._switch_saved("storage_alert", app.sw_storage)
    assert s["storage_alert"] is False
    # Discord: save a link, switch on, pick alerts, user id, Send test, remove
    app._entry_set(app.e_hook, FAKE_HOOK)
    app._hook_validate()
    app._hook_save()
    assert s["discord"]["webhook"] == M.D.normalize_hook(FAKE_HOOK)
    app.sw_hook.select()
    app._hook_switched()
    assert s["discord"]["enabled"] is True
    app.chk_hook["hourly"].select()
    app._hook_options_changed()
    assert s["discord"]["send"]["hourly"] is True
    app._entry_set(app.e_uid, "123456789012345678")
    app._uid_changed()
    assert s["discord"]["user_id"] == "123456789012345678"
    app._hook_test()
    pump(app, 10)
    assert sent and "blocked" in app.lbl_hook_test.cget("text").lower() or \
        "switched off" in app.lbl_hook_test.cget("text")
    # Log: filter and the Problems view
    app.log("a problem line", "warn")
    app._set_log_mode("Problems")
    app._entry_set(app.e_log, "problem")
    app._log_query_changed()
    pump(app, 8)
    assert app._log_shown == 1
    app._set_log_mode("All")
    app._entry_set(app.e_log, "")
    app._log_query_changed()
    # Stats: reset (two clicks), with a run on record
    app.session.stats["runs"] = 3
    M.S.save_stats(app.session.stats)                     # the temp folder's stats.json
    app._reset_stats_click()
    app._reset_stats_click()
    assert app.session.stats["runs"] == 0
    assert any(f.startswith("stats.json.reset-") for f in os.listdir(data))
    # keys: pages and the sidebar (not the pin: that would make it topmost)
    app._key_page("stats")
    app._key_cycle(1)
    assert app._page == "log"
    app._key_sidebar()
    assert app._rail and s["ui"]["sidebar_collapsed"] is True
    app._key_sidebar()
    assert not app._rail
    app._key_escape()
    app._show_page("discord")
    pump(app, 10)
    # the debounced save reached the temp settings.json
    with open(os.path.join(data, "settings.json"), encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["vote_maps"] == ["Arctic", "Lava"] and saved["rare_min"] == 1_000_000
    app._save_settings = lambda *a, **k: None
    return app, data


@state
def toast_rare():
    app, data = _page("home", run=True)
    app._toast("Resonance spawned! · 1 in 100,000", "rare")
    app.after_cancel(app._toast_job)          # hold it for the picture
    app._toast_job = None
    return app, data


def main():
    names = WANT or list(STATES)
    fg0 = u32.GetForegroundWindow()
    stolen, errors = [], []
    for name in names:
        app, data = STATES[name]()
        w, h = grab(app, os.path.join(OUT, f"{name}.png"))
        print(f"{name:22} {w}x{h}")
        if TOOK:
            stolen.append(name)
            print(f"  !! {name}: our window got the foreground "
                  f"{len(TOOK)}x (during: {', '.join(sorted({s for _, s in TOOK}))})"
                  f" - handed back")
            TOOK.clear()
        close(app)
        # errors the app swallowed (Tk callbacks, events) end up in log.txt
        try:
            with open(os.path.join(data, "log.txt"), encoding="utf-8") as fh:
                bad = [ln for ln in fh if "window error" in ln or "event not shown" in ln]
        except OSError:
            bad = []
        if bad:
            errors.append(name)
            print("  !! swallowed errors:", "".join(bad)[:2000])
        shutil.rmtree(data, ignore_errors=True)
    print("pictures in:", OUT)
    print("focus unchanged:", u32.GetForegroundWindow() == fg0)
    print("never had focus:", not stolen, stolen or "")
    real_after = os.path.exists(REAL_DATA)
    print("real data folder untouched:", real_after == REAL_BEFORE)
    print("no swallowed errors:", not errors, errors or "")
    assert real_after == REAL_BEFORE, "a render touched %LOCALAPPDATA%\\MayhemMacro"


if __name__ == "__main__":
    main()
