"""robloxlog: the parser over the five real BMX logs, and the tail over a temp folder.

Nothing here reads the real %LOCALAPPDATA%\\Roblox\\logs: every LogTail gets a
temp folder (or a LOCALAPPDATA pointed at one). The real logs in
tests/fixtures/logs are local only (not in the repo): without them, the tests
that read them skip.
"""
import glob
import os
import re
import shutil
import time

import pytest

from mayhem import PLACE_ID
from mayhem.robloxlog import (LogEvent, LogTail, logs_dir, newest_player_log,
                              parse_line)

LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "logs")
HAVE_LOGS = bool(glob.glob(os.path.join(LOGS, "*_Player_*_last.log")))
needs_logs = pytest.mark.skipif(not HAVE_LOGS, reason="the real Roblox logs are local only")
OTHER_GAME = "15532962292"
KINDS = {"join", "entered", "loaded", "left", "disconnect", "rare_song",
         "world_music", "chromatic", "idle"}


def fixture(tag: str) -> str:
    if not HAVE_LOGS:
        pytest.skip("the real Roblox logs are local only")
    found = glob.glob(os.path.join(LOGS, f"*_Player_{tag}_last.log"))
    assert len(found) == 1, f"fixture log {tag} missing"
    return found[0]


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().split("\n")


def events(tag: str) -> list[LogEvent]:
    return [ev for ev in map(parse_line, read_lines(fixture(tag))) if ev]


def values(evs: list[LogEvent], kind: str) -> list[str]:
    return [ev.value for ev in evs if ev.kind == kind]


def kinds(evs: list[LogEvent]) -> list[str]:
    return [ev.kind for ev in evs]


def line(msg: str, level: str = "") -> str:
    """A synthetic line in Roblox's real format."""
    lv = f",{level}" if level else ""
    return f"2026-10-09T02:00:00.000Z,100.000000,2f98,6{lv} {msg}"


# ------------------------------------------------------------- parser ---

@needs_logs
def test_all_five_fixture_logs_are_there():
    assert len(glob.glob(os.path.join(LOGS, "*_Player_*_last.log"))) == 5


@pytest.mark.parametrize("tag", ["78728", "F2E68", "EFD9F", "CBA41", "F5605"])
def test_every_log_parses_cleanly(tag):
    evs = events(tag)
    assert evs and evs[0].kind == "join"
    assert evs[-1].kind == "left"
    for ev in evs:
        assert ev.kind in KINDS
        assert re.fullmatch(r"\d\d:\d\d:\d\d", ev.utc)
        assert len(ev.raw) <= 300 and ev.log == ""
        assert not ev.raw.endswith(("\n", "\r"))
    # Every session in these logs went in and loaded fine; none was kicked.
    assert "entered" in kinds(evs)


def test_f5605_bmx_session():
    evs = events("F5605")
    assert kinds(evs) == ["join", "entered", "world_music", "loaded", "chromatic",
                          "rare_song", "idle", "idle", "chromatic", "left"]
    join = evs[0]
    assert join.value == PLACE_ID and join.utc == "01:28:38"
    assert "Joining game" in join.raw
    assert values(evs, "world_music") == ["Dreamland"]
    assert values(evs, "chromatic") == ["vexen", "vulcan"]
    assert values(evs, "rare_song") == ["snakeblock"]
    assert values(evs, "idle") == ["133.74098578102075", "163.74975465406203"]
    assert values(evs, "disconnect") == []


def test_efd9f_songs_worlds_and_idle():
    evs = events("EFD9F")
    assert values(evs, "join") == [PLACE_ID]
    assert values(evs, "rare_song") == ["sporedelic", "lucid"]
    # The world music repeats while it plays, so a world change is a NEW name.
    worlds = values(evs, "world_music")
    assert worlds == ["Dreamland", "Arctic", "Arctic", "Arctic"]
    assert [w for i, w in enumerate(worlds) if i == 0 or worlds[i - 1] != w] == [
        "Dreamland", "Arctic"]
    idle = [float(v) for v in values(evs, "idle")]
    assert len(idle) == 8
    assert idle[0] == pytest.approx(138.78035493202043)
    assert all(120 <= s < 240 for s in idle)
    # The idle count restarts when the player touches anything (228 -> 138).
    assert idle[4] < idle[3]


def test_cba41_sols_rng_then_bmx():
    evs = events("CBA41")
    assert values(evs, "join") == [OTHER_GAME, PLACE_ID]
    assert values(evs, "chromatic") == ["nil"]
    assert values(evs, "rare_song") == ["voidrinyx"]
    assert values(evs, "loaded") == [""]
    # Leaving another game for the Roblox home screen: reason 285 three times,
    # then 'Entered app session.' (left) - and only then the BMX join.
    assert kinds(evs) == ["join", "entered", "disconnect", "disconnect",
                          "disconnect", "left", "join", "entered", "loaded",
                          "rare_song", "chromatic", "left"]
    assert values(evs, "disconnect") == ["285", "285", "285"]
    assert "Entered app session" in evs[5].raw
    assert "Tearing down" in evs[-1].raw


def test_78728_and_f2e68_quiet_sessions():
    a = events("78728")
    assert values(a, "join") == [PLACE_ID]
    assert values(a, "world_music") == ["Dreamland"] * 4
    assert len(values(a, "idle")) == 3
    assert kinds(events("F2E68")) == ["join", "entered", "loaded", "left"]


@pytest.mark.parametrize("tag", ["78728", "F2E68", "EFD9F", "CBA41", "F5605"])
def test_client_disconnect_is_not_a_disconnect(tag):
    lines = [ln for ln in read_lines(fixture(tag)) if "Client:Disconnect" in ln]
    assert lines, "every fixture log has Client:Disconnect lines"
    for ln in lines:
        assert parse_line(ln) is None
    assert parse_line(line("[DFLog::NetworkClient] Client:Disconnect", "Info")) is None


@needs_logs
def test_aura_stack_lines_produce_nothing():
    lines = [ln for p in glob.glob(os.path.join(LOGS, "*.log"))
             for ln in read_lines(p) if "Aura.Script" in ln]
    assert len(lines) >= 10
    assert any("iris_chromatic" in ln for ln in lines)
    assert any("iris_inverted" in ln for ln in lines)
    for ln in lines:
        assert parse_line(ln) is None


@pytest.mark.parametrize("text, reason", [
    (line("[FLog::Network] Sending disconnect with reason: 285"), "285"),
    (line("[FLog::Network] Connection lost"), ""),
    (line("[FLog::Network] Connection lost: connectMode: Disconnect ASAP, reason: 0"), "0"),
    (line("[FLog::Network] Disconnection Notification. Reason: 267"), "267"),
    (line("[FLog::Error] id_connection_lost"), ""),
    (line("[FLog::Warning] Lost Connection to the game server", "Warning"), ""),
    ("Sending disconnect with reason: 285", "285"),
    ("Disconnection Notification. Reason: 267\r\n", "267"),
])
def test_disconnect_lines(text, reason):
    ev = parse_line(text)
    assert ev is not None and ev.kind == "disconnect"
    assert ev.value == reason


def test_disconnect_keeps_the_time():
    ev = parse_line(line("[FLog::Network] Disconnection Notification. Reason: 267"))
    assert ev.utc == "02:00:00" and ev.log == ""
    assert parse_line("[FLog::Network] Connection lost").utc == ""


def test_game_prints_cannot_fake_session_signals():
    # A game's own print can say anything; it must not start a rejoin.
    for msg in ("Player lost connection",
                "Sending disconnect with reason: 285",
                "! Joining game 'x' place 1 at 10.0.0.1",
                "Tearing down."):
        for chan in ("CreatorOutput", "CreatorWarning", "CreatorError"):
            assert parse_line(line(f"[FLog::{chan}] {msg}", "Info")) is None


def test_session_lines():
    ev = parse_line(line("[FLog::SessionTransitionFSM] Tearing down."))
    assert (ev.kind, ev.value) == ("left", "")
    ev = parse_line(line("[FLog::SessionTransitionFSM] Entered app session."))
    assert (ev.kind, ev.value) == ("left", "")
    ev = parse_line(line("[FLog::SessionTransitionFSM] Entered play session."))
    assert (ev.kind, ev.value) == ("entered", "")
    ev = parse_line(line("[FLog::Output] ! Joining game 'a-b' place 75270037454060 at 10.1.2.3"))
    assert (ev.kind, ev.value) == ("join", PLACE_ID)
    # Other SessionTransitionFSM lines mean nothing.
    for msg in ("Initialized.", "Session history: IPT", "Sent play session success.",
                "Teleported."):
        assert parse_line(line(f"[FLog::SessionTransitionFSM] {msg}")) is None
    # 'Tearing down' from another channel is not the session ending.
    assert parse_line(line("[FLog::Graphics] Tearing down renderer")) is None


def test_song_chromatic_idle_and_loaded_lines():
    warn = "[FLog::CreatorWarning] Warning: Could not find song group for sound "
    ev = parse_line(line(warn + "snakeblock_song", "Warning"))
    assert (ev.kind, ev.value) == ("rare_song", "snakeblock")
    ev = parse_line(line(warn + "Lava", "Warning"))
    assert (ev.kind, ev.value) == ("world_music", "Lava")
    ev = parse_line(line(warn + "NewThing_SONG  ", "Warning"))
    assert (ev.kind, ev.value) == ("rare_song", "NewThing")
    assert parse_line(line(warn + "_song", "Warning")) is None
    ev = parse_line(line("[FLog::CreatorOutput] [Chromatic] Thread stopped for block: "
                         "vexen_chromatic after 10000 iterations", "Info"))
    assert (ev.kind, ev.value) == ("chromatic", "vexen")
    ev = parse_line(line("[FLog::CreatorOutput] Player has been idle for 600.5 seconds", "Info"))
    assert (ev.kind, ev.value) == ("idle", "600.5")
    ev = parse_line(line("[FLog::CreatorOutput] \u2705 | CLIENT: GuiController module "
                         "loaded in 0.07653s!", "Info"))
    assert (ev.kind, ev.value) == ("loaded", "")


def test_parse_line_odds_and_ends():
    assert parse_line("") is None
    assert parse_line("\n") is None
    assert parse_line("    Stack Begin") is None
    assert parse_line(line("[FLog::Output] Hello World!", "Warning")) is None
    long = line("[FLog::Network] Connection lost: " + "x" * 600)
    ev = parse_line(long)
    assert ev.kind == "disconnect" and len(ev.raw) == 300 and ev.raw == long[:300]


# ---------------------------------------------------------- log files ---

BASE = time.time() - 3600


def put(folder, name: str, lines: list[str], mtime: float, end: str = "\n") -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + (end if lines else ""))
    os.utime(path, (mtime, mtime))
    return path


def add(path: str, text: str, mtime: float | None = None) -> None:
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_logs_dir_uses_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert logs_dir() == os.path.join(str(tmp_path), "Roblox", "logs")
    assert not os.path.exists(logs_dir())       # never created


def test_newest_player_log_filters_names(tmp_path):
    assert newest_player_log(str(tmp_path / "missing")) is None
    assert newest_player_log(str(tmp_path)) is None
    good = put(tmp_path, "0.742_20261009T010000Z_Player_AAAAA_last.log", ["a"], BASE)
    put(tmp_path, "0.742_20261009T010001Z_Player_AAAAA_CrashHandler_last.log", ["b"], BASE + 50)
    put(tmp_path, "0.742_20261009T010002Z_Studio_BBBBB_last.log", ["c"], BASE + 60)
    put(tmp_path, "0.742_20261009T010003Z_Player_CCCCC.log", ["d"], BASE + 70)
    put(tmp_path, "notes.txt", ["e"], BASE + 80)
    assert newest_player_log(str(tmp_path)) == good


def test_newest_player_log_tie_goes_to_later_name(tmp_path):
    put(tmp_path, "0.742_20261009T010000Z_Player_AAAAA_last.log", ["a"], BASE)
    later = put(tmp_path, "0.742_20261009T020000Z_Player_BBBBB_last.log", ["b"], BASE)
    assert newest_player_log(str(tmp_path)) == later


def test_prime_emits_nothing_but_learns_state(tmp_path):
    src = read_lines(fixture("F5605"))
    upto = next(i for i, ln in enumerate(src) if "idle for 163.7" in ln) + 1
    path = put(tmp_path, os.path.basename(fixture("F5605")), src[:upto], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.path == path
    assert tail.place == PLACE_ID and tail.in_bmx
    assert tail.entered and tail.loaded and not tail.disconnected
    assert tail.last_idle == pytest.approx(163.74975465406203)
    assert tail.poll() == []
    assert tail.poll() == []


def test_prime_on_a_finished_log_is_not_in_bmx(tmp_path):
    shutil.copyfile(fixture("F5605"), tmp_path / os.path.basename(fixture("F5605")))
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.place is None and not tail.in_bmx
    assert not tail.entered and not tail.loaded and tail.last_idle is None
    assert tail.poll() == []


def test_prime_on_cba41_ends_in_bmx_then_left(tmp_path):
    src = read_lines(fixture("CBA41"))
    upto = next(i for i, ln in enumerate(src) if "voidrinyx_song" in ln) + 1
    put(tmp_path, os.path.basename(fixture("CBA41")), src[:upto], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    # The other game's session and its 285s came first; the BMX join reset them.
    assert tail.in_bmx and tail.entered and tail.loaded
    assert not tail.disconnected


def test_poll_without_prime_learns_first(tmp_path):
    put(tmp_path, os.path.basename(fixture("EFD9F")), read_lines(fixture("EFD9F"))[:300], BASE)
    tail = LogTail(str(tmp_path))
    assert tail.poll() == []
    assert tail.in_bmx and tail.loaded


def test_appended_lines_and_a_half_written_line(tmp_path):
    src = read_lines(fixture("F5605"))
    upto = next(i for i, ln in enumerate(src) if "idle for 163.7" in ln) + 1
    end = next(i for i, ln in enumerate(src) if "Tearing down." in ln)
    name = os.path.basename(fixture("F5605"))
    path = put(tmp_path, name, src[:upto], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()

    add(path, "\n".join(src[upto:end]) + "\n")
    got = tail.poll()
    assert [(e.kind, e.value) for e in got] == [("chromatic", "vulcan")]
    assert got[0].log == name and got[0].utc == "01:46:26"
    assert tail.poll() == []

    # Roblox is half way through writing 'Tearing down.': nothing yet.
    half, rest = src[end][:60], src[end][60:]
    add(path, half)
    assert tail.poll() == []
    assert tail.in_bmx
    add(path, rest + "\n")
    got = tail.poll()
    assert [e.kind for e in got] == ["left"]
    assert got[0].raw == src[end].rstrip("\r")
    assert tail.place is None and not tail.in_bmx


def test_utf8_split_and_bad_bytes_are_safe(tmp_path):
    path = put(tmp_path, "0.742_20261009T010000Z_Player_AAAAA_last.log", [], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    loaded = line("[FLog::CreatorOutput] \u2705 | CLIENT: GuiController module loaded", "Info")
    data = (line("[FLog::Output] ! Joining game 'a' place 75270037454060 at 1.2.3.4")
            + "\n").encode() + loaded.encode("utf-8")
    cut = data.index("\u2705".encode("utf-8")) + 1     # inside the emoji
    with open(path, "ab") as fh:
        fh.write(data[:cut])
    assert [e.kind for e in tail.poll()] == ["join"]
    with open(path, "ab") as fh:
        fh.write(data[cut:] + b"\n\xff\xfe junk\n")
    assert [e.kind for e in tail.poll()] == ["loaded"]
    assert tail.loaded


def test_newer_log_is_followed_from_its_start(tmp_path):
    old = put(tmp_path, os.path.basename(fixture("F2E68")),
              read_lines(fixture("F2E68"))[:300], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.in_bmx

    # The old client's last words, then Roblox starts a new client and log.
    add(old, line("[FLog::Network] Sending disconnect with reason: 267") + "\n", BASE + 10)
    src = read_lines(fixture("EFD9F"))
    upto = next(i for i, ln in enumerate(src) if "lucid_song" in ln) + 1
    new = put(tmp_path, os.path.basename(fixture("EFD9F")), src[:upto], BASE + 20)

    got = tail.poll()
    assert (got[0].kind, got[0].value, got[0].log) == ("disconnect", "267", os.path.basename(old))
    rest = got[1:]
    assert rest[0].kind == "join" and rest[0].value == PLACE_ID
    assert all(e.log == os.path.basename(new) for e in rest)
    assert values(rest, "rare_song") == ["sporedelic", "lucid"]
    assert tail.path == new
    assert tail.in_bmx and tail.entered and tail.loaded and not tail.disconnected
    assert tail.last_idle == pytest.approx(189.52565149692805)
    assert tail.poll() == []

    # The old log being touched again does not send it back there.
    add(old, line("[FLog::Network] Connection lost") + "\n", BASE + 30)
    assert tail.poll() == []
    assert tail.path == new


def test_crashhandler_logs_are_ignored_even_if_newer(tmp_path):
    name = os.path.basename(fixture("F2E68"))
    player = put(tmp_path, name, read_lines(fixture("F2E68"))[:300], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    crash = name.replace("_last.log", "_CrashHandler_last.log")
    put(tmp_path, crash, [
        line("[FLog::Output] ! Joining game 'z' place 15532962292 at 1.2.3.4"),
        line("[FLog::Network] Connection lost"),
    ], BASE + 100)
    assert tail.poll() == []
    assert tail.path == player and tail.in_bmx
    assert newest_player_log(str(tmp_path)) == player


def test_logs_already_there_at_start_are_never_replayed(tmp_path):
    older = put(tmp_path, os.path.basename(fixture("78728")),
                read_lines(fixture("78728")), BASE)
    newest = put(tmp_path, os.path.basename(fixture("F5605")),
                 read_lines(fixture("F5605"))[:300], BASE + 10)
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.path == newest
    os.utime(older, (BASE + 50, BASE + 50))
    assert tail.poll() == []
    assert tail.path == newest


def test_truncation_starts_over(tmp_path):
    path = put(tmp_path, os.path.basename(fixture("F2E68")), read_lines(fixture("F2E68")), BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.place is None
    put(tmp_path, os.path.basename(path), [
        line("[FLog::Output] ! Joining game 'q' place 75270037454060 at 1.2.3.4"),
        line("[FLog::SessionTransitionFSM] Entered play session."),
    ], BASE + 5)
    got = tail.poll()
    assert [(e.kind, e.value) for e in got] == [("join", PLACE_ID), ("entered", "")]
    assert tail.in_bmx and tail.entered and not tail.loaded


def test_no_logs_folder_yet_then_a_log_appears(tmp_path):
    folder = tmp_path / "logs"
    tail = LogTail(str(folder))
    tail.prime()
    assert tail.path is None and tail.place is None
    assert tail.poll() == []
    # Started with no logs at all, so the first one is a new session.
    put(folder, "0.742_20261009T010000Z_Player_AAAAA_last.log", [
        line("[FLog::Output] ! Joining game 'q' place 75270037454060 at 1.2.3.4"),
    ], BASE)
    got = tail.poll()
    assert [(e.kind, e.value) for e in got] == [("join", PLACE_ID)]
    assert tail.in_bmx


def test_join_resets_the_session_state(tmp_path):
    path = put(tmp_path, "0.742_20261009T010000Z_Player_AAAAA_last.log", [
        line("[FLog::Output] ! Joining game 'q' place 75270037454060 at 1.2.3.4"),
        line("[FLog::SessionTransitionFSM] Entered play session."),
        line("[FLog::CreatorOutput] CLIENT: GuiController module loaded", "Info"),
        line("[FLog::CreatorOutput] Player has been idle for 300.25 seconds", "Info"),
        line("[FLog::Network] Connection lost"),
    ], BASE)
    tail = LogTail(str(tmp_path))
    tail.prime()
    assert tail.loaded and tail.disconnected and tail.last_idle == 300.25
    add(path, line("[FLog::Output] ! Joining game 'r' place 15532962292 at 1.2.3.4") + "\n")
    tail.poll()
    assert tail.place == OTHER_GAME and not tail.in_bmx
    assert not tail.entered and not tail.loaded and not tail.disconnected
    assert tail.last_idle is None
    # BMX's loaded line in another game does not count as BMX loaded.
    add(path, line("[FLog::CreatorOutput] CLIENT: GuiController module loaded", "Info") + "\n")
    assert [e.kind for e in tail.poll()] == ["loaded"]
    assert not tail.loaded


def test_default_folder_comes_from_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    folder = tmp_path / "Roblox" / "logs"
    path = put(folder, "0.742_20261009T010000Z_Player_AAAAA_last.log", [
        line("[FLog::Output] ! Joining game 'q' place 75270037454060 at 1.2.3.4"),
    ], BASE)
    tail = LogTail()
    tail.prime()
    assert tail.path == path and tail.in_bmx
    assert newest_player_log() == path


def test_every_fixture_streamed_through_the_tail_matches_the_parser(tmp_path):
    # Feed each real log in uneven chunks: the tail must give exactly what the
    # parser gives for the whole file, in order, with the file name attached.
    tail = LogTail(str(tmp_path))
    tail.prime()
    for n, tag in enumerate(["78728", "F2E68", "EFD9F", "CBA41", "F5605"]):
        with open(fixture(tag), "rb") as fh:
            data = fh.read()
        name = os.path.basename(fixture(tag))
        path = put(tmp_path, name, [], BASE + 100 * (n + 1))
        got: list[LogEvent] = []
        step = 7919
        for i in range(0, len(data), step):
            with open(path, "ab") as fh:
                fh.write(data[i:i + step])
            os.utime(path, (BASE + 100 * (n + 1), BASE + 100 * (n + 1)))
            got += tail.poll()
        got += tail.poll()
        want = [(e.kind, e.value, e.utc) for e in events(tag)]
        assert [(e.kind, e.value, e.utc) for e in got] == want
        assert all(e.log == name for e in got)
        assert tail.path == path
