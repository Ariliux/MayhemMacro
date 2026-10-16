"""Updates: is there a newer MayhemMacro, and putting it in place.

Checking is one read-only request to GitHub's public API for the newest
release of THIS repo. It asks by the repo's permanent number (REPO_ID), not
by its name: if the account were ever renamed or deleted, someone could
register the name again and publish a fake MayhemMacro, but nobody can ever
get the number. The answer must also still name Ariliux/MayhemMacro (the
release's own API link, its page and the zip's download link), so the number
and the name are the same repo before anything in it is trusted. Nothing
about the user goes along: no settings, no ids, only the app's name and
version in the User-Agent, like any web request.

A release is installed only when its zip is the app build for that very
version (MayhemMacro-v<version>.zip), its link is one of this repo's own
release downloads, and GitHub lists a sha256 fingerprint for it that the
download matches. Anything less and the app only says a new version is out.

Installing happens only in the built app (sys.frozen), never during a run:
  1. download_update()  the zip into <data>\\update, fingerprinted on the way
  2. stage_update()     unpacked into <data>\\update\\new (nothing may land
                        outside it), with a list of every file and its size
  3. hand_over()        starts the NEW MayhemMacro.exe with
                        --finish-update <install folder> <old pid> <old version>
                        and the app closes
  4. finish_update()    runs in the new version: waits for the old app to
                        end, swap_in_update() puts MayhemMacro.exe and
                        _internal in place, and the updated app opens
The --finish-update arguments are a contract between versions (an old
version starts a newer one with them), so they must never change.

Only MayhemMacro.exe and _internal in the install folder are replaced.
settings.json, stats.json and events\\ in %LOCALAPPDATA%\\MayhemMacro are
never opened by any of this (log.txt just gets its usual lines); the
update's own files live in its update\\ folder and are removed afterwards.

Every request (_http), the swap, the install, the clean-up and every program
start call safety.guard() first, so while MAYHEM_TESTING=1 none of them can
happen. Tests pass in fake transports and lift single guards on temp folders.
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile

from mayhem import APP_NAME, APP_VER
from mayhem import paths, safety

REPO = "Ariliux/MayhemMacro"
# The repo's permanent number: asked by this, never by the name above.
REPO_ID = 1421452081
LATEST_API = f"https://api.github.com/repositories/{REPO_ID}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
# GitHub's own links for a release of this repo: asked by number, it may
# answer with either form, but never with another repo's name.
API_PREFIXES = (f"https://api.github.com/repos/{REPO}/releases/",
                f"https://api.github.com/repositories/{REPO_ID}/releases/")
PAGE_PREFIX = f"https://github.com/{REPO}/releases/"
ASSET_RE = re.compile(r"MayhemMacro-v(\d+(?:\.\d+){0,3})\.zip")
DIGEST_RE = re.compile(r"sha256:([0-9a-fA-F]{64})")

EXE_NAME = f"{APP_NAME}.exe"
INTERNAL = "_internal"
FINISH_FLAG = "--finish-update"
MANIFEST = "manifest.json"
UPDATED_FLAG = "updated.txt"

UPDATE_EVERY = 12 * 3600        # runs last for days, so check again now and then
CHECK_TIMEOUT = 6.0
DOWNLOAD_TIMEOUT = 30
MAX_ANSWER = 4 * 1024 * 1024    # a release's JSON is a few KB
MAX_DOWNLOAD = 512 * 1024 * 1024        # the app zip is ~60 MB
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
USER_AGENT = f"{APP_NAME}/{APP_VER}"

# What each guard says, so a test can lift exactly one of them.
GUARD_NET = "ask GitHub for an update"
GUARD_SWAP = "replace MayhemMacro's files with an update"
GUARD_INSTALL = "install an update"
GUARD_START = "start MayhemMacro"
GUARD_TIDY = "tidy up after an update"


# ------------------------------------------------------------- versions ---
def version_tuple(v) -> tuple[int, ...]:
    """'v1.0.3' -> (1, 0, 3), '1.1' -> (1, 1, 0); no number at all -> ().

    Only the first three numbers count, so a tag like 'v1.0.4-x64' can't look
    newer than 1.0.4 itself."""
    nums = [int(n) for n in re.findall(r"\d+", str(v or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums))) if nums else ()


def is_newer(version, than: str = APP_VER) -> bool:
    """Is `version` a later release than `than` (this app)?"""
    new = version_tuple(version)
    return bool(new) and new > version_tuple(than)


# --------------------------------------------------- what GitHub answers ---
def _starts(url, prefixes) -> bool:
    low = str(url or "").lower()
    return any(low.startswith(p.lower()) for p in prefixes)


def pick_asset(assets, version) -> tuple[str, str, int] | None:
    """The app zip for `version` among a release's files, as (download link,
    sha256, size in bytes or 0), or None.

    Only MayhemMacro-v<version>.zip, only a link into this repo's own release
    downloads, and only with the sha256 fingerprint GitHub lists for it: no
    fingerprint, no auto-update."""
    want = version_tuple(version)
    link_re = (re.escape(ASSET_PREFIX) + r"(?!\.\.?/)[^/?#\\\s]+/")
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        link = str(asset.get("browser_download_url") or "")
        m = ASSET_RE.fullmatch(name)
        d = DIGEST_RE.fullmatch(str(asset.get("digest") or ""))
        if not (m and d and want and version_tuple(m.group(1)) == want):
            continue
        if not re.fullmatch(link_re + re.escape(name), link):
            continue
        size = asset.get("size")
        ok_size = isinstance(size, int) and not isinstance(size, bool) and size > 0
        return link, d.group(1).lower(), size if ok_size else 0
    return None


def parse_release(data) -> dict | None:
    """GitHub's answer -> {"version", "page", "zip", "sha256", "size"}.

    None when it isn't a published release of this repo: a draft, a
    pre-release, a tag without a version, an error message (a rate limit),
    or an answer whose own link names another repo. "zip" and "sha256" are
    None when there's nothing safe to install (see pick_asset); the app then
    only says the new version is out."""
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if data.get("draft") or data.get("prerelease") or not version_tuple(tag):
        return None
    api = data.get("url")
    if api and not _starts(api, API_PREFIXES):
        return None           # that number now answers with another repo's name
    page = str(data.get("html_url") or "")
    if not _starts(page, (PAGE_PREFIX,)):
        page = RELEASES_URL   # only ever our own release page
    version = tag.lstrip("vV")
    zip_url, sha, size = pick_asset(data.get("assets"), version) or (None, None, 0)
    return {"version": version, "page": page, "zip": zip_url, "sha256": sha,
            "size": size}


# ------------------------------------------------------------ the network ---
def _http(req, timeout):
    """The one place an update request leaves the app. urlopen is looked up on
    every call, so a test that fakes urllib.request.urlopen still sees it."""
    safety.guard(GUARD_NET)
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)


def _request(url: str, accept: str | None = None):
    import urllib.request
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.Request(url, headers=headers)


def release_info(transport=None, timeout: float = CHECK_TIMEOUT) -> dict | None:
    """The newest published release (see parse_release), or None.

    Never raises: no network, a GitHub outage, a rate limit or the testing
    guard all just mean "no news", and the app carries on as before."""
    send = transport or _http
    try:
        with send(_request(LATEST_API, "application/vnd.github+json"), timeout) as r:
            data = json.loads(r.read(MAX_ANSWER).decode("utf-8"))
    except Exception:
        return None
    return parse_release(data)


def download_update(url, sha256, dest, progress=None, transport=None,
                    timeout: float = DOWNLOAD_TIMEOUT, limit: int = MAX_DOWNLOAD) -> bool:
    """Download `url` to `dest`, fingerprinting it on the way. True only when
    its SHA-256 is `sha256`. A mismatch, a link outside this repo's release
    downloads, a file far too big or a broken connection all leave no file
    behind. progress(done, total) is called as it goes (total 0 = unknown)."""
    want = str(sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want) or not str(url or "").startswith(ASSET_PREFIX):
        return False
    tmp = dest + ".part"
    h = hashlib.sha256()
    send = transport or _http
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with send(_request(url), timeout) as r, open(tmp, "wb") as out:
            try:
                total = int((getattr(r, "headers", None) or {}).get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise ValueError("far bigger than any release")
                out.write(chunk)
                h.update(chunk)
                if progress:
                    progress(done, total)
        if h.hexdigest() != want:
            raise ValueError("fingerprint mismatch")
        os.replace(tmp, dest)
        return True
    except Exception:
        _remove(tmp)
        return False


# ------------------------------------------------------- the update folder ---
def update_dir() -> str:
    """<data>\\update: the download, the unpacked copy and the 'just updated'
    note. Nothing else in the data folder is ever used by an update."""
    return os.path.join(paths.data_dir(), "update")


def staging_dir() -> str:
    return os.path.join(update_dir(), "new")


def frozen() -> bool:
    """True in the built app (PyInstaller), False from source."""
    return bool(getattr(sys, "frozen", False))


def app_exe() -> str:
    return os.path.abspath(sys.executable)


def install_dir() -> str:
    """The folder MayhemMacro.exe runs from."""
    return os.path.dirname(app_exe())


def finishing(argv) -> bool:
    """Was this process started by an older version to finish an update?"""
    return frozen() and len(argv) >= 5 and argv[1] == FINISH_FLAG


def can_self_update(info) -> bool:
    """Can this copy replace itself with `info`'s release? Only the built app
    (a MayhemMacro.exe with _internal beside it), only with a zip and its
    fingerprint, and only in a folder it may write to. Otherwise the app just
    says a new version is out."""
    if not frozen() or not isinstance(info, dict):
        return False
    if not info.get("zip") or not info.get("sha256"):
        return False
    exe = app_exe()
    here = os.path.dirname(exe)
    if os.path.basename(exe).lower() != EXE_NAME.lower():
        return False
    if not os.path.isdir(os.path.join(here, INTERNAL)):
        return False
    try:
        upd = os.path.normcase(os.path.realpath(update_dir()))
        mine = os.path.normcase(os.path.realpath(here))
        if mine == upd or mine.startswith(upd + os.sep):
            return False                 # never install over the unpacked copy itself
    except Exception:
        return False
    probe = os.path.join(here, ".mayhem-write-test")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def stage_update(zip_path, stage_dir) -> str | None:
    """Unpack a downloaded update into stage_dir. The path of its
    MayhemMacro.exe, or None when the zip isn't a MayhemMacro folder build
    (MayhemMacro.exe with _internal beside it), is broken, or tries to put
    anything outside stage_dir."""
    shutil.rmtree(stage_dir, ignore_errors=True)
    os.makedirs(stage_dir, exist_ok=True)
    root = os.path.realpath(stage_dir)
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > MAX_UNPACKED:
                return None
            for info in infos:                    # nothing may land outside
                dest = os.path.realpath(os.path.join(root, info.filename))
                if dest != root and not dest.startswith(root + os.sep):
                    return None
            z.extractall(root)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        return None
    for folder in (os.path.join(root, APP_NAME), root):
        exe = os.path.join(folder, EXE_NAME)
        if os.path.isfile(exe) and os.path.isdir(os.path.join(folder, INTERNAL)):
            # every file and its size, so the swap can refuse a staged copy
            # that lost files after unpacking
            sizes = {}
            for base, _dirs, files in os.walk(folder):
                for f in files:
                    full = os.path.join(base, f)
                    sizes[os.path.relpath(full, folder)] = os.path.getsize(full)
            with open(os.path.join(root, MANIFEST), "w", encoding="utf-8") as fh:
                json.dump(sizes, fh)
            return exe
    return None


# ----------------------------------------------------- handing over to it ---
def hand_over(new_exe, target_dir, pid, old_version: str = APP_VER, popen=None) -> None:
    """Start the unpacked new version to finish the update once this app has
    closed. Detached, so it outlives us. Raises if it can't be started."""
    safety.guard(GUARD_START)
    if popen is None:
        import subprocess
        popen = subprocess.Popen
    popen([new_exe, FINISH_FLAG, target_dir, str(pid), str(old_version)],
          cwd=os.path.dirname(new_exe), close_fds=True,
          creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP


def _wait_for_exit(pid, seconds: float = 30) -> None:
    """Wait (at most `seconds`) for the old MayhemMacro to end."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if handle:
            k32.WaitForSingleObject(handle, int(seconds * 1000))
            k32.CloseHandle(handle)
    except Exception:
        pass


def _in_use(path) -> bool:
    """Is this file open in a running program (or not openable at all)?"""
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def swap_in_update(src_dir, dst_dir, manifest=None, tries: int = 60,
                   wait: float = 0.5, sleep=time.sleep) -> bool:
    """Put the new MayhemMacro.exe and _internal from src_dir into dst_dir.

    The slow part, copying, goes into _internal.new and MayhemMacro.exe.new
    NEXT TO the install, which stays complete and runnable the whole time.
    Only once the old exe is free do three quick renames switch over, and if
    any of them fails they're undone. True when switched; False when nothing
    changed (the old app is still running). Nothing else in dst_dir is ever
    touched: not a settings file, not a folder of the user's.
    """
    safety.guard(GUARD_SWAP)
    internal = os.path.join(dst_dir, INTERNAL)
    new_int, old_int = internal + ".new", internal + ".old"
    exe = os.path.join(dst_dir, EXE_NAME)
    new_exe = exe + ".new"

    def drop_new():
        shutil.rmtree(new_int, ignore_errors=True)
        _remove(new_exe)

    for leftover in (new_int, old_int):
        shutil.rmtree(leftover, ignore_errors=True)
    # 1. copy, and check the copy against the list made at unpacking
    try:
        shutil.copytree(os.path.join(src_dir, INTERNAL), new_int)
        shutil.copy2(os.path.join(src_dir, EXE_NAME), new_exe)
        for rel, size in (manifest or {}).items():
            parts = os.path.normpath(str(rel)).split(os.sep)
            if parts == [EXE_NAME]:
                here = new_exe
            elif parts[0] == INTERNAL and ".." not in parts:
                here = os.path.join(new_int, *parts[1:])
            else:
                continue                     # not part of what gets installed
            if not os.path.isfile(here) or os.path.getsize(here) != size:
                raise RuntimeError(f"the new files are incomplete ({rel})")
    except Exception:
        drop_new()
        raise
    # 2. wait until the old exe is free (the old app may take a moment to end)
    for _ in range(max(1, tries)):
        if not os.path.exists(exe) or not _in_use(exe):
            break
        sleep(wait)
    else:
        drop_new()
        return False
    # 3. switch with renames; undo them if any fails
    had_old = os.path.isdir(internal)
    try:
        if had_old:
            os.rename(internal, old_int)          # fails if anything runs from it
    except OSError:
        drop_new()
        return False
    moved_in = False
    try:
        os.rename(new_int, internal)
        moved_in = True
        os.replace(new_exe, exe)
    except Exception:
        if moved_in:
            os.rename(internal, new_int)
        if had_old:
            os.rename(old_int, internal)
        drop_new()
        raise
    shutil.rmtree(old_int, ignore_errors=True)
    return True


def _read_manifest(src_dir) -> dict | None:
    for folder in (os.path.dirname(src_dir), src_dir):
        try:
            with open(os.path.join(folder, MANIFEST), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return None


def relaunch(exe) -> None:
    """Open MayhemMacro (the updated one, or the old one if nothing changed)."""
    safety.guard(GUARD_START)
    os.startfile(exe)


def finish_update(dst_dir, old_pid, old_version, *, src_dir=None, log=None,
                  wait_for_exit=None, launch=None) -> bool:
    """Runs in the NEW version, started by the old one with --finish-update
    from the update folder: wait for the old app to end, swap the files in,
    and open the updated app. No window of its own. True if installed."""
    safety.guard(GUARD_INSTALL)
    note = log or (lambda line: None)
    (wait_for_exit or _wait_for_exit)(old_pid)
    src_dir = os.path.abspath(src_dir or os.path.dirname(app_exe()))
    dst_dir = os.path.abspath(str(dst_dir))
    exe = os.path.join(dst_dir, EXE_NAME)
    ok = False
    if (os.path.normcase(src_dir) == os.path.normcase(dst_dir)
            or not os.path.isfile(exe)):
        note(f"update failed: {dst_dir} isn't a {APP_NAME} folder - nothing changed")
        return False
    manifest = _read_manifest(src_dir)
    if manifest is None:
        note("update failed: the list of new files is missing - nothing changed")
    else:
        try:
            ok = swap_in_update(src_dir, dst_dir, manifest)
            if not ok:
                note(f"update skipped: {APP_NAME} was still open - nothing changed")
        except Exception as exc:
            note(f"update failed while copying: {exc} - nothing changed")
            ok = False
    if ok:
        try:
            os.makedirs(update_dir(), exist_ok=True)
            with open(os.path.join(update_dir(), UPDATED_FLAG), "w",
                      encoding="utf-8") as fh:
                fh.write(str(old_version))
        except Exception:
            pass
        note(f"update installed: {old_version} -> {APP_VER}")
    elif _in_use(exe):
        # the old copy is open again (someone double-clicked it): leave it be
        # rather than starting a second one
        return False
    try:
        (launch or relaunch)(exe)
    except Exception as exc:
        note(f"couldn't reopen {APP_NAME}: {exc}")
    return ok


# ------------------------------------------------- the first start after ---
def take_updated_note() -> str | None:
    """The version this app was updated from, if it was just updated (the
    note is removed, so this answers only once). None on any other start."""
    path = os.path.join(update_dir(), UPDATED_FLAG)
    try:
        with open(path, encoding="utf-8") as fh:
            old = fh.read().strip()
        os.remove(path)
    except OSError:
        return None
    return old or "?"


def tidy_after_update(target_dir=None) -> None:
    """Only on the first start after an update (never otherwise: a hand-over
    might be running from the update folder right then): remove the update
    folder and an old _internal the swap couldn't delete."""
    safety.guard(GUARD_TIDY)
    shutil.rmtree(update_dir(), ignore_errors=True)
    if target_dir:
        shutil.rmtree(os.path.join(target_dir, INTERNAL + ".old"), ignore_errors=True)


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
