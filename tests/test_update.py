"""The self-updater: checking GitHub, downloading, unpacking, swapping the
files and handing over, all offline and on throwaway folders.

Nothing here reaches the network, starts a program, opens a window or asks
anything on screen: GitHub's answers and the downloads come from fake
transports, every real urlopen / socket / Popen / startfile is a tripwire
that fails the test, and the guards in mayhem/update.py stay on except the
one a test lifts, on its own tmp_path. The app side runs the window's real
update methods on a stand-in without Tk (like test_ui_logic.py does).
"""
import copy
import hashlib
import io
import json
import os
import socket
import subprocess
import types
import urllib.request
import zipfile

import pytest

import mayhemmacro as M
from mayhem import APP_VER, paths, safety, update

API = "https://api.github.com/repositories/1421452081/releases/latest"


def bump(v: str) -> str:
    major, minor, patch = update.version_tuple(v)
    return f"{major}.{minor}.{patch + 1}"


NEWER = bump(APP_VER)           # 1.0.1 while the app is 1.0.0
OLDER = "0.0.1"


# ------------------------------------------------------------- tripwires ---
@pytest.fixture(autouse=True)
def nothing_real(monkeypatch):
    """Any real network call, program start or file opening fails the test,
    even one that something swallowed."""
    hits = []

    def trip(name):
        def fn(*a, **k):
            hits.append(name)
            raise AssertionError(f"tests never call the real {name}")
        return fn
    monkeypatch.setattr(urllib.request, "urlopen", trip("urlopen"))
    monkeypatch.setattr(socket, "create_connection", trip("socket"))
    monkeypatch.setattr(subprocess, "Popen", trip("Popen"))
    monkeypatch.setattr(os, "startfile", trip("startfile"), raising=False)
    yield
    assert hits == [], hits


@pytest.fixture
def data(tmp_path, monkeypatch):
    """This test's own data folder, with a user's files in it."""
    d = tmp_path / "data"
    (d / "events").mkdir(parents=True)
    (d / "settings.json").write_text('{"auto_vote": false, "check_updates": true}')
    (d / "stats.json").write_text('{"runs": 12}')
    (d / "log.txt").write_text("2026-10-15 12:00:00  hello\n")
    (d / "events" / "vote.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setenv("MAYHEM_DATA_DIR", str(d))
    assert update.update_dir() == str(d / "update")       # the override took hold
    return d


def snapshot(folder) -> dict:
    """Every file under `folder` (except the update folder) and its bytes."""
    out = {}
    for base, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if x != "update"]
        for f in files:
            full = os.path.join(base, f)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, folder)] = fh.read()
    return out


class Lifted:
    """update.py's view of safety with only `allowed` guards lifted."""

    def __init__(self, *allowed):
        self.allowed, self.asked = set(allowed), []
        self.SideEffectBlocked = safety.SideEffectBlocked
        self.testing = safety.testing

    def guard(self, what):
        self.asked.append(what)
        if what not in self.allowed:
            safety.guard(what)


def lift(monkeypatch, *allowed) -> Lifted:
    lifted = Lifted(*allowed)
    monkeypatch.setattr(update, "safety", lifted)
    assert update.safety is lifted
    return lifted


# ------------------------------------------------------- fake GitHub ---
class Resp(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def transport(answer, calls=None, headers=None):
    """A stand-in for urlopen: `answer` is a dict (JSON), bytes or an
    exception. Every request it gets goes into `calls`."""
    def send(req, timeout):
        if calls is not None:
            calls.append({"url": req.full_url, "headers": dict(req.header_items()),
                          "timeout": timeout})
        if isinstance(answer, Exception):
            raise answer
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return Resp(body, headers)
    return send


def asset(version=NEWER, **over):
    name = f"MayhemMacro-v{version}.zip"
    a = {"name": name, "size": 1234, "digest": "sha256:" + "ab" * 32,
         "browser_download_url": f"{update.ASSET_PREFIX}v{version}/{name}"}
    a.update(over)
    return a


def release(version=NEWER, assets=None, **over):
    tag = f"v{version}"
    data = {"url": "https://api.github.com/repos/Ariliux/MayhemMacro/releases/250000001",
            "html_url": f"https://github.com/Ariliux/MayhemMacro/releases/tag/{tag}",
            "tag_name": tag, "draft": False, "prerelease": False,
            "assets": [asset(version)] if assets is None else assets}
    data.update(over)
    return data


def make_zip(path, files: dict) -> str:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


BUILD = {"MayhemMacro/MayhemMacro.exe": b"NEW-EXE",
         "MayhemMacro/_internal/base_library.zip": b"new library",
         "MayhemMacro/_internal/customtkinter/theme.json": b"{}"}


def install(folder, exe=b"OLD-EXE"):
    """An installed MayhemMacro: the exe, _internal and a file of the user's."""
    os.makedirs(os.path.join(folder, "_internal"))
    with open(os.path.join(folder, "MayhemMacro.exe"), "wb") as fh:
        fh.write(exe)
    with open(os.path.join(folder, "_internal", "old.dll"), "wb") as fh:
        fh.write(b"old")
    with open(os.path.join(folder, "my notes.txt"), "w") as fh:
        fh.write("keep me")
    return str(folder)


# ------------------------------------------------------- version numbers ---
def test_version_numbers():
    vt = update.version_tuple
    assert vt("v1.0.10") > vt("1.0.9")
    assert vt("v1.0.3") == (1, 0, 3)
    assert vt("1.1") == (1, 1, 0) == vt("v1.1.0")
    assert vt("nonsense") == () and vt(None) == () and vt("") == ()
    assert vt("v1.0.4-x64") == vt("1.0.4")             # extra numbers don't count
    assert update.is_newer(NEWER) and update.is_newer("v" + NEWER)
    assert not update.is_newer(APP_VER) and not update.is_newer("v" + APP_VER)
    assert not update.is_newer(OLDER) and not update.is_newer("garbage")
    assert update.is_newer("2.0.0", than="1.9.9") and not update.is_newer("1.9.9", than="2.0")


# ----------------------------------------------------- reading the release ---
def test_a_newer_release_is_read_by_the_repos_number():
    calls = []
    info = update.release_info(transport(release(), calls))
    assert info == {"version": NEWER,
                    "page": f"https://github.com/Ariliux/MayhemMacro/releases/tag/v{NEWER}",
                    "zip": f"{update.ASSET_PREFIX}v{NEWER}/MayhemMacro-v{NEWER}.zip",
                    "sha256": "ab" * 32, "size": 1234}
    (call,) = calls
    assert call["url"] == API == update.LATEST_API       # THIS repo, by number only
    assert "Ariliux" not in call["url"]
    assert call["headers"]["User-agent"] == f"MayhemMacro/{APP_VER}"
    assert call["timeout"] == update.CHECK_TIMEOUT


def test_the_repo_is_pinned_by_its_number():
    assert update.REPO_ID == 1421452081 and update.REPO == "Ariliux/MayhemMacro"
    # asked by number, GitHub may name it either way...
    for link in ("https://api.github.com/repos/Ariliux/MayhemMacro/releases/1",
                 "https://api.github.com/repos/ariliux/mayhemmacro/releases/1",
                 "https://api.github.com/repositories/1421452081/releases/1"):
        assert update.release_info(transport(release(url=link))) is not None, link
    # ...but an answer naming another repo is not this repo's release
    for link in ("https://api.github.com/repos/SomeoneElse/MayhemMacro/releases/1",
                 "https://api.github.com/repos/Ariliux/OtherRepo/releases/1",
                 "https://api.github.com/repositories/999/releases/1",
                 "https://evil.example/repos/Ariliux/MayhemMacro/releases/1"):
        assert update.release_info(transport(release(url=link))) is None, link


def test_a_page_outside_the_repo_is_never_used():
    info = update.release_info(transport(release(html_url="https://evil.example/download")))
    assert info["page"] == update.RELEASES_URL
    assert update.RELEASES_URL == "https://github.com/Ariliux/MayhemMacro/releases/latest"


@pytest.mark.parametrize("answer, why", [
    (release(draft=True), "a draft"),
    (release(prerelease=True), "a pre-release"),
    (release(tag_name="latest"), "a tag without a version"),
    ({"message": "API rate limit exceeded"}, "a rate limit"),
    (b"<html>not json</html>", "garbage"),
    ([1, 2], "not an object"),
    (OSError("no network"), "no internet"),
    (TimeoutError("slow"), "a timeout"),
])
def test_no_news_is_not_a_crash(answer, why):
    assert update.release_info(transport(answer)) is None, why


# --------------------------------------------------------- the app's zip ---
@pytest.mark.parametrize("tweak, why", [
    ({"browser_download_url": f"https://evil.example/MayhemMacro-v{NEWER}.zip"},
     "a zip hosted anywhere but this repo"),
    ({"browser_download_url":
      f"https://github.com/Someone/MayhemMacro/releases/download/v{NEWER}/MayhemMacro-v{NEWER}.zip"},
     "a zip from another repo's releases"),
    ({"browser_download_url": f"{update.ASSET_PREFIX}../MayhemMacro-v{NEWER}.zip"},
     "a link that climbs out of the releases"),
    ({"browser_download_url": f"{update.ASSET_PREFIX}v{NEWER}/MayhemMacro-v{NEWER}.zip?x=1"},
     "a link with extras on the end"),
    ({"browser_download_url": f"{update.ASSET_PREFIX}v{NEWER}/other.zip"},
     "a link to a different file"),
    ({"digest": None}, "a zip with no fingerprint"),
    ({"digest": "md5:" + "ab" * 16}, "a fingerprint that isn't sha256"),
    ({"digest": "sha256:" + "ab" * 31}, "a cut-off fingerprint"),
    ({"name": f"MayhemMacro-v{NEWER}.exe"}, "something that isn't the app zip"),
    ({"name": f"OtherMacro-v{NEWER}.zip"}, "another app's zip"),
    ({"name": f"MayhemMacro-v{APP_VER}.zip"}, "a zip of a different version"),
])
def test_only_this_repos_fingerprinted_app_zip_is_installable(tweak, why):
    a = asset()
    a.update(tweak)
    info = update.release_info(transport(release(assets=[a])))
    assert info is not None and info["version"] == NEWER, why     # still "it's out"
    assert info["zip"] is None and info["sha256"] is None, why    # but never installed


def test_the_app_zip_is_found_among_other_files():
    files = [{"name": "Source code.zip", "browser_download_url": "x"}, "junk",
             asset(name=f"MayhemMacro-v{NEWER}.exe"),
             asset(digest="sha256:" + "CD" * 32, size=True)]
    got = update.pick_asset(files, NEWER)
    assert got == (f"{update.ASSET_PREFIX}v{NEWER}/MayhemMacro-v{NEWER}.zip", "cd" * 32, 0)
    assert update.pick_asset(None, NEWER) is None and update.pick_asset([], NEWER) is None


# ------------------------------------------------------------ downloading ---
ZIP_URL = f"{update.ASSET_PREFIX}v{NEWER}/MayhemMacro-v{NEWER}.zip"


def test_a_download_with_the_right_fingerprint_is_kept(tmp_path):
    body = b"x" * 600_000
    sha = hashlib.sha256(body).hexdigest()
    calls, seen = [], []
    dest = str(tmp_path / "update" / "dl.zip")
    ok = update.download_update(ZIP_URL, sha.upper(), dest,
                                lambda done, total: seen.append((done, total)),
                                transport(body, calls, {"Content-Length": str(len(body))}))
    assert ok and open(dest, "rb").read() == body
    assert not os.path.exists(dest + ".part")
    assert seen[-1] == (len(body), len(body)) and len(seen) == 3     # 256 KB at a time
    assert [c["url"] for c in calls] == [ZIP_URL]


def test_a_download_with_the_wrong_fingerprint_is_thrown_away(tmp_path):
    dest = str(tmp_path / "dl.zip")
    assert not update.download_update(ZIP_URL, "00" * 32, dest, transport=transport(b"evil"))
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_a_download_from_anywhere_else_is_never_even_asked_for(tmp_path):
    calls = []
    dest = str(tmp_path / "dl.zip")
    for url in ("https://evil.example/MayhemMacro.zip",
                "https://github.com/Someone/MayhemMacro/releases/download/v1/x.zip",
                "file:///C:/Windows/notepad.exe", "", None):
        assert not update.download_update(url, "ab" * 32, dest,
                                          transport=transport(b"x", calls))
    for sha in ("", None, "ab" * 31, "zz" * 32):
        assert not update.download_update(ZIP_URL, sha, dest, transport=transport(b"x", calls))
    assert calls == [] and not os.listdir(tmp_path)


def test_a_download_far_too_big_is_dropped(tmp_path):
    body = b"x" * 300_000
    dest = str(tmp_path / "dl.zip")
    assert not update.download_update(ZIP_URL, hashlib.sha256(body).hexdigest(), dest,
                                      transport=transport(body), limit=100_000)
    assert not os.listdir(tmp_path)


def test_a_broken_connection_leaves_nothing(tmp_path):
    class Breaks(Resp):
        def read(self, n=-1):
            if self.tell():
                raise ConnectionResetError("gone")
            return super().read(n)
    dest = str(tmp_path / "dl.zip")
    assert not update.download_update(ZIP_URL, "ab" * 32, dest,
                                      transport=lambda req, t: Breaks(b"y" * 600_000))
    assert not os.listdir(tmp_path)


# --------------------------------------------------------------- unpacking ---
def test_a_folder_build_unpacks_and_lists_its_files(tmp_path):
    z = str(tmp_path / "u.zip")
    make_zip(z, BUILD)
    stage = str(tmp_path / "stage")
    exe = update.stage_update(z, stage)
    assert exe == os.path.join(os.path.realpath(stage), "MayhemMacro", "MayhemMacro.exe")
    manifest = json.load(open(os.path.join(stage, "manifest.json")))
    assert manifest == {"MayhemMacro.exe": 7,
                        os.path.join("_internal", "base_library.zip"): 11,
                        os.path.join("_internal", "customtkinter", "theme.json"): 2}


def test_a_build_at_the_zips_top_level_works_too(tmp_path):
    z = str(tmp_path / "u.zip")
    make_zip(z, {k.split("/", 1)[1]: v for k, v in BUILD.items()})
    exe = update.stage_update(z, str(tmp_path / "stage"))
    assert exe and exe.endswith(os.path.join("stage", "MayhemMacro.exe"))


@pytest.mark.parametrize("extra", ["../../outside.txt", "../sneaky.txt",
                                   "/top.txt", "C:/Windows/evil.txt"])
def test_a_zip_that_writes_outside_its_folder_is_refused(tmp_path, extra):
    z = str(tmp_path / "evil.zip")
    make_zip(z, dict(BUILD, **{extra: b"gotcha"}))
    stage = tmp_path / "a" / "stage"
    assert update.stage_update(z, str(stage)) is None
    assert not (tmp_path / "outside.txt").exists() and not (tmp_path / "a" / "sneaky.txt").exists()
    assert os.listdir(stage) == []                         # refused before unpacking anything


@pytest.mark.parametrize("files", [
    {"MayhemMacro/MayhemMacro.exe": b"x"},                         # no _internal
    {"MayhemMacro/_internal/a.dll": b"x"},                         # no exe
    {"OtherApp/OtherApp.exe": b"x", "OtherApp/_internal/a": b"x"},  # not MayhemMacro
])
def test_a_zip_that_isnt_the_app_build_is_refused(tmp_path, files):
    z = str(tmp_path / "u.zip")
    make_zip(z, files)
    assert update.stage_update(z, str(tmp_path / "stage")) is None


def test_a_broken_zip_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 not really a zip")
    assert update.stage_update(str(bad), str(tmp_path / "stage")) is None


# --------------------------------------------------------- swapping files ---
@pytest.fixture
def staged(tmp_path):
    """The new version, unpacked: (its folder, its manifest)."""
    z = str(tmp_path / "u.zip")
    make_zip(z, BUILD)
    stage = str(tmp_path / "stage")
    exe = update.stage_update(z, stage)
    return os.path.dirname(exe), json.load(open(os.path.join(stage, "manifest.json")))


def state_of(folder) -> dict:
    return {os.path.relpath(os.path.join(b, f), folder): open(os.path.join(b, f), "rb").read()
            for b, _d, fs in os.walk(folder) for f in fs}


def test_the_swap_is_guarded(tmp_path, staged):
    app = install(tmp_path / "app")
    before = state_of(app)
    with pytest.raises(safety.SideEffectBlocked):
        update.swap_in_update(staged[0], app, staged[1])
    assert state_of(app) == before and sorted(os.listdir(app)) == [
        "MayhemMacro.exe", "_internal", "my notes.txt"]


def test_the_swap_puts_in_exactly_the_new_files(monkeypatch, tmp_path, staged):
    lifted = lift(monkeypatch, update.GUARD_SWAP)
    app = install(tmp_path / "app")
    assert update.swap_in_update(staged[0], app, staged[1]) is True
    assert lifted.asked == [update.GUARD_SWAP]
    assert state_of(app) == {
        "MayhemMacro.exe": b"NEW-EXE",
        os.path.join("_internal", "base_library.zip"): b"new library",   # old.dll is gone
        os.path.join("_internal", "customtkinter", "theme.json"): b"{}",
        "my notes.txt": b"keep me"}                                      # never touched
    assert sorted(os.listdir(app)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]


def test_a_swap_that_fails_half_way_changes_nothing(monkeypatch, tmp_path):
    lift(monkeypatch, update.GUARD_SWAP)
    app = install(tmp_path / "app")
    before = state_of(app)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "MayhemMacro.exe").write_bytes(b"X")             # no _internal
    with pytest.raises(OSError):
        update.swap_in_update(str(broken), app)
    assert state_of(app) == before
    assert sorted(os.listdir(app)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]


def test_an_incomplete_copy_is_refused(monkeypatch, tmp_path, staged):
    lift(monkeypatch, update.GUARD_SWAP)
    app = install(tmp_path / "app")
    before = state_of(app)
    lying = dict(staged[1], **{os.path.join("_internal", "missing.dll"): 123})
    with pytest.raises(RuntimeError, match="incomplete"):
        update.swap_in_update(staged[0], app, lying)
    assert state_of(app) == before
    assert sorted(os.listdir(app)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]


def test_a_file_in_use_calls_the_switch_off(monkeypatch, tmp_path, staged):
    lift(monkeypatch, update.GUARD_SWAP)
    app = install(tmp_path / "app")
    before = state_of(app)
    with open(os.path.join(app, "_internal", "old.dll"), "rb"):       # "running"
        assert update.swap_in_update(staged[0], app, staged[1], tries=2, wait=0) is False
    assert state_of(app) == before
    assert sorted(os.listdir(app)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]


def test_the_swap_waits_for_the_old_app_then_gives_up(monkeypatch, tmp_path, staged):
    lift(monkeypatch, update.GUARD_SWAP)
    monkeypatch.setattr(update, "_in_use", lambda path: True)       # the old app never ends
    app = install(tmp_path / "app")
    before = state_of(app)
    waits = []
    assert update.swap_in_update(staged[0], app, staged[1], tries=3, wait=0.25,
                                 sleep=waits.append) is False
    assert waits == [0.25] * 3
    assert state_of(app) == before
    assert sorted(os.listdir(app)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]


# ---------------------------------------------- the hand-over and finishing ---
def test_hand_over_and_reopening_are_guarded(tmp_path):
    started = []
    with pytest.raises(safety.SideEffectBlocked):
        update.hand_over("new.exe", str(tmp_path), 1, popen=lambda *a, **k: started.append(a))
    with pytest.raises(safety.SideEffectBlocked):
        update.relaunch(str(tmp_path / "MayhemMacro.exe"))
    with pytest.raises(safety.SideEffectBlocked):
        update.finish_update(str(tmp_path), 1, APP_VER, wait_for_exit=started.append,
                             launch=started.append)
    with pytest.raises(safety.SideEffectBlocked):
        update.tidy_after_update(str(tmp_path))
    assert started == []                     # refused before doing anything at all


def test_hand_over_starts_the_new_version_with_the_finish_arguments(monkeypatch, tmp_path):
    lift(monkeypatch, update.GUARD_START)
    started = []
    new_exe = str(tmp_path / "update" / "new" / "MayhemMacro" / "MayhemMacro.exe")
    update.hand_over(new_exe, r"D:\Games\MayhemMacro", 4242, "1.0.0",
                     popen=lambda args, **kw: started.append((args, kw)))
    (args, kw), = started
    # a contract between versions: an old version starts a newer one with these
    assert args == [new_exe, "--finish-update", r"D:\Games\MayhemMacro", "4242", "1.0.0"]
    assert kw["cwd"] == os.path.dirname(new_exe) and kw["close_fds"] is True
    assert kw["creationflags"] & 0x8 and kw["creationflags"] & 0x200     # detached


def test_finishing_needs_the_built_app_and_the_flag(monkeypatch):
    argv = ["MayhemMacro.exe", "--finish-update", r"C:\x", "1", "1.0.0"]
    monkeypatch.setattr(update, "frozen", lambda: False)
    assert not update.finishing(argv)                       # from source: never
    monkeypatch.setattr(update, "frozen", lambda: True)
    assert update.finishing(argv)
    assert not update.finishing(argv[:4]) and not update.finishing(["x", "--other", "a", "b", "c"])
    assert not update.finishing(["MayhemMacro.exe"])


def finish(monkeypatch, src, app, **kw):
    """finish_update with its swap and install guards lifted and the waiting
    and reopening recorded."""
    lifted = lift(monkeypatch, update.GUARD_INSTALL, update.GUARD_SWAP)
    rec = {"waited": [], "launched": [], "notes": []}
    ok = update.finish_update(app, kw.pop("pid", "4242"), kw.pop("old", "1.0.0"),
                              src_dir=src, log=rec["notes"].append,
                              wait_for_exit=rec["waited"].append,
                              launch=rec["launched"].append, **kw)
    return ok, rec, lifted


def test_finish_update_swaps_reopens_and_leaves_the_users_data_alone(monkeypatch, tmp_path,
                                                                     data, staged):
    app = install(tmp_path / "app")
    before = snapshot(data)
    ok, rec, lifted = finish(monkeypatch, staged[0], app)
    assert ok is True
    assert rec["waited"] == ["4242"]                         # waited for the old app first
    assert lifted.asked[0] == update.GUARD_INSTALL
    assert open(os.path.join(app, "MayhemMacro.exe"), "rb").read() == b"NEW-EXE"
    assert open(os.path.join(app, "my notes.txt")).read() == "keep me"
    assert rec["launched"] == [os.path.join(app, "MayhemMacro.exe")]
    assert rec["notes"] == [f"update installed: 1.0.0 -> {APP_VER}"]
    # settings, stats, log and pictures are exactly as they were
    assert snapshot(data) == before
    assert sorted(os.listdir(data)) == ["events", "log.txt", "settings.json", "stats.json",
                                        "update"]
    # the new app learns it was just updated, once
    assert update.take_updated_note() == "1.0.0"
    assert update.take_updated_note() is None


def test_finish_update_with_the_old_app_still_open_changes_nothing(monkeypatch, tmp_path, staged):
    app = install(tmp_path / "app")
    before = state_of(app)
    monkeypatch.setattr(update, "swap_in_update", lambda *a, **k: False)
    monkeypatch.setattr(update, "_in_use", lambda path: True)    # and someone reopened it
    ok, rec, _ = finish(monkeypatch, staged[0], app)
    assert ok is False and rec["launched"] == []                # no second copy started
    assert state_of(app) == before
    assert "still open" in rec["notes"][0]


def test_a_failed_finish_reopens_the_old_version(monkeypatch, tmp_path, data, staged):
    app = install(tmp_path / "app")
    before = state_of(app)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(update, "swap_in_update", boom)
    ok, rec, _ = finish(monkeypatch, staged[0], app)
    assert ok is False and state_of(app) == before
    assert rec["launched"] == [os.path.join(app, "MayhemMacro.exe")]   # back to the old one
    assert "disk full" in rec["notes"][0] and "nothing changed" in rec["notes"][0]
    assert update.take_updated_note() is None


def test_finish_update_without_the_file_list_changes_nothing(monkeypatch, tmp_path, staged):
    os.remove(os.path.join(os.path.dirname(staged[0]), "manifest.json"))
    app = install(tmp_path / "app")
    before = state_of(app)
    ok, rec, _ = finish(monkeypatch, staged[0], app)
    assert ok is False and state_of(app) == before
    assert "list of new files is missing" in rec["notes"][0]


@pytest.mark.parametrize("where", ["empty", "same"])
def test_finish_update_refuses_a_folder_that_isnt_mayhemmacro(monkeypatch, tmp_path, staged,
                                                             where):
    target = staged[0] if where == "same" else str(tmp_path / "empty")
    os.makedirs(target, exist_ok=True)
    before = state_of(target)
    ok, rec, _ = finish(monkeypatch, staged[0], target)
    assert ok is False and rec["launched"] == [] and state_of(target) == before


# -------------------------------------------------- who may update itself ---
def built_app(folder, name="MayhemMacro.exe", internal=True):
    os.makedirs(folder, exist_ok=True)
    if internal:
        os.makedirs(os.path.join(folder, "_internal"), exist_ok=True)
    exe = os.path.join(folder, name)
    open(exe, "wb").close()
    return exe


INFO = {"version": NEWER, "page": update.RELEASES_URL, "zip": ZIP_URL,
        "sha256": "ab" * 32, "size": 0}


def test_only_the_built_app_updates_itself(monkeypatch, tmp_path, data):
    exe = built_app(str(tmp_path / "app"))
    monkeypatch.setattr(update, "app_exe", lambda: exe)
    monkeypatch.setattr(update, "frozen", lambda: False)
    assert not update.can_self_update(INFO)                 # from source: never
    monkeypatch.setattr(update, "frozen", lambda: True)
    assert update.can_self_update(INFO)
    assert update.install_dir() == str(tmp_path / "app")
    assert sorted(os.listdir(tmp_path / "app")) == ["MayhemMacro.exe", "_internal"]  # probe gone
    for bad in (dict(INFO, zip=None), dict(INFO, sha256=None), None):
        assert not update.can_self_update(bad)
    for exe2 in (built_app(str(tmp_path / "b"), name="python.exe"),
                 built_app(str(tmp_path / "c"), internal=False),
                 built_app(os.path.join(update.staging_dir(), "MayhemMacro"))):
        monkeypatch.setattr(update, "app_exe", lambda e=exe2: e)
        assert not update.can_self_update(INFO), exe2


# ---------------------------------------------------- the real network ---
def test_the_real_network_is_blocked_while_testing(tmp_path):
    req = update._request(update.LATEST_API)
    with pytest.raises(safety.SideEffectBlocked):
        update._http(req, 1)
    assert update.release_info() is None                      # no transport: the guard
    assert not update.download_update(ZIP_URL, "ab" * 32, str(tmp_path / "x.zip"))
    assert not os.listdir(tmp_path)


def test_the_only_places_it_talks_to_are_this_repos():
    """Every web address in update.py is this repo's API (by number) or its
    own releases."""
    import ast
    src = open(update.__file__, encoding="utf-8").read()
    urls = {n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "://" in n.value}
    urls |= {n.values[0].value for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.JoinedStr) and n.values
             and isinstance(n.values[0], ast.Constant) and "://" in str(n.values[0].value)}
    assert urls
    for u in urls:
        assert u.startswith(("https://api.github.com/repos", "https://api.github.com/repositories/",
                             "https://github.com/")), u
    for u in (update.LATEST_API, update.ASSET_PREFIX, update.RELEASES_URL, *update.API_PREFIXES):
        assert "Ariliux/MayhemMacro" in u or "/repositories/1421452081/" in u, u


# ===================================================== the window's side ===
_MISSING = object()


class Label:
    def __init__(self):
        self.kw = {}

    def configure(self, **kw):
        self.kw.update(kw)

    def cget(self, key):
        return self.kw.get(key)


class Switch:
    def __init__(self, on):
        self.on = on

    def get(self):
        return int(self.on)

    def select(self):
        self.on = True

    def deselect(self):
        self.on = False


class App:
    """Just enough of the window for its update methods, without Tk. The
    app's real methods run on it (looked up in MayhemMacro's own class
    body, never Tk's), widgets and drawing are recorders."""

    def __init__(self, **settings):
        self.settings = copy.deepcopy(paths.DEFAULT_SETTINGS)
        self.settings.update(settings)
        self.running = False
        self._stopping = None
        self.logged, self.toasts, self.states, self.later = [], [], [], []
        self.questions, self.closed, self.pages = [], [], []
        self.changed = 0
        self._q_shown = False
        self.lbl_upd = Label()
        M.MayhemMacro._init_updates(self)

    def __getattr__(self, name):
        attr = M.MayhemMacro.__dict__.get(name, _MISSING)
        if attr is _MISSING:
            raise AttributeError(name)
        if isinstance(attr, staticmethod):
            return attr.__func__
        if isinstance(attr, types.FunctionType):
            return types.MethodType(attr, self)
        return attr

    # what the window would draw or schedule
    def _ui(self, fn):
        fn()

    def after(self, ms, fn, *a):
        self.later.append((ms, fn))

    def log(self, text, level="info"):
        self.logged.append((level, text))

    def _toast(self, text, kind="info", action=None):
        self.toasts.append((kind, text, action))

    def set_state(self, title, detail="", colour=None):
        self.states.append((title, detail))

    def _show_page(self, key, remember=True):
        self.pages.append(key)

    def _changed(self, save_now=False):
        self.changed += 1

    def _close(self):
        self.closed.append(True)

    def _paint_badges(self):
        pass

    def _paint_update_link(self):
        pass

    def _show_update_question(self, version):
        self.questions.append(version)
        self._q_shown = True

    def _update_question_shown(self):
        return self._q_shown

    def _hide_update_question(self):
        self._q_shown = False

    def run_later(self):
        while self.later:
            self.later.pop(0)[1]()


class Threads:
    """M.threading with Thread replaced: run=True runs the target at once,
    run=False only records it."""

    def __init__(self, run=True):
        self.made, self.run = [], run

    def Thread(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        threads = self

        class T:
            def start(self):
                threads.made.append(name)
                if threads.run:
                    target(*args, **(kwargs or {}))

            def is_alive(self):
                return False
        return T()


@pytest.fixture
def threads(monkeypatch):
    t = Threads()
    monkeypatch.setattr(M, "threading", t)
    return t


def answers(monkeypatch, info):
    """update.release_info, as the app calls it, answers `info`."""
    asked = []
    monkeypatch.setattr(M.update, "release_info", lambda *a, **k: asked.append(1) or info)
    return asked


@pytest.mark.parametrize("version, found", [(APP_VER, False), (OLDER, False),
                                            (NEWER, True), (bump(NEWER), True)])
def test_a_check_only_notices_a_newer_version(monkeypatch, version, found):
    monkeypatch.setattr(update, "frozen", lambda: True)
    answers(monkeypatch, dict(INFO, version=version))
    app = App()
    assert M.MayhemMacro._check_updates_once(app) is found
    assert (app._update_info is not None) is found
    assert app.questions == ([version] if found else [])
    if found:
        assert ("info", f"update available: MayhemMacro {version}") in app.logged
        assert "click here to update" in app.lbl_upd.kw["text"]


def test_up_to_date_and_offline_each_leave_one_quiet_line(monkeypatch):
    app = App()
    answers(monkeypatch, dict(INFO, version=APP_VER))
    app._check_updates_once()
    assert app.logged == [("info", f"update check: up to date (latest on GitHub is {APP_VER})")]
    assert app.lbl_upd.kw["text"].startswith("Up to date") and app.lbl_upd.kw["text_color"] == M.GREEN
    answers(monkeypatch, None)
    app._check_updates_once()
    assert app.logged[-1] == ("info", "update check: couldn't reach GitHub - will try later")
    assert app.lbl_upd.kw["text_color"] == M.AMBER
    assert app.toasts == [] and app.questions == []


def test_with_the_switch_off_github_is_never_asked(monkeypatch, threads):
    asked = answers(monkeypatch, dict(INFO))
    app = App(check_updates=False)
    assert app._check_updates_once() is False
    app._check_now()
    assert asked == [] and threads.made == [] and app._update_info is None
    assert app.lbl_upd.kw["text"] == "Turn on Check for updates first."


def test_check_now_says_what_it_found(monkeypatch, threads):
    answers(monkeypatch, dict(INFO, version=APP_VER))
    app = App()
    app._check_now()
    assert threads.made == ["MayhemMacro check"] and app._checking is False
    assert app.toasts[-1][0] == "ok" and app.toasts[-1][1].startswith("Up to date")


def test_the_background_check_never_starts_while_testing(threads):
    app = App()
    app._start_update_checks()
    assert threads.made == []


def test_it_never_asks_during_a_run_and_asks_once_it_stops(monkeypatch):
    monkeypatch.setattr(update, "frozen", lambda: True)
    app = App()
    app.running = True
    app._show_update(dict(INFO))
    app._update_tick()
    assert app.questions == [] and app._update_pending          # waits
    app.running = False                                          # Stop / F2
    app._update_tick()
    assert [ms for ms, _ in app.later] == [1500]
    app.run_later()
    assert app.questions == [NEWER]                              # "v1.0.1 is available..."
    app._update_tick()
    assert app.questions == [NEWER]                              # once


def test_a_question_on_screen_goes_away_when_a_run_starts(monkeypatch):
    monkeypatch.setattr(update, "frozen", lambda: True)
    app = App()
    app._show_update(dict(INFO))
    assert app._q_shown
    app.running = True                                           # F1 with it still up
    app._update_tick()
    assert not app._q_shown and app._update_pending
    app.running = False
    app._update_tick()
    app.run_later()
    assert app.questions == [NEWER, NEWER] and app._q_shown      # asked again after


def test_no_changes_nothing(monkeypatch, threads):
    monkeypatch.setattr(update, "frozen", lambda: True)
    app = App()
    app._show_update(dict(INFO))
    app._answer_update("no")
    assert not app._q_shown and not app._updating and threads.made == []
    assert app.closed == [] and app.toasts[-1][0] == "info"
    assert ("info", "update question: no") in app.logged


def test_a_yes_during_a_run_waits_for_the_run_to_stop(monkeypatch):
    app = App()
    app._update_info = dict(INFO)
    started = []
    monkeypatch.setattr(app, "_start_update", lambda: started.append(True), raising=False)
    app.running = True
    app._update_answer("yes")
    assert started == [] and app._update_yes_pending
    app._update_tick()
    app.running = False
    app._update_tick()
    app.run_later()
    assert started == [True] and not app._update_yes_pending


def test_start_update_never_runs_during_a_run(monkeypatch, threads):
    monkeypatch.setattr(update, "frozen", lambda: True)
    app = App()
    app._update_info = dict(INFO)
    app.running = True
    app._start_update()
    assert threads.made == [] and not app._updating


def test_from_source_it_only_says_a_new_version_is_out(monkeypatch, threads):
    monkeypatch.setattr(update, "frozen", lambda: False)
    app = App()
    app._show_update(dict(INFO))
    assert app.questions == []                                   # nothing to say Yes to
    kind, text, _ = app.toasts[-1]
    assert NEWER in text and "source" in text
    assert "runs from source" in app.lbl_upd.kw["text"]
    app._start_update()                                          # even if it got here
    assert threads.made == [] and not app._updating
    assert app.toasts[-1][1] == "Opening the browser is off while testing"   # guarded


def test_yes_in_the_built_app_downloads_unpacks_hands_over_and_closes(
        monkeypatch, tmp_path, data, threads):
    exe = built_app(str(tmp_path / "MayhemMacro App"))
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(update, "app_exe", lambda: exe)
    z = str(tmp_path / "release.zip")
    sha = make_zip(z, BUILD)
    body = open(z, "rb").read()
    got = []
    # the download, served by a fake GitHub
    monkeypatch.setattr(update, "_http",
                        transport(body, got, {"Content-Length": str(len(body))}))
    handed = []
    monkeypatch.setattr(M.update, "hand_over", lambda *a: handed.append(a))
    before = snapshot(data)

    app = App()
    app._show_update(dict(INFO, sha256=sha, size=len(body)))
    assert app.questions == [NEWER]
    app._answer_update("yes")

    assert [g["url"] for g in got] == [ZIP_URL]
    new_exe = os.path.join(os.path.realpath(update.staging_dir()), "MayhemMacro",
                           "MayhemMacro.exe")
    assert handed == [(new_exe, str(tmp_path / "MayhemMacro App"), os.getpid(), APP_VER)]
    assert open(new_exe, "rb").read() == b"NEW-EXE"
    assert app.closed == [True] and app._updating                # closes; F1 stays refused
    assert app.pages == ["home"]
    details = [d for t, d in app.states if t == f"Updating to {NEWER}"]
    assert details[0] == "Downloading · 0%" and "Unpacking…" in details
    assert any(d.startswith("Downloading · 100% of") for d in details)
    assert not os.path.exists(os.path.join(update.update_dir(), f"MayhemMacro-v{NEWER}.zip"))
    assert snapshot(data) == before                              # the user's files untouched
    # the install itself is only swapped by the new version, after we've closed
    assert open(exe, "rb").read() == b""


def test_a_download_that_doesnt_match_changes_nothing(monkeypatch, tmp_path, data, threads):
    exe = built_app(str(tmp_path / "app"))
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(update, "app_exe", lambda: exe)
    monkeypatch.setattr(update, "_http", transport(b"not what GitHub listed"))
    handed = []
    monkeypatch.setattr(M.update, "hand_over", lambda *a: handed.append(a))
    app = App()
    app._update_info = dict(INFO)
    app._start_update()
    assert handed == [] and app.closed == [] and not app._updating
    assert app.states[-1][0] == "Update didn't work"
    kind, text, action = app.toasts[-1]
    assert kind == "warn" and action[0] == "Download page"
    assert any("fingerprint" in t for _, t in app.logged)
    assert not os.path.exists(update.staging_dir())


def test_a_copy_that_cant_update_itself_offers_the_download_page(monkeypatch, tmp_path,
                                                                 threads):
    exe = built_app(str(tmp_path / "app"), internal=False)     # not the folder build
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(update, "app_exe", lambda: exe)
    app = App()
    app._update_info = dict(INFO)
    app._start_update()
    assert threads.made == [] and not app._updating
    assert any("can't update itself" in t for _, t in app.logged)


def test_start_is_refused_while_updating():
    app = App()
    app._updating = True
    app.toggle_run()
    assert app.toasts[-1][1].startswith("Updating")
    assert app.running is False


def test_the_two_switches_agree(monkeypatch):
    app = App()
    about, settings = Switch(True), Switch(True)
    app._update_switches = [about, settings]
    about.on = False
    app._update_switched(about)
    assert app.settings["check_updates"] is False and settings.on is False and app.changed == 1
    assert app.lbl_upd.kw["text"] == "Update checks are off."
    settings.on = True
    app._update_switched(settings)
    assert app.settings["check_updates"] is True and about.on is True


def test_a_normal_start_never_touches_the_update_folder(monkeypatch, data, threads):
    staging = os.path.join(update.update_dir(), "new")
    os.makedirs(staging)
    threads.run = False
    app = App()
    app._after_update()
    assert threads.made == [] and os.path.isdir(staging) and app.logged == []


def test_the_first_start_after_an_update_says_so_and_tidies(monkeypatch, tmp_path, data,
                                                            threads):
    os.makedirs(update.staging_dir())
    with open(os.path.join(update.update_dir(), "updated.txt"), "w") as fh:
        fh.write("1.0.0")
    threads.run = False                       # the tidy-up waits 20 s: not run here
    before = snapshot(data)
    app = App()
    app._after_update()
    assert app.logged == [("info", f"updated: MayhemMacro 1.0.0 -> {APP_VER}")]
    assert app.toasts[-1][0] == "ok" and threads.made == ["MayhemMacro tidy"]
    # what that thread does, with its guard lifted on this test's folders
    target = install(tmp_path / "app")
    os.makedirs(os.path.join(target, "_internal.old"))
    lift(monkeypatch, update.GUARD_TIDY)
    update.tidy_after_update(target)
    assert not os.path.exists(update.update_dir())
    assert sorted(os.listdir(target)) == ["MayhemMacro.exe", "_internal", "my notes.txt"]
    assert snapshot(data) == before
