"""The run loop: one worker thread that keeps the player in Block Mayhem X.

About twice a second tick() reads what is new in the Roblox log, looks at the
Roblox window and, when it is allowed to, does ONE thing: an anti-AFK jump, a
world-vote click or a rejoin. It never talks to Discord and never writes a file
itself (pictures go through deps.save_picture). Everything it notices goes out
as a plain dict through `emit`, and the app turns those into alerts, stats and
the activity feed - one place that applies the user's alert switches.

Every side effect sits in Deps, so the tests swap in fakes and nothing real
happens. The rules from DESIGN.md this file is built around:
- Keys and clicks only go to Roblox while it is the window in front, the
  session is Block Mayhem X, and the user hasn't touched the PC for 5 s.
- Disruptive things (closing and opening Roblox, pulling it to the front) wait
  until the user has been away for a whole minute.
- Anti-AFK, the vote click and a rejoin never overlap: one action lock.
- tick() never raises. A problem is logged once and the loop carries on.
"""
import math
import os
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from PIL import Image

from . import MAPS, PLACE_ID, blocks, hud, paths, vote
from .ocr import Ocr
from .robloxlog import LogEvent, LogTail

if TYPE_CHECKING:
    from .window import InputClock

_NEVER = float("-inf")

# ------------------------------------------------------------------ timing ---
TICK = 0.5                  # one look every half second
USER_BUSY_LEAVE = 30.0                  # left BMX within this many s of your input = on purpose
AFK_CLICK_AT = (0.5, 0.42)              # client fractions: open game view, clear of the HUD panels
AFK_JITTER = 60.0                       # with variety on, act up to this many s before the threshold
VOTE_BUTTON_DY = 0.064                  # map name -> VOTE button centre, share of client height (65 px at 1009)
CLICK_INSET = 4                         # px kept clear of the Roblox picture's edges
INPUT_IDLE = 5.0            # the user hasn't touched the PC this long: keys and clicks are OK
AWAY_IDLE = 60.0            # ...and this long means away: rejoin and focus changes are OK

# The screen. The top HUD read costs 50-100 ms, so not every tick.
TOP_EVERY = 2.0             # vote timer + storage this often
TOP_EVERY_LOW = 0.5         # ...and this often once the timer is low
TIMER_LOW = 15              # seconds left that count as "low"
TIMER_STALE = 20.0          # a timer reading older than this says nothing any more
BANNER_EVERY = 1.0
STORAGE_STALE = 60.0        # storage not read for this long: show nothing

# Rare blocks.
BANNER_DEDUPE = 30.0        # a banner stays up a few seconds: one alert, not five
RARE_MERGE = 60.0           # a banner and a log line for the same block = one alert
CHROMA_MERGE = 15 * 60.0    # the chromatic log line only comes minutes after the block
LOG_HOLD = 3.0              # with the screen readable, give the banner this long to beat the log line
BANNER_PATIENCE = 3.0       # wait this long for a read of a banner that shows its rarity
SETUP_IDLE = 1.5            # start-of-run setup waits this long after your last input (the F1 press)
PLAY_EVERY = 1.5            # look for the title screen's PLAY button this often while loading
MEDAL_DELAY = 2.0           # press the clip key this long after the spawn (so the clip shows it)
MEDAL_GAP = 20.0            # at most one clip this often
ESC_CHECK_EVERY = 3.0       # look for Roblox's Esc menu this often...
ESC_HUD_LOST = 6.0          # ...once the game's top bar has been hidden this long
ESC_ANYWAY = 20.0           # and this often even when the top bar still reads (dim menu overlay)
ESC_GAP = 4.0               # at most one Esc press this often
SCAN_ESC = 0x01
CHAT_EVERY = 2.0            # read the chat for 'has collected' lines this often
FIND_QUIET = 120.0          # the same find again only after it was gone this long
BANNER_TEXT_QUIET = 600.0   # write an unknown banner text to the log at most this often
BANNER_TEXT_WORDS = 4       # shorter scraps are scenery, not banners
# The game's own banners that mean nothing for the macro (worker abilities,
# the vote result, the worker pool): never logged. Letters only, lower case,
# short enough to survive OCR slips.
KNOWN_BANNERS = ("ritual", "generat", "frenzy", "duplicat", "golden", "touch", "summoning",
                 "votingconclud", "newworld", "pickedup", "workerpool", "activated")


def _reads_clean(text: str) -> bool:
    """OCR read it properly: real words only (letters, digits, normal
    punctuation), no stray symbols, no glued-together words."""
    if re.search(r"[^A-Za-z0-9 ,.!?'()/:-]", text):
        return False
    words = [w.strip(",.!?'():") for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return False
    good = sum(1 for w in words if re.fullmatch(r"[A-Za-z]+|[0-9][0-9,]*|[0-9]+/[0-9,]+", w)
               and not re.search(r"[a-z][A-Z]", w) and len(w) <= 14
               and (len(w) > 1 or w in ("a", "A", "I") or w.isdigit()))
    return good / len(words) >= 0.9

# Anti-AFK. Roblox kicks after 20 idle minutes.
AFK_MIN, AFK_MAX = 2, 15    # minutes: anti_afk_after_min is kept inside this
IDLE_FRESH = 150.0          # idle lines come every 30 s while idle; none this long = none coming
IDLE_LAG = 3.0              # an idle line read this soon after a jump was written before it
BEHIND_IDLE = 15 * 60.0     # Roblox behind other windows: act (or warn) at this much idle
AFK_RETRY = 30.0            # at least this long between two jump attempts
FRONT_SETTLE = 0.4          # after pulling Roblox forward, before the jump
FRONT_AFTER = 0.2           # after the jump, before putting the user's window back

# The world vote (every 10 minutes: three map cards, each with a green VOTE button).
VOTE_AT = 5                 # start looking when the timer says this many seconds or less
VOTE_SCAN = 45.0            # look for the vote screen this long
VOTE_RECHECK = 2.0          # after a click, look again this much later
VOTE_COOLDOWN = 90.0        # no new vote search this soon after one ended
VOTE_SCALE = 1.25           # whole-client OCR: 1.0-1.5 keeps it under ~250 ms
VOTE_MISSES = 3             # click_at said no this often: give up on this vote
FRONT_EVERY = 3600.0        # bring Roblox to the front this often (and at the start)
FRONT_IDLE = 60.0           # ...the hourly one only with you away from the keys this long
VOTE_TITLE_AFTER = 2.0      # round timer gone this long: look for the vote screen itself
VOTE_TITLE_EVERY = 2.0      # ...this often
VOTE_NOTE = 180.0           # a world change this soon after voting mentions the vote

STORAGE_FULL_FOR = 60.0     # full this long before the alert

# Rejoin.
NO_WINDOW_AFTER = 20.0      # no Roblox window this long = it closed or crashed
NOT_BMX_AFTER = 60.0        # not in a BMX session this long = left, or another game
JOIN_WAIT = 120.0           # after launching: a new log with the BMX join, this soon
LOAD_WAIT = 60.0            # ...then the game's GUI loaded, this soon
BACKOFF = (30.0, 60.0, 120.0, 300.0)
MAX_FAILS = 5               # failed tries in a row, then the run stops
MAX_PER_HOUR = 6            # launches per hour, so a broken server can't crash-loop
HOUR = 3600.0

KEEP_PICTURES = 100         # vote and rare pictures kept in events\
_PICTURE_NAME = re.compile(r"^\d{8}_\d{6}_\d{3}_[a-z0-9_-]+\.jpg$")


# -------------------------------------------------------------------- deps ---
@dataclass
class Deps:
    """Everything with a side effect, swappable in tests."""
    window: Any                     # roblox_hwnd, roblox_running, is_foreground, is_minimized,
                                    # client_rect, grab, bring_to_front, restore_front
                                    # (fullscreen_app_in_front, screen_size if there)
    ocr: Ocr
    inputs: Any                     # press_space, click_at, move_to
    rejoin: Any                     # deep_links, kill_roblox, launch (validate_link if there)
    tail: LogTail
    clock: "InputClock"
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    save_picture: Callable[[Image.Image, str], str | None] | None = None


def trim_pictures(folder: str, keep: int = KEEP_PICTURES) -> None:
    """Delete all but the newest `keep` pictures this file saved. Only files
    named the way save_picture() names them are touched. Never raises."""
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if _PICTURE_NAME.match(f)]
        files = [f for f in files if os.path.isfile(f)]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def save_picture(img: Image.Image, tag: str) -> str | None:
    """Save `img` as events\\<time>_<tag>.jpg in the data folder (made on first
    use). Returns the file name, or None. Never raises: a picture is not worth
    stopping the run for."""
    try:
        folder = paths.events_dir()
        os.makedirs(folder, exist_ok=True)
        safe = re.sub(r"[^a-z0-9_-]", "", str(tag).lower())[:20] or "event"
        stamp = time.time()
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(stamp)) + \
            f"_{int(stamp * 1000) % 1000:03d}_{safe}.jpg"
        img.convert("RGB").save(os.path.join(folder, name), "JPEG", quality=92)
        trim_pictures(folder)
        return name
    except Exception:
        return None


def default_deps() -> Deps:
    """The real thing. Only the app builds these; tests always pass fakes."""
    from . import inputs, rejoin, window
    return Deps(window=window, ocr=Ocr(), inputs=inputs, rejoin=rejoin, tail=LogTail(),
                clock=inputs.clock, now=time.monotonic, sleep=time.sleep,
                save_picture=save_picture)


# --------------------------------------------------------------- settings ---
def _flag(st: dict, key: str) -> bool:
    v = st.get(key, paths.DEFAULT_SETTINGS[key])
    return v if isinstance(v, bool) else bool(paths.DEFAULT_SETTINGS[key])


def _int(st: dict, key: str, lo: int | None = None, hi: int | None = None) -> int:
    """A whole-number setting, the default when it is missing or junk."""
    default = paths.DEFAULT_SETTINGS[key]
    v = st.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        v = default
    v = int(v)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _ranking(st: dict) -> list[str]:
    """vote_maps, the ticked maps in rank order. Anything but a list = none."""
    maps = st.get("vote_maps")
    return [str(m) for m in maps] if isinstance(maps, list) else []


def afk_after(st: dict) -> float:
    """anti_afk_after_min in seconds, kept to 2-15 minutes: past 15 a missed
    jump or two would run into Roblox's 20-minute kick."""
    return 60.0 * _int(st, "anti_afk_after_min", AFK_MIN, AFK_MAX)


_LINK = re.compile(r"(?i)\b(?:roblox|https?)://[^\s'\"<>]+")
_CODE = re.compile(r"(?i)\b((?:privateserver)?linkcode|code|accesscode)=[^&\s'\"<>]+")


def _scrub(text: str) -> str:
    """Error text can carry what failed: os.startfile puts the whole roblox://
    link, private server code and all, in its message. No link reaches the log."""
    return _CODE.sub(r"\1=…", _LINK.sub("<link>", text))


def _key(name: str) -> str:
    """'Snake Block', 'SNAKEBLOCK' and 'snakeblock' are the same block."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _mins(secs: float) -> str:
    m = int(secs // 60)
    return f"{m} min" if m else f"{int(secs)} s"


# ------------------------------------------------------------------ state ---
@dataclass
class _Look:
    """What the window and the user are doing this tick."""
    hwnd: int
    fg: bool
    minimized: bool
    in_bmx: bool
    user_idle: float

    @property
    def readable(self) -> bool:
        """Screen reading is allowed: Roblox in front, not minimised, in BMX."""
        return bool(self.hwnd) and self.fg and not self.minimized and self.in_bmx


@dataclass
class _Idle:
    value: float                    # what the line said
    at: float                       # when we read it
    seq: int


@dataclass
class _Vote:
    started: float
    looks: int = 0                  # whole-client reads so far
    seen: bool = False              # a vote screen was on screen at some point
    options: list[str] = field(default_factory=list)
    chose: str | None = None
    clicks: int = 0                 # clicks that went through
    misses: int = 0                 # click_at said no
    clicked_at: float = 0.0
    before: int | None = None       # the chosen card's vote count at the first click
    busy_noted: bool = False
    img: Any = None                 # the vote screen, kept for a bug picture if the vote fails


@dataclass
class _Rejoin:
    why: str
    since: float
    fails: int = 0                  # failed tries in a row
    tries: int = 0                  # the try in progress (or the last one)
    phase: str = "wait"             # wait | confirm | loading
    next_at: float = 0.0            # backoff: no try before this
    old_log: str | None = None
    launched_at: float = 0.0
    entered_at: float = 0.0


# ----------------------------------------------------------------- engine ---
def _better_read(new: hud.Banner, old: hud.Banner) -> bool:
    """Full-size beats shrunken; a read with the number beats one without;
    a variant word ('Inverted') beats none."""
    rank = lambda b: (not b.small, b.denominator is not None, b.variant != "normal")
    return rank(new) > rank(old)


class Engine:
    """The run loop. start() runs tick() on one worker thread until stop()."""

    def __init__(self, get_settings: Callable[[], dict], emit: Callable[[dict], None],
                 deps: Deps | None = None):
        self.get_settings = get_settings
        self._emit_to = emit
        self.deps = deps if deps is not None else default_deps()
        self.running = False
        self.state = "stopped"
        self.why = ""
        # One tick at a time, whoever calls it. Re-entrant: stop() can be
        # called from inside a tick (a fake's callback in the tests).
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen = 0                           # bumped every start, so an old worker bows out
        self._finished = True
        # Launch times for the hourly cap. Kept across runs on purpose: Stop
        # and Start again must not reset it.
        self._launches: deque[float] = deque()
        self.rng = random.Random()              # anti-AFK variety; tests seed it
        self._afk_early = 0.0
        self._reset_run()

    # -- run control ----------------------------------------------------------
    def listen(self, get_settings: Callable[[], dict], emit: Callable[[dict], None]) -> None:
        """Point the engine at the next run's settings and listener. The app
        keeps ONE engine for its whole life (the hourly rejoin cap lives here)
        and calls this before each start(). Waits for a tick still under way,
        so an old run's last events still go to the old listener."""
        with self._lock:
            self.get_settings = get_settings
            self._emit_to = emit

    def start(self, thread: bool = True) -> None:
        """Read the log so far (without alerting on any of it) and start the
        worker. thread=False only primes: the caller then calls tick() itself."""
        with self._lock:
            if self.running:
                return
            self._gen += 1
            gen = self._gen
            self._stop_evt = threading.Event()
            self._reset_run()
            with self._state_lock:
                self._finished = False
                self.state = ""
            self.running = True
            try:
                self.deps.tail.prime()
            except Exception as e:
                self._error("reading the Roblox log", e)
            # Roblox already in some other game when the run starts: that game
            # is the user's, never a reason to close it for a rejoin.
            place = getattr(self.deps.tail, "place", None)
            self._start_elsewhere = bool(place) and str(place) != PLACE_ID
            self._set_state("running", "Starting")
        if thread:
            self._thread = threading.Thread(target=self._loop, args=(gen, self._stop_evt),
                                            name="MayhemMacro engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the run. Waits up to 3 s for the worker to finish its tick."""
        self._stop_evt.set()
        was_running, self.running = self.running, False
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(3.0)
        if was_running:
            # A banner still waiting for a better read goes out now, not never.
            got = self._lock.acquire(timeout=1.0)
            try:
                self._step("sending a rare block alert", self._send_waiting,
                           self.deps.now(), self._settings())
            finally:
                if got:
                    self._lock.release()
        self._finish("Stopped")

    def _loop(self, gen: int, stop_evt: threading.Event) -> None:
        while not stop_evt.is_set():
            t0 = time.monotonic()
            self._tick(gen)
            if stop_evt.wait(max(0.05, TICK - (time.monotonic() - t0))):
                break

    def _halt(self, why: str) -> None:
        """The run stops itself (rejoin gave up). Called from inside a tick."""
        self.running = False
        self._stop_evt.set()
        self._step("sending a rare block alert", self._send_waiting, self.deps.now(),
                   self._settings())
        self._finish(why)

    def _finish(self, why: str) -> None:
        with self._state_lock:
            if self._finished:
                return
            self._finished = True
            self._busy = None
            self._vote = None
            self.state, self.why = "stopped", why
        self._emit({"type": "state", "state": "stopped", "why": why})

    def tick(self) -> None:
        """ONE iteration. The worker calls it in a loop; tests call it directly."""
        self._tick(None)

    def _tick(self, gen: int | None) -> None:
        with self._lock:
            if not self.running or (gen is not None and gen != self._gen):
                return
            try:
                self._tick_body()
            except Exception as e:      # every step has its own guard; this is the last one
                self._error("running", e)

    def _tick_body(self) -> None:
        now = self.deps.now()
        st = self._settings()
        if self._first:
            self._first = False
            self._step("starting up", self._hello, st)
        disconnects = self._step("reading the Roblox log", self._read_log, now, st) or []
        if not self.running:
            return
        look = self._step("looking at the Roblox window", self._look, now)
        if look is None:
            return
        if self._start_elsewhere:
            self._start_elsewhere = False
            place = getattr(self.deps.tail, "place", None)
            if look.hwnd and place and str(place) != PLACE_ID:
                # Started (maybe by a key another program also uses) while
                # Roblox is in some other game: never close that game.
                self._halt("Roblox is in another game, so I stopped. Join Block Mayhem X, "
                           "then start again.")
                return
        self._step("rejoining", self._rejoin, now, st, look, disconnects)
        if not self.running:
            return
        self._step("bringing Roblox to the front", self._keep_front, now, st, look)
        self._step("reading the screen", self._screen, now, st, look)
        self._step("getting the game ready", self._setup, now, st, look)
        self._step("clipping with Medal", self._medal, now, st, look)
        self._step("closing the Roblox menu", self._esc_menu, now, st, look)
        self._step("looking for the map vote", self._vote_title, now, st, look)
        self._step("doing the anti-AFK jump", self._anti_afk, now, st, look)
        self._step("sending a rare block alert", self._flush_log_rares, now, st, look)
        self._step("updating the HUD", self._send_hud, now)
        self._step("updating the status", self._update_state, now, st, look)

    # -- plumbing -------------------------------------------------------------
    def _reset_run(self) -> None:
        """Fresh per-run state."""
        self._first = True
        self._esc_at = self._esc_pressed_at = _NEVER
        self._title_at = _NEVER
        self._front_at = _NEVER
        self._clip_at: float | None = None      # when to press the Medal clip key
        self._clip_last = _NEVER
        self._play_at = _NEVER
        self._start_elsewhere = False           # Roblox was in another game at Start
        self._setup_done = False                # start-of-run setup (pinned recipe, cursor)
        self._setup_idle = SETUP_IDLE           # how long you must be still before it runs
        self._setups = 0
        self._joined_at = _NEVER                # the latest join line this run
        self._chat_at = _NEVER
        self._chat_primed = False               # the first chat read only learns what's there
        self._finds_seen: dict[str, float] = {}
        self._errors: set[str] = set()
        self._busy: str | None = None           # the action lock: None | vote | rejoin | anti_afk
        # What the screen said.
        self._timer_val: int | None = None
        self._timer_at = _NEVER
        self._lost_low = False
        self._top_at = _NEVER
        self._banner_at = _NEVER
        self._storage: tuple[int, int] | None = None
        self._storage_at = _NEVER
        self._full_since: float | None = None
        self._full_seen_at = _NEVER
        self._full_alerted = False
        self._timer_low_seen = _NEVER           # last time the timer read a minute or less
        self.world: str | None = None
        self._world_at = _NEVER                 # when the last world_music line came
        self._world_said_at = _NEVER            # when a banner last said the new world
        self._hud_sent: tuple | None = None
        self._blind_noted = False
        # Rare blocks.
        self._banner_seen: dict[str, tuple[float, str]] = {}    # key -> (last read, variant)
        self._chroma_seen: dict[str, float] = {}                # key -> chromatic sighting
        self._banner_wait: dict[str, tuple[hud.Banner, float, Image.Image | None]] = {}
        self._banner_texts: dict[str, float] = {}
        self._sighted: dict[str, tuple[float, str, str]] = {}    # key -> (when, source, variant)
        self._log_wait: dict[str, tuple[float, dict]] = {}       # key -> (until, rare kwargs)
        # The log.
        self._dc_noted = False
        self._dc_reason = ""
        # Anti-AFK.
        self._idle: _Idle | None = None
        self._idle_seq = 0
        self._jump_at = _NEVER
        self._jump_idle = 0.0
        self._jump_seq = 0
        self._jump_checked = True
        self._afk_tried_at = _NEVER
        self._afk_fail_noted = False
        self._afk_miss_noted = False
        self._behind_noted = False
        # The vote.
        self._vote: _Vote | None = None
        self._vote_quiet_until = _NEVER
        self._voted: tuple[float, str] | None = None
        # Rejoin.
        self._rj: _Rejoin | None = None
        self._no_window_since: float | None = None
        self._not_bmx_since: float | None = None
        self._off_noted = ""
        self._cap_noted = False

    def _emit(self, ev: dict) -> None:
        try:
            self._emit_to(ev)
        except Exception:
            pass                        # a broken listener must not stop the run

    def _log(self, level: str, text: str) -> None:
        self._emit({"type": "log", "level": level, "text": text})

    def _error(self, what: str, e: BaseException) -> None:
        """Log a problem once. The same problem again stays quiet, so a broken
        screen read can't fill the log twice a second."""
        text = _scrub(str(e).strip())
        msg = text.splitlines()[0][:120] if text else ""
        key = f"{what}|{type(e).__name__}|{msg}"
        if key in self._errors:
            return
        if len(self._errors) < 200:
            self._errors.add(key)
        detail = type(e).__name__ + (f": {msg}" if msg else "")
        self._log("error", f"Something went wrong while {what} ({detail}). The macro keeps going.")

    def _step(self, what: str, fn: Callable, *args):
        try:
            return fn(*args)
        except Exception as e:
            self._error(what, e)
            return None

    def _set_state(self, state: str, why: str) -> None:
        with self._state_lock:
            if self._finished or (state, why) == (self.state, self.why):
                return
            self.state, self.why = state, why
        self._emit({"type": "state", "state": state, "why": why})

    def _settings(self) -> dict:
        try:
            st = self.get_settings()
        except Exception as e:
            self._error("reading the settings", e)
            st = None
        return st if isinstance(st, dict) else {}

    def _hello(self, st: dict) -> None:
        """First tick: say up front what won't work, instead of failing quietly."""
        ocr = self.deps.ocr
        if not getattr(ocr, "available", True):
            why = getattr(ocr, "error", "") or "Text reading isn't working."
            self._log("warn", f"Can't read the screen. {why} Rare banners and the world vote "
                              "won't work, but anti-AFK and rejoin still do.")
        if _flag(st, "auto_rejoin"):
            check = getattr(self.deps.rejoin, "validate_link", None)
            if check is not None:
                ok, why = check(str(st.get("private_server_link") or ""))
                if not ok:
                    self._log("warn", f"{why} Until then, rejoin will use a public server.")

    # -- the log ----------------------------------------------------------------
    def _read_log(self, now: float, st: dict) -> list[LogEvent]:
        """New log lines -> state. Returns this tick's disconnect lines."""
        tail = self.deps.tail
        bmx = tail.in_bmx                       # per line: a join can change it mid-batch
        disconnects: list[LogEvent] = []
        for ev in tail.poll():
            kind = ev.kind
            if kind == "join":
                bmx = ev.value == PLACE_ID
                self._new_session(now, st)
                if bmx:
                    self._log("info", "Joined Block Mayhem X.")
                else:
                    # The macro only ever launches Block Mayhem X, so this was you.
                    self._halt("You joined another game, so I stopped.")
                    return disconnects
            elif kind == "left":
                bmx = False
                if "Entered app session" in ev.raw and self.deps.clock.user_idle_seconds() < USER_BUSY_LEAVE:
                    # Back to the Roblox home screen while you're at the PC: on purpose.
                    self._halt("You left Block Mayhem X, so I stopped.")
                    return disconnects
            elif kind == "disconnect":
                disconnects.append(ev)
                if ev.value:
                    self._dc_reason = ev.value
            elif not bmx:
                continue                        # the rest only means something in BMX
            elif kind == "idle":
                self._on_idle(now, ev)
            elif kind == "world_music":
                self._on_world(now, ev.value)
            elif kind == "rare_song":
                self._on_song(now, st, ev.value)
            elif kind == "chromatic":
                self._on_chromatic(now, st, ev.value)
        return disconnects

    def _new_session(self, now: float, st: dict) -> None:
        """A join: what the screen and log said about the last server is old news."""
        self._send_waiting(now, st)             # the last server's held alerts go out first
        self._start_elsewhere = False
        self._joined_at = now
        self._setup_done = False
        if self._setups:
            # Setup again after this join, but not while you play: the 1.5 s
            # pause is only for the F1 press. The macro's own rejoin lowers
            # this to the usual 5 s once it's back in (_rejoined).
            self._setup_idle = AWAY_IDLE
        self._timer_val, self._timer_at, self._lost_low = None, _NEVER, False
        self._storage, self._storage_at = None, _NEVER
        # (storage is saved with the account, so "full" is still full on the
        # new server: _full_alerted stays until a reading shows room again)
        self._full_since = None
        self._timer_low_seen = _NEVER
        self.world, self._world_at, self._world_said_at = None, _NEVER, _NEVER
        self._idle = None
        self._jump_checked = True
        self._behind_noted = False

    def _on_idle(self, now: float, ev: LogEvent) -> None:
        try:
            value = float(ev.value)
        except ValueError:
            return
        self._idle_seq += 1
        self._idle = _Idle(value, now, self._idle_seq)
        if now - self._jump_at < IDLE_LAG:
            # Written just before our jump and read just after it: it says
            # nothing about whether the jump worked.
            self._jump_seq = self._idle_seq
            return
        if not self._jump_checked:
            # Roblox's idle clock restarts on input, so the first line after a
            # jump that landed shows a SMALLER number than the one before it.
            self._jump_checked = True
            if value < self._jump_idle:
                self._log("info", "The anti-AFK jump worked: Roblox's idle timer reset.")
                self._afk_miss_noted = False
            elif not self._afk_miss_noted:
                self._afk_miss_noted = True
                self._log("warn", "The anti-AFK jump didn't reach Roblox (its idle timer kept "
                                  "counting). Trying again.")

    def _world_banner(self, now: float, lines: list[str]) -> None:
        """'World voting concluded! The new world is Arctic.' - the game saying
        it outright beats guessing from music (which can be the old world's)."""
        m = re.search(r"new\s+world\s+is\s+([A-Za-z ]{3,20})", " ".join(lines), re.I)
        if not m:
            return
        import difflib
        got = difflib.get_close_matches(m.group(1).strip().title(), list(MAPS), n=1, cutoff=0.6)
        if got:
            self._world_said_at = now
            self._on_world(now, got[0], said=True)

    def _on_world(self, now: float, name: str, said: bool = False) -> None:
        if not name:
            return
        if not said and name != self.world and now - self._world_said_at < 540:
            return                              # music from before the vote: the banner already said
        self._world_at = now
        if name == self.world:
            return                              # the same world's music repeats
        self.world = name
        text = f"World is now {name}."
        if self._voted and now - self._voted[0] <= VOTE_NOTE:
            text += f" You voted {self._voted[1]}."
        self._log("info", text)

    def _on_song(self, now: float, st: dict, raw: str) -> None:
        name = blocks.display_name(raw)
        key = _key(name)
        if not key:
            return
        kw = dict(block=name, denom=blocks.rarity(raw), variant="normal",
                  source="log_song", picture=None, key=key)
        # Hold it a moment: when Roblox is in front the banner usually shows
        # at the same time, and it has the rarity and a picture.
        self._log_wait[key] = (now + LOG_HOLD, kw)

    def _on_chromatic(self, now: float, st: dict, raw: str) -> None:
        if _key(raw) in ("", "nil"):
            # 'nil_chromatic': the block was gone before Roblox wrote its name.
            self._log("info", "A chromatic block was on your server (Roblox didn't say which).")
            return
        name = blocks.display_name(raw)
        self._rare(now, st, block=name, denom=None, variant="chromatic",
                   source="log_chromatic", picture=None, key=_key(name))

    def _flush_log_rares(self, now: float, st: dict, look: _Look) -> None:
        for key, (until, kw) in list(self._log_wait.items()):
            # Roblox behind other windows: no banner is coming, don't wait.
            if now >= until or not look.readable:
                del self._log_wait[key]
                self._rare(now, st, **kw)

    # -- rare blocks ------------------------------------------------------------
    def _rare(self, now: float, st: dict, *, block: str, denom: int | None, variant: str,
              source: str, picture: Image.Image | None, key: str) -> None:
        """One rare block sighting -> an alert (or a log line when it is below
        the user's threshold), merged with other sightings of the same block."""
        if source == "log_chromatic":
            # Roblox writes this minutes after the block was there, so it can
            # only be matched to a chromatic banner, from up to 15 min back.
            # Kept apart from _sighted, so a normal spawn of the same block in
            # between can't hide that banner.
            seen = self._chroma_seen.pop(key, None)
            if seen is not None and now - seen <= CHROMA_MERGE:
                return
        else:
            if variant == "chromatic":
                self._chroma_seen[key] = now    # (even if merged below: its late note is old news)
                if len(self._chroma_seen) > 200:
                    self._prune(self._chroma_seen, now - CHROMA_MERGE, lambda v: v)
            prev = self._sighted.get(key)
            if prev is not None and self._same_sighting(prev, source, now):
                return                          # the banner and the log saw the same block
        self._sighted[key] = (now, source, variant)
        if len(self._sighted) > 200:
            self._prune(self._sighted, now - CHROMA_MERGE, lambda v: v[0])

        name = blocks.display_name(block)
        shown = name if variant == "normal" else f"{variant.title()} {name}"
        odds = f" (1 in {denom:,})" if denom else ""
        if variant != "normal" and _flag(st, "alert_variants_always"):
            alert = True
        elif denom is not None:
            alert = denom >= _int(st, "rare_min", 0)
        else:
            # No number to go by. Better one alert too many than a missed
            # mythic - except a chromatic note, which never has a number.
            alert = source != "log_chromatic"
        if not alert:
            self._log("info", f"{shown} spawned{odds}.")
            return

        # A picture only for blocks at or above your own rarity setting (not for
        # 'always alert' variants under it, or blocks with no known odds).
        if denom is not None and denom >= _int(st, "rare_min", 0):
            if picture is not None:
                self._picture(picture, "rare")
            if (_flag(st, "medal_clip") and self._clip_at is None
                    and denom >= _int(st, "medal_min", 1)):   # its own, higher bar (10M by default)
                self._clip_at = now + MEDAL_DELAY
        ping = denom is not None and denom >= _int(st, "rare_ping_min", 0)
        self._emit({"type": "rare", "block": name, "denominator": denom, "variant": variant,
                    "source": source, "ping": ping, "picture": picture, "world": self.world})
        if source == "log_chromatic":
            self._log("info", f"A {shown} was on your server (Roblox notes these late).")
        elif source == "log_song":
            self._log("info", f"Rare block: {shown} appeared{odds}!")
        else:
            self._log("info", f"Rare block: {shown} spawned{odds}!")

    @staticmethod
    def _same_sighting(prev: tuple[float, str, str], source: str, now: float) -> bool:
        """Is this sighting (a banner or a song line) the block `prev` already
        reported? Late chromatic notes are matched in _rare itself."""
        when, prev_source, _prev_variant = prev
        if prev_source == "log_chromatic":
            return False                        # a late note, not this spawn
        if source == "banner" and prev_source == "banner":
            return False                        # banners dedupe on their own (BANNER_DEDUPE)
        return now - when <= RARE_MERGE

    @staticmethod
    def _prune(d: dict, before: float, when: Callable[[Any], float]) -> None:
        for k in [k for k, v in d.items() if when(v) < before]:
            del d[k]

    def _on_banner(self, now: float, st: dict, b: hud.Banner, img: Image.Image) -> None:
        key = _key(b.block)
        if not key:
            return
        waiting = self._banner_wait.get(key)
        last = self._banner_seen.get(key)
        fresh = last is not None and now - last[0] <= BANNER_DEDUPE
        # The key has no variant in it (small reads often lose the word), so
        # a fresh entry is the same banner again - unless this read names a
        # variant the last one didn't: 'Chromatic Vexen' right after a normal
        # Vexen is a new block, and far rarer.
        new_variant = fresh and b.variant != "normal" and b.variant != last[1]
        self._banner_seen[key] = (now, b.variant if b.variant != "normal" or not fresh
                                  else last[1])
        if waiting is None and fresh and not new_variant:
            return                              # the same banner, still up
        denom = b.denominator or blocks.rarity_of(b.block, b.variant)
        if (denom is None and b.variant == "normal") or b.small:
            # The number didn't come through, or the text was shrunken (a
            # banner popping in or pushed up by the next one). A full-size
            # read a second later is better; keep the best read meanwhile.
            if waiting is None:
                self._banner_wait[key] = (b, now, img)
                return
            if _better_read(b, waiting[0]):
                self._banner_wait[key] = (b, waiting[1], img)
            if now - waiting[1] < BANNER_PATIENCE:
                return
            b, _, img = self._banner_wait[key]
            denom = b.denominator or blocks.rarity_of(b.block, b.variant)
        self._banner_wait.pop(key, None)
        self._rare(now, st, block=b.block, denom=denom, variant=b.variant, source="banner",
                   picture=img, key=key)

    def _banner_waited(self, now: float, st: dict, force: bool = False) -> None:
        """Banners still waiting for a readable rarity: past patience (or
        `force`: no better read can come), alert with the best read so far."""
        for key, (b, since, img) in list(self._banner_wait.items()):
            if force or now - since >= BANNER_PATIENCE:
                del self._banner_wait[key]
                self._rare(now, st, block=b.block,
                           denom=b.denominator or blocks.rarity_of(b.block, b.variant),
                           variant=b.variant, source="banner", picture=img, key=key)

    def _send_waiting(self, now: float, st: dict) -> None:
        """Alerts held back for a better read go out as they are: the run is
        stopping or the server is changing, so no better read is coming."""
        self._banner_waited(now, st, force=True)
        for key, (_, kw) in list(self._log_wait.items()):
            del self._log_wait[key]
            self._rare(now, st, **kw)

    # -- collected blocks (chat + worker pickups) --------------------------------
    def _pickups(self, now: float, lines: list[str]) -> None:
        """'Void King just picked up a Chromatic Mosaic block!' banners."""
        for text in lines:
            f = hud.parse_pickup(text)
            if f is not None:
                self._fix_waiting_variant(f)
                self._found(now, f)

    def _fix_waiting_variant(self, f: hud.Find) -> None:
        """A worker grabs a block right away and its spawn banner shrinks out,
        so the spawn read is often tiny and loses 'Inverted'/'Chromatic'. The
        pickup banner is big and says it plainly: let it fill the gap."""
        key = _key(f.block)
        waiting = self._banner_wait.get(key)
        if waiting is None or f.variant == "normal" or waiting[0].variant != "normal":
            return
        b, since, img = waiting
        self._banner_wait[key] = (replace(b, variant=f.variant), since, img)

    def _read_chat(self, now: float, img: Image.Image) -> None:
        """'SomePlayer has collected a Sporedelic with a 1 / 100,000 chance!'
        Only the account playing counts (its name comes from the Roblox log)."""
        _, finds = hud.read_chat(self.deps.ocr, img)
        me = str(getattr(self.deps.tail, "player", None) or "")
        # The first read of a run only learns what is already in the chat:
        # those finds are from before you pressed Start.
        quiet, self._chat_primed = not self._chat_primed, True
        for f in finds:
            if me and not hud.same_player(f.who, me):
                continue
            self._found(now, f, quiet=quiet)

    def _found(self, now: float, f: hud.Find, quiet: bool = False) -> None:
        # A chat line stays on screen for minutes: while it keeps being seen
        # it is the same find, not a new one.
        key = f"{f.variant}:{f.block.lower()}:{f.denominator}"
        seen = self._finds_seen.get(key, _NEVER)
        self._finds_seen[key] = now
        if quiet or now - seen < FIND_QUIET:
            return
        if len(self._finds_seen) > 300:
            self._prune(self._finds_seen, now - FIND_QUIET, lambda v: v)
        name = blocks.display_name(f.block)
        self._emit({"type": "collected", "block": name, "variant": f.variant,
                    "denominator": f.denominator, "how": f.how})
        odds = f" (1 in {f.denominator:,})" if f.denominator else ""
        what = (f"{f.variant.title()} " if f.variant != "normal" else "") + name
        self._log("info", f"Collected {what}{odds}." if f.how == "chat"
                  else f"A worker picked up {what}{odds}.")

    def _note_banner_text(self, now: float, lines: list[str]) -> None:
        """Banners that aren't spawns go in the log so new kinds can be learned,
        but only when they read cleanly: the game's own worker banners (Ritual,
        Fragment Frenzy, Golden Touch...) are skipped, OCR scraps are skipped,
        and near-copies of a banner already logged are skipped."""
        import difflib
        for text in lines:
            t = " ".join(str(text).split())
            if len(t.split()) < BANNER_TEXT_WORDS or not _reads_clean(t):
                continue
            flat = re.sub(r"[^a-z]", "", t.lower())
            if any(k in flat for k in KNOWN_BANNERS):
                continue
            k = t.lower()
            if any(now - at < BANNER_TEXT_QUIET and difflib.SequenceMatcher(None, k, old).ratio() >= 0.8
                   for old, at in self._banner_texts.items()):
                continue
            self._banner_texts[k] = now
            if len(self._banner_texts) > 200:
                self._prune(self._banner_texts, now - BANNER_TEXT_QUIET, lambda v: v)
            self._log("info", f'Banner: "{t[:120]}"')

    # -- the window -------------------------------------------------------------
    def _look(self, now: float) -> _Look:
        w = self.deps.window
        hwnd = int(w.roblox_hwnd() or 0)
        fg = bool(hwnd) and bool(w.is_foreground(hwnd))
        minimized = bool(hwnd) and bool(w.is_minimized(hwnd))
        # Every tick, so real input is sampled before one of our own
        # injections overwrites Windows' "last input" time.
        user_idle = float(self.deps.clock.user_idle_seconds())
        look = _Look(hwnd, fg, minimized, bool(self.deps.tail.in_bmx), user_idle)
        if look.hwnd:
            self._no_window_since = None
        elif self._no_window_since is None:
            self._no_window_since = now
        if look.in_bmx:
            self._not_bmx_since = None
        elif self._not_bmx_since is None:
            self._not_bmx_since = now
        if look.fg and not look.minimized:
            self._behind_noted = False
        return look

    def _disconnected(self, disconnects: list[LogEvent]) -> bool:
        tail = self.deps.tail
        if hasattr(tail, "disconnected"):
            return bool(tail.disconnected)      # cleared by the next join
        return bool(disconnects)

    def _away(self, look: _Look) -> bool:
        """The user is away from the PC: no keyboard or mouse for a minute,
        and no full-screen program (a video, a game, a presentation) in front.
        A gamepad or a film doesn't move the mouse, so idle alone isn't enough
        before closing Roblox or pulling it to the front."""
        if look.user_idle < AWAY_IDLE:
            return False
        busy = getattr(self.deps.window, "fullscreen_app_in_front", None)
        return not (busy is not None and busy())

    def _input_ok(self, look: _Look) -> bool:
        """Keys and clicks are allowed: Roblox in front, in BMX, the user not
        busy at the PC, and no rejoin going on."""
        return look.readable and look.user_idle >= INPUT_IDLE and self._rj is None \
            and not self._dc_noted

    # -- the screen -------------------------------------------------------------
    def _timer_est(self, now: float) -> float | None:
        """Seconds left on the vote timer, counted down from the last reading."""
        if self._timer_val is None or now - self._timer_at > TIMER_STALE:
            return None
        return max(0.0, self._timer_val - (now - self._timer_at))

    def _screen(self, now: float, st: dict, look: _Look) -> None:
        v = self._vote
        if v is not None and now - v.started >= VOTE_SCAN:
            self._end_vote(now, "timeout")
            v = None
        if not look.readable or self._rj is not None:
            # Roblox behind, or the game is going away: no better read of a
            # waiting banner is coming, so it goes out as it is.
            self._banner_waited(now, st, force=True)
            return
        est = self._timer_est(now)
        low = (est is not None and est <= TIMER_LOW) or self._lost_low
        top_due = now - self._top_at >= (TOP_EVERY_LOW if low else TOP_EVERY)
        banner_due = now - self._banner_at >= BANNER_EVERY
        if v is None and not top_due and not banner_due:
            return
        w = self.deps.window
        rect = w.client_rect(look.hwnd)
        if not rect:
            return
        try:
            img = w.grab(rect)
        except OSError:
            # Locked PC or a UAC prompt: can't see, not broken.
            self._banner_waited(now, st, force=True)
            if not self._blind_noted:
                self._blind_noted = True
                self._log("warn", "Can't see the Roblox window right now (is the PC locked?).")
            return
        self._blind_noted = False
        if v is not None:
            self._vote_tick(now, st, look, rect, img)
            self._banner_waited(now, st)
            return
        if banner_due:
            self._banner_at = now
            lines, banner = hud.read_banner(self.deps.ocr, img)
            self._pickups(now, lines)
            self._world_banner(now, lines)
            if banner is not None:
                self._on_banner(now, st, banner, img)
            else:
                self._note_banner_text(now, lines)
            self._banner_waited(now, st)
        if now - self._chat_at >= CHAT_EVERY:
            self._chat_at = now
            self._read_chat(now, img)
        if top_due:
            self._top_at = now
            self._read_top(now, st, img)
        if self._vote_due(now, st):
            self._start_vote(now)

    def _read_top(self, now: float, st: dict, img: Image.Image) -> None:
        top = hud.read_top(self.deps.ocr, img)
        if top.timer is not None:
            if top.timer <= 60:
                self._timer_low_seen = now
            elif top.timer >= 300 and now - self._timer_low_seen <= 150:
                # The timer went from under a minute back up to ~10: a vote
                # happened, so the world may have changed. A world_music line
                # says to what (Overworld, Void and Cave have none).
                self._world_changed(self._timer_low_seen)
            self._timer_val, self._timer_at = top.timer, now
            self._lost_low = False
        else:
            # Low a moment ago and now gone: the vote screen may be covering it.
            self._lost_low = (self._timer_val is not None and self._timer_val <= TIMER_LOW
                              and now - self._timer_at <= TIMER_STALE)
        if top.storage is not None:
            self._storage, self._storage_at = top.storage, now
            self._check_storage(now, *top.storage)

    def _check_storage(self, now: float, n: int, m: int) -> None:
        if n < m:
            self._full_since, self._full_alerted = None, False     # room again: alert next time
            return
        if self._full_since is None or now - self._full_seen_at > STORAGE_STALE:
            self._full_since = now              # not seen for a while: start over
        self._full_seen_at = now
        if not self._full_alerted and now - self._full_since >= STORAGE_FULL_FOR:
            self._full_alerted = True
            self._emit({"type": "storage_full", "n": n, "m": m})
            self._log("warn", f"Storage is full ({n}/{m}). New blocks won't fit until you "
                              "make room.")

    def _world_changed(self, since: float) -> None:
        """A vote happened (it started at `since`). Unless a world_music line
        came after that, which world we are in is not known any more."""
        self._timer_low_seen = _NEVER           # one vote, one reset
        if self._world_at <= since:
            self.world = None

    def _send_hud(self, now: float) -> None:
        est = self._timer_est(now)
        timer = None if est is None else int(math.ceil(est))
        storage = None
        if self._storage is not None and now - self._storage_at <= STORAGE_STALE:
            storage = [self._storage[0], self._storage[1]]
        sent = (timer, tuple(storage) if storage else None, self.world)
        if sent != self._hud_sent:
            self._hud_sent = sent
            self._emit({"type": "hud", "timer": timer, "storage": storage, "world": self.world})

    # -- the vote ---------------------------------------------------------------
    def _vote_due(self, now: float, st: dict) -> bool:
        if not _flag(st, "auto_vote") or self._vote is not None or self._busy is not None:
            return False
        if self._rj is not None or now < self._vote_quiet_until:
            return False
        est = self._timer_est(now)
        return (est is not None and est <= VOTE_AT) or self._lost_low

    def _start_vote(self, now: float) -> None:
        self._busy = "vote"
        self._vote = _Vote(started=now)

    def _vote_tick(self, now: float, st: dict, look: _Look, rect: tuple[int, int, int, int],
                   img: Image.Image) -> None:
        """One look at the whole client while a vote may be up."""
        v = self._vote
        v.looks += 1
        lines = self.deps.ocr.read(img, VOTE_SCALE)
        self._banner_from_lines(now, st, lines, img)
        opts = vote.find_options(lines, img.height, img.width)
        up = vote.is_vote_screen(opts)

        def votes_for(name: str | None) -> int | None:
            """The 'N Votes' count on that map's card. Only the chosen card
            says whether the click counted: the countdown and the other
            players' votes change all over the screen anyway."""
            opt = next((o for o in opts if o.name == name), None)
            return None if opt is None else vote.card_votes(lines, opt, img.height, img.width)

        if v.clicks == 0:
            if not up:
                if v.seen:
                    self._end_vote(now, "gone")
                return
            if not v.seen:
                v.seen = True
                v.options = [o.name for o in opts]
                v.img = img                     # pictures only if something goes wrong
                self._log("info", "Vote screen: " + ", ".join(v.options) + ".")
            pick = vote.choose(opts, _ranking(st))
            v.chose = pick.name
            if not self._input_ok(look):
                if not v.busy_noted:
                    v.busy_noted = True
                    self._log("info", "The vote is up, but you're using the PC, so I'll wait "
                                      "before clicking.")
                return
            if self._click(rect, pick, st):
                v.clicks, v.clicked_at, v.before = 1, now, votes_for(pick.name)
            else:
                v.misses += 1
                if v.misses >= VOTE_MISSES:
                    self._end_vote(now, "missed")
            return

        # Clicked: did the chosen card's vote count go up?
        if not up:
            self._end_vote(now, "done")
            return
        if now - v.clicked_at < VOTE_RECHECK:
            return
        after = votes_for(v.chose)
        counted = after is not None and (after > v.before if v.before is not None else after >= 1)
        if counted:
            self._end_vote(now, "done")         # it took the click
            return
        if v.clicks >= 2:
            self._end_vote(now, "unchanged")
            return
        # Up and not counted 2 s later: click once more, at most.
        pick = next((o for o in opts if o.name == v.chose), None) or vote.choose(opts, _ranking(st))
        if pick is None or not self._input_ok(look):
            self._end_vote(now, "done")
            return
        v.chose = pick.name
        if self._click(rect, pick, st):
            v.clicks, v.clicked_at = 2, now
        else:
            self._end_vote(now, "done")

    def _banner_from_lines(self, now: float, st: dict, lines: list, img: Image.Image) -> None:
        """The vote scan reads the whole window, so spawn banners still count.
        Same checks as a normal banner read: name typos, a misread number,
        shrunken text."""
        y0, y1 = hud.REGIONS["banner"][1] * img.height, hud.REGIONS["banner"][3] * img.height
        band = [ln for ln in lines if ln.words and y0 <= ln.words[0].y <= y1]
        banner = hud.spawn_banner(self.deps.ocr, img, band, img.height)
        if banner is not None:
            self._on_banner(now, st, banner, img)

    def _click(self, rect: tuple[int, int, int, int], pick: vote.VoteOption, st: dict) -> bool:
        off = st.get("vote_click_offset") or (0, 0)
        x, y = vote.click_point(pick, off)
        y += round(VOTE_BUTTON_DY * rect[3])    # the green VOTE button sits under the map name
        if not (CLICK_INSET <= x < rect[2] - CLICK_INSET and CLICK_INSET <= y < rect[3] - CLICK_INSET):
            return False                        # outside Roblox's picture: never click there
        # click_at wants SCREEN pixels; the OCR boxes are client pixels.
        return bool(self.deps.inputs.click_at(rect[0] + x, rect[1] + y))

    def _end_vote(self, now: float, how: str) -> None:
        v = self._vote
        self._vote = None
        if self._busy == "vote":
            self._busy = None
        self._vote_quiet_until = now + VOTE_COOLDOWN
        self._timer_val, self._timer_at, self._lost_low = None, _NEVER, False
        if v is None or how == "rejoin":
            return
        if not v.seen:
            if v.looks:                         # (Roblox behind the whole time: nothing to say)
                self._log("info", "Didn't see a vote screen this time.")
            return
        ok = v.clicks > 0
        self._emit({"type": "vote", "options": list(v.options), "chose": v.chose, "ok": ok})
        self._world_changed(v.started)
        if ok:
            self._voted = (now, v.chose or "")
            self._log("info", f"Voted for {v.chose}.")
            if how == "unchanged":
                if v.img is not None:
                    self._picture(v.img, "vote_problem")
                self._log("warn", f"Clicked {v.chose} twice but its vote count didn't go up, "
                                  "so the vote may not have counted.")
        elif v.busy_noted:
            self._log("info", "Didn't vote this time: you were using the PC.")
        else:
            if v.img is not None:
                self._picture(v.img, "vote_problem")
            self._log("warn", "Couldn't click the vote this time.")

    def _picture(self, img: Image.Image, tag: str) -> None:
        """Save a picture to the data folder, only with 'Save pictures' on."""
        if self.deps.save_picture is None or not _flag(self._settings(), "save_pictures"):
            return
        try:
            self.deps.save_picture(img, tag)
        except Exception as e:
            self._error("saving a picture", e)

    # -- anti-AFK ---------------------------------------------------------------
    def _fresh_idle(self, now: float) -> _Idle | None:
        """The latest idle line, if it is recent and came after our last jump."""
        i = self._idle
        if i is None or i.seq <= self._jump_seq or now - i.at > IDLE_FRESH:
            return None
        return i

    def _no_idle_lines(self, now: float) -> bool:
        return self._idle is None or now - self._idle.at > IDLE_FRESH

    # -- Roblox's Esc menu ---------------------------------------------------------
    def _esc_menu(self, now: float, st: dict, look: _Look) -> None:
        """Left open, Roblox's Esc menu hides every banner (a 1B went unseen
        like that). When the game's top bar has been gone a while, check for
        the menu and press Esc - only with the user away from the keys."""
        if not _flag(st, "close_esc_menu") or not look.readable:
            return
        if self._busy is not None or self._rj is not None or self._vote is not None:
            return
        hud_hidden = now - self._timer_at >= ESC_HUD_LOST
        if now - self._esc_at < (ESC_CHECK_EVERY if hud_hidden else ESC_ANYWAY):
            return
        self._esc_at = now
        w = self.deps.window
        rect = w.client_rect(look.hwnd)
        if not rect:
            return
        try:
            img = w.grab(rect)
        except OSError:
            return
        if not hud.esc_menu_open(self.deps.ocr, img):
            return
        if not self._input_ok(look) or now - self._esc_pressed_at < ESC_GAP:
            return
        self._busy = "esc"
        try:
            ok = bool(self.deps.inputs.tap_scan(SCAN_ESC))
        finally:
            self._busy = None
        if ok:
            self._esc_pressed_at = now
            self._log("info", "Closed the Roblox menu (Esc) so the macro can see the game.")

    # -- Roblox in front -------------------------------------------------------------
    def _keep_front(self, now: float, st: dict, look: _Look) -> None:
        """The macro only sees the game with Roblox in front (a 2B went unseen
        behind Discord for 7 hours). At the start of a run, and then every
        hour, bring it forward if something covers it. The hourly one waits
        until you've left the keys, so it never jumps over your typing."""
        if not _flag(st, "front_hourly") or not look.hwnd:
            return
        if self._busy is not None or self._rj is not None:
            return
        first = self._front_at == _NEVER
        if not first and now - self._front_at < FRONT_EVERY:
            return
        if look.fg and not look.minimized:
            self._front_at = now                # already in front: nothing to do
            return
        if not first and look.user_idle < FRONT_IDLE:
            return                              # you're using the PC: wait until you're away
        if self.deps.window.bring_to_front(look.hwnd):
            self._front_at = now
            self._log("info", "Brought Roblox to the front so the macro can see the game.")
        else:
            self._front_at = now - FRONT_EVERY + 60.0   # try again in a minute

    # -- the vote screen, seen directly ---------------------------------------------
    def _vote_title(self, now: float, st: dict, look: _Look) -> None:
        """The vote screen hides the round timer. If the timer was missed
        (covered, misread, the game changed it) the timer never says 'vote
        now', so with the top bar gone a moment, look for the vote screen's
        own title and start the vote from that."""
        if not _flag(st, "auto_vote") or not look.readable:
            return
        if self._vote is not None or self._busy is not None or self._rj is not None:
            return
        if now < self._vote_quiet_until or now - self._timer_at < VOTE_TITLE_AFTER:
            return
        if now - self._title_at < VOTE_TITLE_EVERY:
            return
        self._title_at = now
        w = self.deps.window
        rect = w.client_rect(look.hwnd)
        if not rect:
            return
        try:
            img = w.grab(rect)
        except Exception:                   # the screen step already says when it can't see
            return
        if hud.vote_title_shown(self.deps.ocr, img):
            self._start_vote(now)

    # -- Medal clips ----------------------------------------------------------------
    def _medal(self, now: float, st: dict, look: _Look) -> None:
        """Press the user's Medal clip key a moment after a rare spawn. Only with
        Roblox in front and the user away from the keys; gives up after 15 s."""
        due = self._clip_at
        if due is None or now < due:
            return
        if now - self._clip_last < MEDAL_GAP or now - due > 15:
            self._clip_at = None
            return
        if not look.readable or self._busy is not None or not self._input_ok(look):
            return
        press = getattr(self.deps.inputs, "press_keys", None)
        key = str(st.get("medal_key") or "F4")
        self._clip_at = None
        if press is not None and press(key):
            self._clip_last = now
            self._log("info", f"Clipped it with Medal ({key}).")

    # -- start-of-run setup -------------------------------------------------------
    def _setup(self, now: float, st: dict, look: _Look) -> None:
        """Once per run (and again after a rejoin): open the pinned recipe and
        put the cursor in the middle of the screen."""
        if self._setup_done or not look.readable or self._busy is not None or self._rj is not None:
            return
        if self._dc_noted:
            return                              # a dead session: nothing to set up
        if look.user_idle < self._setup_idle or not getattr(self.deps.tail, "loaded", True):
            return
        self._setup_done = True
        self._setups += 1
        self._busy = "setup"
        try:
            if _flag(st, "open_pinned"):
                self._open_pinned(look)
            if _flag(st, "center_cursor"):
                w = self.deps.window
                size = getattr(w, "screen_size", None)
                if size is not None:
                    sw, sh = size()
                else:
                    r = w.client_rect(look.hwnd) or (0, 0, 0, 0)
                    sw, sh = r[0] * 2 + r[2], r[1] * 2 + r[3]
                if sw and sh and self.deps.inputs.move_to(sw // 2, sh // 2):
                    self._log("info", "Moved the cursor to the middle of the screen.")
            if _flag(st, "start_jump"):
                self.deps.sleep(0.2)
                if self.deps.inputs.press_space():
                    self._jump_at = now             # counts as input for the anti-AFK clock
                    self._log("info", "Jumped to start.")
        finally:
            self._busy = None

    def _open_pinned(self, look: _Look) -> None:
        w = self.deps.window
        rect = w.client_rect(look.hwnd)
        if not rect:
            return
        try:
            img = w.grab(rect)
        except OSError:
            return                              # can't see right now (locked PC)
        if hud.pinned_open(self.deps.ocr, img):
            self._log("info", "The pinned recipe is already open.")
            return
        pt = hud.pinned_arrow(img)
        if pt is None:
            self._picture(img, "pinned_missing")   # so the arrow can be tuned from a real picture
            self._log("warn", "Couldn't find the pinned recipe arrow (saved a picture of it).")
            return
        if pt[2] != "left":
            # ▶: the panel is open, its header just wasn't read (no text
            # reading, or a misread). Clicking would close it.
            self._log("info", "The pinned recipe is already open.")
            return
        if not self.deps.inputs.click_at(rect[0] + pt[0], rect[1] + pt[1]):
            return
        self.deps.sleep(0.8)
        try:
            opened = hud.pinned_open(self.deps.ocr, w.grab(rect))
        except OSError:
            return
        if opened:
            self._log("info", "Opened the pinned recipe.")
        else:
            self._log("warn", "Clicked the pinned recipe arrow, but it didn't open.")

    def _anti_afk(self, now: float, st: dict, look: _Look) -> None:
        if not _flag(st, "anti_afk") or not look.hwnd or not look.in_bmx:
            return
        if self._rj is not None or self._busy is not None or self._dc_noted:
            return
        if now - self._afk_tried_at < AFK_RETRY:
            return
        after = afk_after(st) - (self._afk_early if _flag(st, "afk_variety") else 0.0)
        line = self._fresh_idle(now)
        # Since the last input Roblox got: the user's, or our last jump.
        since_input = min(look.user_idle, now - self._jump_at)

        if look.fg and not look.minimized:
            if not self._input_ok(look):
                return
            # A line from before the user's last input is out of date: that
            # input reset Roblox's idle clock too.
            if line is not None and line.at >= now - look.user_idle:
                if line.value >= after:
                    self._jump(now, line.value, st, look)
            elif self._no_idle_lines(now) and since_input >= max(after, IDLE_FRESH):
                # The game stopped printing idle lines: fall back to a timer.
                self._jump(now, since_input, st, look)
            return

        # Roblox is behind other windows (or minimised): no input can reach it.
        if line is not None:
            idle = line.value
        elif self._no_idle_lines(now):
            idle = since_input
        else:
            return
        if idle < BEHIND_IDLE:
            return
        if _flag(st, "afk_bring_to_front") and self._away(look):
            self._front_jump(now, look, idle)
        elif not self._behind_noted:
            self._behind_noted = True
            self._log("warn", "Roblox is behind other windows, you may get kicked "
                              "(it will rejoin).")

    def _jump(self, now: float, idle: float, st: dict, look: _Look) -> None:
        """Mostly a jump; with afk_variety, sometimes a double jump or a click
        in the open game view first, so it doesn't look like a robot."""
        self._afk_tried_at = now
        self._busy = "anti_afk"
        inp, how, ok = self.deps.inputs, "jump", False
        roll = self.rng.random() if _flag(st, "afk_variety") else 0.0
        try:
            if roll >= 0.8:
                rect = self.deps.window.client_rect(look.hwnd)
                if rect and inp.click_at(rect[0] + int(rect[2] * AFK_CLICK_AT[0]),
                                         rect[1] + int(rect[3] * AFK_CLICK_AT[1])):
                    how = "click_jump"
                    self.deps.sleep(0.3)
            ok = bool(inp.press_space())
            if ok and 0.6 <= roll < 0.8:
                self.deps.sleep(0.35 + 0.3 * self.rng.random())
                inp.press_space()
                how = "double_jump"
        finally:
            self._busy = None
        self._afk_early = self.rng.uniform(0.0, AFK_JITTER)
        self._jumped(now, idle, ok, how)

    def _front_jump(self, now: float, look: _Look, idle: float) -> None:
        """Bring Roblox forward, jump, put the user's window back."""
        self._afk_tried_at = now
        self._busy = "anti_afk"
        w = self.deps.window
        raised = jumped = False
        try:
            raised = bool(w.bring_to_front(look.hwnd))
            if raised:
                self.deps.sleep(FRONT_SETTLE)
                jumped = bool(self.deps.inputs.press_space())
                self.deps.sleep(FRONT_AFTER)
        finally:
            if raised:
                try:
                    w.restore_front(look.hwnd)
                except Exception as e:
                    self._error("putting your window back", e)
            self._busy = None
        if not raised:
            if not self._afk_fail_noted:
                self._afk_fail_noted = True
                self._log("warn", "Couldn't bring Roblox to the front for the anti-AFK jump.")
            return
        self._jumped(now, idle, jumped, "front_jump")

    def _jumped(self, now: float, idle: float, ok: bool, how: str) -> None:
        if not ok:
            if not self._afk_fail_noted:
                self._afk_fail_noted = True
                self._log("warn", "Couldn't press Space in Roblox for the anti-AFK jump.")
            return
        self._afk_fail_noted = False
        self._jump_at, self._jump_idle = now, idle
        self._jump_seq = self._idle_seq         # lines from before the jump don't count
        self._jump_checked = False
        self._emit({"type": "anti_afk", "idle": round(float(idle), 1), "how": how})
        where = " (Roblox was behind other windows)" if how == "front_jump" else ""
        verb = {"double_jump": "Double-jumped", "click_jump": "Clicked and jumped"}.get(how, "Jumped")
        self._log("info", f"{verb} so Roblox won't kick you for being idle "
                          f"({_mins(idle)}){where}.")

    # -- rejoin -----------------------------------------------------------------
    def _rejoin_reason(self, now: float, look: _Look, disconnects: list[LogEvent]) -> str | None:
        """Why the player is not in their game right now, or None if they are."""
        if self._disconnected(disconnects):
            code = f" (code {self._dc_reason})" if self._dc_reason else ""
            return f"You got disconnected{code}"
        if self._no_window_since is not None and now - self._no_window_since >= NO_WINDOW_AFTER:
            return "Roblox closed"
        if self._not_bmx_since is not None and now - self._not_bmx_since >= NOT_BMX_AFTER:
            if self.deps.tail.place:
                return "You're in a different game"
            return "You left Block Mayhem X"
        return None

    def _rejoin(self, now: float, st: dict, look: _Look, disconnects: list[LogEvent]) -> None:
        rj = self._rj
        if rj is not None and rj.phase in ("confirm", "loading"):
            self._rejoin_follow(now, rj, disconnects)
            return

        dc = self._disconnected(disconnects)
        if dc and not self._dc_noted:
            code = f" (code {self._dc_reason})" if self._dc_reason else ""
            self._log("warn", f"Disconnected from the server{code}.")
        self._dc_noted = dc
        why = self._rejoin_reason(now, look, disconnects)
        if why is None and rj is not None and look.in_bmx and \
                not getattr(self.deps.tail, "loaded", True):
            # Back in the server, but the game hasn't loaded: not back yet.
            # Give it as long as a rejoin's load gets, then try again.
            if now - self._joined_at < LOAD_WAIT:
                return
            why = "The game didn't finish loading"

        if rj is not None and why is None:
            self._rj = None
            self._log("info", "You're back in Block Mayhem X, so no rejoin is needed.")
            return
        if why is None:
            self._off_noted = ""
            return
        if not _flag(st, "auto_rejoin"):
            if rj is not None:
                self._rj = None
                self._log("info", "Auto rejoin was turned off, so I won't rejoin.")
            if self._off_noted != why:
                self._off_noted = why
                self._log("warn", f"{why}. Auto rejoin is off, so I'm just waiting.")
            return
        if rj is None:
            self._rj = rj = _Rejoin(why=why, since=now)
            self._cap_noted = False
            self._log("warn", f"{why}. I'll rejoin once you've been away from the PC "
                              "for a minute.")
        rj.why = why

        # Ready for a try? Away, past the backoff, under the hourly cap.
        if now < rj.next_at or not self._away(look):
            return
        if self._capped(now):
            if not self._cap_noted:
                self._cap_noted = True
                self._log("warn", f"Rejoined {MAX_PER_HOUR} times in the last hour, so I'm "
                                  "waiting a while before trying again.")
            return
        self._rejoin_try(now, st, rj)

    def _capped(self, now: float) -> bool:
        """MAX_PER_HOUR launches in the last hour already."""
        while self._launches and now - self._launches[0] >= HOUR:
            self._launches.popleft()
        return len(self._launches) >= MAX_PER_HOUR

    def _rejoin_try(self, now: float, st: dict, rj: _Rejoin) -> None:
        if self._busy == "vote":
            self._end_vote(now, "rejoin")       # getting back in matters more
        if self._busy is not None or self._stop_evt.is_set():
            return
        self._busy = "rejoin"
        rj.tries = rj.fails + 1
        tail = self.deps.tail
        rj.old_log = getattr(tail, "path", None)
        self._emit({"type": "rejoin", "phase": "start", "try": rj.tries, "why": rj.why})
        self._log("info", f"Rejoining (try {rj.tries} of {MAX_FAILS})...")
        rejoin = self.deps.rejoin
        try:
            killed = int(rejoin.kill_roblox() or 0)
            if killed:
                self._log("info", "Closed Roblox.")
            if self._stop_evt.is_set():         # Stop pressed meanwhile: don't open it again
                self._busy = None
                return
            cands = list(rejoin.deep_links(str(st.get("private_server_link") or "")))
            # Rotate: if one form of the link doesn't get in, the next try
            # uses the other.
            uri = cands[(rj.tries - 1) % len(cands)]
            rejoin.launch(uri)
        except Exception as e:
            self._error("opening Roblox", e)
            self._rejoin_failed(now, rj, "Couldn't open Roblox.")
            return
        self._launches.append(now)
        rj.phase, rj.launched_at = "confirm", now
        self._log("info", "Opened Roblox, waiting for it to join.")

    def _rejoin_follow(self, now: float, rj: _Rejoin, disconnects: list[LogEvent]) -> None:
        """A try is under way: watch the log for the new session."""
        tail = self.deps.tail
        path = getattr(tail, "path", None)
        new_log = bool(path) and path != rj.old_log
        if rj.phase == "confirm":
            old = os.path.basename(rj.old_log) if rj.old_log else ""
            if new_log and any(d.log != old for d in disconnects):
                self._rejoin_failed(now, rj, "You got disconnected while joining.")
                return
            if new_log and tail.in_bmx and tail.entered:
                rj.phase, rj.entered_at = "loading", now
                self._log("info", "Joined the server, waiting for the game to load.")
            elif now - rj.launched_at >= JOIN_WAIT:
                self._rejoin_failed(now, rj, "Roblox didn't get into the server within "
                                             "2 minutes.")
                return
        if rj.phase == "loading":
            if not (new_log and tail.in_bmx):
                self._rejoin_failed(now, rj, "Roblox left the server while it was loading.")
            elif tail.loaded:
                self._rejoined(now, rj)
            elif now - rj.entered_at >= LOAD_WAIT:
                self._rejoin_failed(now, rj, "The game didn't finish loading within a minute.")
            else:
                try:
                    self._press_play(now, look)     # the title screen waits for PLAY
                except Exception as e:
                    self._error("pressing Play", e)

    def _press_play(self, now: float, look: _Look) -> None:
        """Block Mayhem X opens on a title screen: press its PLAY button so the
        game loads (the rejoin waits for that). Only with Roblox in front."""
        if now - self._play_at < PLAY_EVERY or not (look.hwnd and look.fg and not look.minimized):
            return
        self._play_at = now
        w = self.deps.window
        rect = w.client_rect(look.hwnd)
        if not rect:
            return
        try:
            img = w.grab(rect)
        except OSError:
            return
        pt = hud.find_play(self.deps.ocr, img)
        if pt and self.deps.inputs.click_at(rect[0] + pt[0], rect[1] + pt[1]):
            self._log("info", "Pressed Play.")

    def _rejoined(self, now: float, rj: _Rejoin) -> None:
        self._rj = None
        self._busy = None
        self._no_window_since = self._not_bmx_since = None
        self._dc_noted = False
        self._setup_idle = INPUT_IDLE           # our own rejoin: set up with the usual 5 s rule
        self._emit({"type": "rejoin", "phase": "ok", "try": rj.tries, "why": rj.why})
        self._log("info", "Back in Block Mayhem X.")

    def _rejoin_failed(self, now: float, rj: _Rejoin, why: str) -> None:
        if self._busy == "rejoin":
            self._busy = None
        rj.fails += 1
        rj.phase = "wait"
        if rj.fails >= MAX_FAILS:
            self._emit({"type": "rejoin", "phase": "fail", "try": rj.tries, "why": why})
            stuck = f"Couldn't get back into Block Mayhem X after {MAX_FAILS} tries in a row."
            self._emit({"type": "rejoin", "phase": "gave_up", "try": rj.tries, "why": stuck})
            self._log("error", stuck + " The run stopped.")
            self._rj = None
            self._halt(stuck)
            return
        wait = BACKOFF[min(rj.fails, len(BACKOFF)) - 1]
        rj.next_at = now + wait
        self._emit({"type": "rejoin", "phase": "fail", "try": rj.tries, "why": why,
                    "wait": wait})
        self._log("warn", f"{why} Trying again in {_mins(wait)}.")

    # -- status -----------------------------------------------------------------
    def _update_state(self, now: float, st: dict, look: _Look) -> None:
        rj = self._rj
        if rj is not None and rj.phase in ("confirm", "loading"):
            state, why = "rejoining", f"Rejoining (try {rj.tries} of {MAX_FAILS})"
        elif rj is not None:
            if now < rj.next_at:
                state, why = "waiting", "Waiting a bit before trying to rejoin again"
            elif self._capped(now):
                state, why = "waiting", "Rejoined a lot this hour, waiting before the next try"
            else:
                state, why = "waiting", "Waiting for you to step away to rejoin"
        elif self._dc_noted:
            state, why = "waiting", "Disconnected"
        elif not look.hwnd:
            state, why = "waiting", "Roblox isn't open"
        elif not look.in_bmx:
            state, why = "waiting", "Not in Block Mayhem X"
        elif look.minimized:
            state, why = "paused", "Roblox is minimised"
        elif not look.fg:
            state, why = "paused", "Roblox is behind other windows"
        else:
            state, why = "running", "In Block Mayhem X"
        self._set_state(state, why)
