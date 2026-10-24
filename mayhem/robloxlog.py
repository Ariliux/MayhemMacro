"""Roblox's own log: which game we are in, and what just happened in it.

Every Roblox client process writes its own file to %LOCALAPPDATA%\\Roblox\\logs.
It says which place we joined, when the session ended and when the network
dropped, and - through Block Mayhem X's own prints - when a music block or a
chromatic block turned up. None of that needs the screen, so it still works
with Roblox behind other windows.

Two traps, both seen in this PC's real logs:
- `[DFLog::NetworkClient] Client:Disconnect` is written on EVERY normal
  teleport and leave. It is not a disconnect, and nothing here treats it as one.
- Each player log gets a CrashHandler twin about a second later, and that file
  has `_Player_` in its name too. "The newest log" without a name filter flips
  over to it and loses the real one.

Reading only. Nothing here writes, creates folders or touches Roblox.
"""
import os
import re
from dataclasses import dataclass, replace

from . import PLACE_ID


@dataclass(frozen=True)
class LogEvent:
    kind: str    # join | entered | loaded | left | disconnect | rare_song | world_music | chromatic | idle
    value: str   # place id | '' | '' | '' | reason number or '' | block name (e.g. 'snakeblock') |
                 # world name | block name without _chromatic (e.g. 'vexen') | idle seconds as text
    utc: str     # 'HH:MM:SS' from the line, '' if unknown
    raw: str     # the line, max 300 chars
    log: str     # basename of the log file it came from


RAW_MAX = 300

# 2026-10-09T01:28:38.925Z,7.925577,2358,6,Warning [FLog::CreatorWarning] Warning: ...
# The level after the thread id is optional. A line without the stamp is the
# second half of a multi-line message (a stack trace, mostly).
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\S*\s+")
_CHANNEL_RE = re.compile(r"^\[D?FLog::([A-Za-z0-9_]+)\]\s*")

_JOIN_RE = re.compile(r"! Joining game '[^']*' place (\d+)")
# The game asks for a song group that does not exist. A name ending in _song is
# a music block (sporedelic_song); anything else is the world's own music
# (Dreamland, Arctic). Overworld, Void and Cave have none, so they print nothing.
_SONG_RE = re.compile(r"Could not find song group for sound (.+?)\s*$")
# A block that just spawned into the world and has a broken texture or script
# names itself: '... load failed in Workspace.Blocks.ignityx.Base.core...',
# 'Workspace.Blocks.eruption_chromatic.Base.BlockScript'. Only some blocks do
# (Luminescent, Bubbly, Relic, Ignityx, Atonion, Borealis, Eruption...), but it
# needs no screen at all.
_SPAWN_RE = re.compile(r"\bWorkspace\.Blocks\.([A-Za-z0-9_]+)\.")
# Printed when a chromatic block's colour loop ends, so it comes minutes AFTER
# the block was there. 'nil_chromatic' is real too (the block was already gone).
_CHROMA_RE = re.compile(r"\[Chromatic\] Thread stopped for block: (.+?)_chromatic\b",
                        re.IGNORECASE)
# Roblox's LocalPlayer.Idled, printed by the game: after 2 idle minutes, then
# every 30 s. The 20-minute idle kick runs off the same clock.
_IDLE_RE = re.compile(r"Player has been idle for (\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) seconds")
_LOADED = "CLIENT: GuiController module loaded"
_REASON_RE = re.compile(r"reason:\s*(\d+)", re.IGNORECASE)
# Lower-case pieces of the real network-drop lines. 'Client:Disconnect' is
# deliberately not here (see the top of the file).
_DROP = ("connection lost", "lost connection", "id_connection_lost",
         "sending disconnect with reason", "disconnection notification")


def logs_dir() -> str:
    """%LOCALAPPDATA%\\Roblox\\logs. Not created or checked: Roblox makes it."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Roblox", "logs")


def _is_player_log(name: str) -> bool:
    low = name.lower()
    return ("_player_" in low and low.endswith("_last.log")
            and "crashhandler" not in low)


def _player_logs(logdir: str) -> list[tuple[float, str]]:
    """(mtime, path) of every player log in the folder. A missing folder is
    simply none. os.stat per file, not the directory listing: Windows can
    leave a listing's times stale while Roblox still has the file open."""
    out: list[tuple[float, str]] = []
    try:
        names = os.listdir(logdir)
    except OSError:
        return out
    for name in names:
        if not _is_player_log(name):
            continue
        path = os.path.join(logdir, name)
        try:
            out.append((os.path.getmtime(path), path))
        except OSError:
            pass
    return out


def newest_player_log(logdir: str | None = None) -> str | None:
    """The player log written most recently, never a CrashHandler one. On an
    mtime tie the later name wins (names start with the launch time)."""
    logs = _player_logs(logdir or logs_dir())
    return max(logs)[1] if logs else None


def _classify(channel: str, msg: str) -> tuple[str, str] | None:
    """(kind, value) for one message, or None when it means nothing to us."""
    # The game's own prints (CreatorOutput/Warning/Error) can say anything, so
    # the session and network signals are only taken from Roblox's channels.
    # A game printing "Player lost connection" must not start a rejoin.
    if not channel.startswith("Creator"):
        m = _JOIN_RE.search(msg)
        if m:
            return "join", m.group(1)
        if channel in ("SessionTransitionFSM", ""):
            if msg.startswith("Entered play session"):
                return "entered", ""
            # Tearing down = the client is closing. Entered app session = it
            # went back to the Roblox home screen (Leave in the menu); real
            # logs show that one WITHOUT a Tearing down, so it ends the game too.
            if msg.startswith("Tearing down") or msg.startswith("Entered app session"):
                return "left", ""
        low = msg.lower()
        if "client:disconnect" not in low and any(p in low for p in _DROP):
            m = _REASON_RE.search(msg)
            return "disconnect", m.group(1) if m else ""
    if _LOADED in msg:
        return "loaded", ""
    m = _SONG_RE.search(msg)
    if m:
        name = m.group(1).strip()
        if name.lower().endswith("_song"):
            block = name[:-len("_song")].strip()
            return ("rare_song", block) if block else None
        return "world_music", name
    m = _CHROMA_RE.search(msg)
    if m:
        return "chromatic", m.group(1).strip()
    m = _SPAWN_RE.search(msg)
    if m:
        return "block_spawn", m.group(1)
    m = _IDLE_RE.search(msg)
    if m:
        return "idle", m.group(1)
    return None


def parse_line(line: str) -> LogEvent | None:
    """One log line -> an event, or None. Pure: no state, log=''."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    stamp = _STAMP_RE.match(line)
    utc = stamp.group(1) if stamp else ""
    body = line[stamp.end():] if stamp else line
    chan = _CHANNEL_RE.match(body)
    channel = chan.group(1) if chan else ""
    msg = body[chan.end():] if chan else body
    found = _classify(channel, msg)
    if found is None:
        return None
    return LogEvent(found[0], found[1], utc, line[:RAW_MAX], "")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class LogTail:
    """Follows the newest player log, complete lines only.

    prime() reads what is already there to learn where we are, without
    reporting any of it: old lines must never come back as new alerts. poll()
    then hands over what was written since. Only the engine thread calls it.

    Reads are binary and stop at the last newline. Roblox may be half way
    through writing a line, and half a line matches nothing - or the wrong
    thing. The rest is picked up on the next poll.
    """

    # Bytes read per poll. A log grows by a few KB a minute; this only matters
    # if something odd dumps megabytes at once, and the rest comes next poll.
    READ_MAX = 4 * 1024 * 1024

    def __init__(self, logdir: str | None = None) -> None:
        self.logdir = logdir
        self.path: str | None = None          # current file
        self.place: str | None = None         # place id of the latest join, None after 'left'
        self.entered = False                  # 'Entered play session.' seen after the latest join
        self.loaded = False                   # BMX 'loaded' seen after the latest join
        self.last_idle: float | None = None   # seconds from the latest idle line (None after a join or 'left')
        self.disconnected = False             # a disconnect line since the latest join
        self.player: str | None = None        # the account playing (from 'Players.<name>.PlayerGui')
        self._pos = 0
        self._primed = False
        # Logs we moved on from, or that were already old at prime(). Never
        # followed again, so two clients writing at once can't make it read a
        # file from the top twice and repeat its alerts.
        self._passed: set[str] = set()

    @property
    def in_bmx(self) -> bool:
        return self.place == PLACE_ID

    def _dir(self) -> str:
        return self.logdir or logs_dir()

    _PLAYER = re.compile(r"\bPlayers\.([A-Za-z0-9_]{3,20})\.PlayerGui\b")

    def _reset(self) -> None:
        self.place = None
        self.entered = self.loaded = self.disconnected = False
        self.last_idle = None

    def _apply(self, ev: LogEvent) -> None:
        kind = ev.kind
        if kind == "join":
            self._reset()
            self.place = ev.value
        elif kind == "entered":
            self.entered = True
        elif kind == "loaded":
            # GuiController is BMX's module; another game printing the same
            # words does not make this a loaded BMX session.
            self.loaded = self.loaded or self.in_bmx
        elif kind == "left":
            self.place = None
            self.entered = self.loaded = False
            self.last_idle = None
        elif kind == "disconnect":
            self.disconnected = True
        elif kind == "idle":
            try:
                self.last_idle = float(ev.value)
            except ValueError:
                pass

    def _read(self, limit: int | None) -> list[LogEvent]:
        """Complete lines after the saved offset, applied to the state."""
        if not self.path:
            return []
        try:
            with open(self.path, "rb") as fh:
                if os.fstat(fh.fileno()).st_size < self._pos:
                    # Shorter than what was already read: the file was cut or
                    # rewritten. Start over as if it were a new log.
                    self._reset()
                    self._pos = 0
                fh.seek(self._pos)
                data = fh.read(limit) if limit else fh.read()
        except OSError:
            return []
        cut = data.rfind(b"\n")
        if cut < 0:
            # A full chunk with no line end is not a log line, and waiting for
            # one would stall the tail for good. Skip past it.
            if limit and len(data) >= limit:
                self._pos += len(data)
            return []
        self._pos += cut + 1
        name = os.path.basename(self.path)
        out: list[LogEvent] = []
        for line in data[:cut].decode("utf-8", errors="replace").split("\n"):
            if self.player is None and "PlayerGui" in line:
                m = self._PLAYER.search(line)
                if m:
                    self.player = m.group(1)    # the account playing: chat 'collected' lines are matched to it
            ev = parse_line(line)
            if ev is not None:
                ev = replace(ev, log=name)
                self._apply(ev)
                out.append(ev)
        return out

    def _newer(self) -> str | None:
        """A player log that started after the current one, or None."""
        cur = _norm(self.path) if self.path else None
        best: tuple[float, str] | None = None
        for mtime, path in _player_logs(self._dir()):
            key = _norm(path)
            if key == cur or key in self._passed:
                continue
            if best is None or (mtime, path) > best:
                best = (mtime, path)
        if best is None:
            return None
        if self.path:
            try:
                cur_m = os.path.getmtime(self.path)
            except OSError:
                cur_m = -1.0       # gone: anything newer takes over
            if (best[0], os.path.basename(best[1])) < (cur_m, os.path.basename(self.path)):
                return None
        return best[1]

    def prime(self) -> None:
        """Read the newest log to the end and learn the state. Emits nothing."""
        self._reset()
        self._pos = 0
        logs = _player_logs(self._dir())
        newest = max(logs)[1] if logs else None
        self._passed = {_norm(p) for _, p in logs if p != newest}
        self.path = newest
        self._read(None)
        self._primed = True

    def poll(self) -> list[LogEvent]:
        """New complete lines since the last call, as events. Moves to a newer
        log when Roblox starts one and reads that from its first line."""
        if not self._primed:
            # Never primed: learn first, so the history is not replayed.
            self.prime()
            return []
        out: list[LogEvent] = []
        newer = self._newer()
        if newer:
            # Finish the old file first: its last lines ('Tearing down.', a
            # disconnect) happened before the new file's join.
            out += self._read(None)
            if self.path:
                self._passed.add(_norm(self.path))
            self.path, self._pos = newer, 0
            self._reset()
        out += self._read(self.READ_MAX)
        return out
