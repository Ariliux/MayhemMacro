"""Where MayhemMacro keeps its files, and settings.json.

Everything lives in %LOCALAPPDATA%\\MayhemMacro, never beside the program: a
PyInstaller build unpacks to a temp folder that is gone on the next launch.
MAYHEM_DATA_DIR points it somewhere else (the tests always set it). Nothing
here creates a folder or a file at import time.
"""
import copy
import json
import math
import os
import shutil
import time

from mayhem import APP_NAME, MAPS
from mayhem import safety

# The settings file as a fresh install has it. load_settings() lays the saved
# file over this, so a key added in a later version just appears with its
# default. vote_maps = the ticked maps in rank order (index 0 = most wanted).
DEFAULT_SETTINGS: dict = {
    "private_server_link": "",
    "auto_rejoin": True,
    "anti_afk": True,
    "anti_afk_after_min": 10,
    "auto_vote": True,
    "vote_maps": [],
    "vote_click_offset": [0, 0],
    "rare_alerts": True,
    "rare_min": 100000,
    "rare_ping_min": 1000000,
    "alert_variants_always": True,
    "storage_alert": True,
    "afk_bring_to_front": False,
    "afk_variety": True,
    "session_bar": True,
    "bar_rare_min": 10000000,
    "open_pinned": True,
    "center_cursor": True,
    "start_jump": True,
    "attach_picture": False,
    "save_pictures": False,
    "medal_clip": False,
    "medal_key": "F4",
    "medal_min": 10000000,
    "close_esc_menu": True,
    "keep_awake": True,
    "discord": {"enabled": False, "webhook": "", "user_id": "",
                "send": {"rare": True, "run": True, "rejoin": True, "storage": True,
                         "error": True, "stuck": True, "hourly": False},
                "mention": {"rare": True, "error": True, "stuck": True}},
    "check_updates": True,
    "ui": {"geometry": "", "page": "home", "sidebar_collapsed": False, "pin": False},
    "settings_version": 2,
}

# 1.0.0 saved "check_updates": false with every settings file, but it had no
# update checks and no switch for them: that false was only the old default.
# A file without settings_version is from 1.0.0, so its check_updates is
# dropped once and the new default applies. From version 2 on, whatever is
# saved is the user's own choice and is kept.
SETTINGS_VERSION = 2

SAVE_TRIES = 3          # os.replace can fail for a moment while antivirus or
RETRY_WAIT = 0.1        # a backup tool has the old file open


# ---------------------------------------------------------------- folders ---
def data_dir() -> str:
    """$MAYHEM_DATA_DIR if set, else %LOCALAPPDATA%\\MayhemMacro. Never creates it.

    While testing, the real folder is off limits: without MAYHEM_DATA_DIR this
    raises SideEffectBlocked instead of quietly using the user's real data.
    """
    override = os.environ.get("MAYHEM_DATA_DIR")
    if override:
        return override
    safety.guard("the real MayhemMacro data folder")       # raises only while testing
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, APP_NAME)


def ensure_data_dir() -> str:
    """data_dir(), created if it isn't there yet."""
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def settings_path() -> str:
    return os.path.join(data_dir(), "settings.json")


def stats_path() -> str:
    return os.path.join(data_dir(), "stats.json")


def log_path() -> str:
    return os.path.join(data_dir(), "log.txt")


def events_dir() -> str:
    """Pictures of vote screens and rare banners. Not created here: whoever
    saves the first picture makes the folder."""
    return os.path.join(data_dir(), "events")


# ------------------------------------------------------------ JSON files ---
def atomic_write_json(path: str, data, *, indent: int = 2, backup: bool = False) -> bool:
    """Write `data` to `path` in one piece: a half-written file can't happen.

    tmp file, flush + fsync, then os.replace (tried SAVE_TRIES times). With
    backup=True the previous file is copied to <path>.bak first. Never raises
    for a disk problem; returns False instead.
    """
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        _remove_quietly(tmp)
        return False
    if backup and os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    for attempt in range(SAVE_TRIES):
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            if attempt + 1 < SAVE_TRIES:
                time.sleep(RETRY_WAIT * (attempt + 1))
    _remove_quietly(tmp)
    return False


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------- settings ---
def _fits(default, value) -> bool:
    """Is `value` the same kind of thing as `default`? A bool is not a
    number here, even though Python says True == 1."""
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    return isinstance(value, type(default))


def _merge(defaults: dict, data: dict) -> dict:
    """`data` laid over `defaults`, recursively. Keys we don't know are kept
    as they are; a known key with the wrong type gets its default back."""
    out = copy.deepcopy(defaults)
    for key, value in data.items():
        if key not in defaults:
            out[key] = copy.deepcopy(value)
            continue
        default = defaults[key]
        # 10.0 typed by hand into an int setting is still 10
        if (isinstance(default, int) and not isinstance(default, bool)
                and isinstance(value, float) and value.is_integer()):
            value = int(value)
        if not _fits(default, value):
            continue
        if isinstance(default, dict):
            out[key] = _merge(default, value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _fix_lists(st: dict) -> dict:
    """The two list settings get a closer look than 'is it a list'."""
    names = {m.lower(): m for m in MAPS}
    ranked: list[str] = []
    for item in st.get("vote_maps") or []:
        name = names.get(str(item).strip().lower()) if isinstance(item, str) else None
        if name and name not in ranked:
            ranked.append(name)
    st["vote_maps"] = ranked
    off = st.get("vote_click_offset")
    if (isinstance(off, list) and len(off) == 2
            and all(_fits(0.0, v) for v in off)):
        st["vote_click_offset"] = [int(round(v)) for v in off]
    else:
        st["vote_click_offset"] = list(DEFAULT_SETTINGS["vote_click_offset"])
    return st


def _set_aside(path: str) -> None:
    """Rename a damaged settings.json to settings.corrupt-<n>.json, the first
    n that is free. It is kept, never deleted, so nothing is lost for good."""
    folder = os.path.dirname(path)
    for n in range(1, 1000):
        target = os.path.join(folder, f"settings.corrupt-{n}.json")
        if not os.path.exists(target):
            try:
                os.replace(path, target)
            except OSError:
                pass
            return


def load_settings(path: str | None = None) -> dict:
    """settings.json laid over DEFAULT_SETTINGS. Always a complete dict.

    No file: the defaults (nothing is written). A file that isn't JSON or
    isn't an object is renamed to settings.corrupt-<n>.json and the defaults
    are used.
    """
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:     # -sig: Notepad's BOM
            data = json.load(fh)
    except FileNotFoundError:
        return _fix_lists(copy.deepcopy(DEFAULT_SETTINGS))
    except (ValueError, UnicodeDecodeError):              # JSONDecodeError is a ValueError
        _set_aside(path)
        return _fix_lists(copy.deepcopy(DEFAULT_SETTINGS))
    except OSError:
        return _fix_lists(copy.deepcopy(DEFAULT_SETTINGS))
    if not isinstance(data, dict):
        _set_aside(path)
        return _fix_lists(copy.deepcopy(DEFAULT_SETTINGS))
    return _fix_lists(_merge(DEFAULT_SETTINGS, _migrate(data)))


def _migrate(data: dict) -> dict:
    """An older settings file brought up to date (see SETTINGS_VERSION)."""
    if "settings_version" not in data:
        data = {k: v for k, v in data.items() if k != "check_updates"}
        data["settings_version"] = SETTINGS_VERSION
    return data


def save_settings(d: dict, path: str | None = None) -> bool:
    """Write the whole settings dict atomically. False if it couldn't be saved."""
    path = path or settings_path()
    if not isinstance(d, dict):
        return False
    return atomic_write_json(path, d, indent=2)
