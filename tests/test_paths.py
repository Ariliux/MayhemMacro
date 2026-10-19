"""paths.py: the data folder and settings.json, all inside temp folders.

The real %LOCALAPPDATA%\\MayhemMacro is never touched: conftest points
MAYHEM_DATA_DIR at a temp folder, and every test that writes uses its own
pytest tmp_path on top of that.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from mayhem import MAPS, paths, safety

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A fresh, empty data folder for one test."""
    d = tmp_path / "data"
    monkeypatch.setenv("MAYHEM_DATA_DIR", str(d))
    monkeypatch.setattr(paths, "RETRY_WAIT", 0)
    return d


def write(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding) as fh:
        fh.write(text)


# ---- the data folder ----------------------------------------------------------
def test_conftest_sandbox_is_in_use():
    d = paths.data_dir()
    assert safety.testing()
    assert d == os.environ["MAYHEM_DATA_DIR"]
    assert os.path.abspath(d).lower().startswith(
        os.path.abspath(tempfile.gettempdir()).lower())


def test_real_folder_is_blocked_while_testing(monkeypatch):
    monkeypatch.delenv("MAYHEM_DATA_DIR", raising=False)
    for fn in (paths.data_dir, paths.ensure_data_dir, paths.settings_path,
               paths.stats_path, paths.log_path, paths.events_dir,
               paths.load_settings):
        with pytest.raises(safety.SideEffectBlocked):
            fn()
    with pytest.raises(safety.SideEffectBlocked):
        paths.save_settings({"auto_vote": False})


def test_real_folder_name_outside_testing(monkeypatch, tmp_path):
    # only the name is worked out here - nothing is created
    monkeypatch.setenv("MAYHEM_TESTING", "0")
    monkeypatch.delenv("MAYHEM_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.data_dir() == os.path.join(str(tmp_path), "MayhemMacro")
    assert paths.settings_path() == os.path.join(str(tmp_path), "MayhemMacro", "settings.json")
    assert not os.path.exists(os.path.join(str(tmp_path), "MayhemMacro"))


def test_file_names(data):
    assert paths.settings_path() == os.path.join(str(data), "settings.json")
    assert paths.stats_path() == os.path.join(str(data), "stats.json")
    assert paths.log_path() == os.path.join(str(data), "log.txt")
    assert paths.events_dir() == os.path.join(str(data), "events")
    assert not data.exists(), "asking for a path never creates anything"


def test_ensure_data_dir_creates_it(data):
    assert not data.exists()
    assert paths.ensure_data_dir() == str(data)
    assert data.is_dir()
    assert paths.ensure_data_dir() == str(data)      # a second call is fine


def test_import_creates_nothing(tmp_path):
    """A fresh interpreter imports the services with MAYHEM_TESTING on and
    MAYHEM_DATA_DIR unset: nothing may raise or be created at import."""
    env = {k: v for k, v in os.environ.items() if k != "MAYHEM_DATA_DIR"}
    env.update(MAYHEM_TESTING="1", MAYHEM_NO_SEND="1", LOCALAPPDATA=str(tmp_path))
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); "
            "import mayhem.paths, mayhem.stats, mayhem.discord, mayhem.update; print('ok')")
    r = subprocess.run([sys.executable, "-I", "-c", code], env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert os.listdir(tmp_path) == []


# ---- the defaults -------------------------------------------------------------
# Written out here, not read from the docs: a default changing by accident
# fails this test, and the docs can be reworded freely.
EXPECTED_DEFAULTS = {
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
    "front_hourly": True,
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
    "roll_key": "F8",
    "roll_count": 10,
    "discord": {"enabled": False, "webhook": "", "user_id": "",
                "send": {"rare": True, "run": True, "rejoin": True, "storage": True,
                         "error": True, "stuck": True, "hourly": False},
                "mention": {"rare": True, "error": True, "stuck": True}},
    "check_updates": True,
    "ui": {"geometry": "", "page": "home", "sidebar_collapsed": False, "pin": False},
    "settings_version": 2,
}


def test_defaults_are_exactly_the_design():
    assert paths.DEFAULT_SETTINGS == EXPECTED_DEFAULTS
    assert paths.DEFAULT_SETTINGS["afk_bring_to_front"] is False
    assert paths.DEFAULT_SETTINGS["attach_picture"] is False
    assert paths.DEFAULT_SETTINGS["discord"]["enabled"] is False
    assert paths.DEFAULT_SETTINGS["check_updates"] is True
    assert paths.DEFAULT_SETTINGS["settings_version"] == paths.SETTINGS_VERSION


# ---- 1.0.0's settings file ---------------------------------------------------
def test_a_1_0_0_file_gets_update_checks_on(data):
    """1.0.0 wrote "check_updates": false into every settings.json, with no
    switch for it: that was only the old default, so it doesn't count."""
    write(paths.settings_path(), json.dumps({"check_updates": False, "auto_vote": False,
                                             "private_server_link": "x"}))
    st = paths.load_settings()
    assert st["check_updates"] is True
    assert st["settings_version"] == 2
    assert st["auto_vote"] is False and st["private_server_link"] == "x"   # the rest is kept


def test_a_choice_saved_by_this_version_is_kept(data):
    st = paths.load_settings()
    st["check_updates"] = False                    # turned off on About or Settings
    assert paths.save_settings(st)
    assert json.loads((data / "settings.json").read_text())["settings_version"] == 2
    assert paths.load_settings()["check_updates"] is False
    assert paths.load_settings()["check_updates"] is False      # and stays off


def test_no_file_gives_the_defaults_and_writes_nothing(data):
    st = paths.load_settings()
    assert st == paths.DEFAULT_SETTINGS
    assert not data.exists()


def test_loaded_settings_are_a_copy(data):
    st = paths.load_settings()
    st["discord"]["send"]["rare"] = False
    st["vote_maps"].append("Void")
    st["vote_click_offset"][0] = 99
    assert paths.DEFAULT_SETTINGS["discord"]["send"]["rare"] is True
    assert paths.DEFAULT_SETTINGS["vote_maps"] == []
    assert paths.DEFAULT_SETTINGS["vote_click_offset"] == [0, 0]
    assert paths.load_settings() == paths.DEFAULT_SETTINGS


# ---- deep merge ------------------------------------------------------------------
def test_saved_values_are_laid_over_the_defaults(data):
    write(paths.settings_path(), json.dumps({
        "auto_vote": False, "rare_min": 505000, "private_server_link": "x",
        "discord": {"enabled": True, "send": {"hourly": True}},
        "ui": {"page": "stats"}}))
    st = paths.load_settings()
    assert st["auto_vote"] is False and st["rare_min"] == 505000
    assert st["private_server_link"] == "x"
    assert st["discord"]["enabled"] is True
    assert st["discord"]["send"]["hourly"] is True
    assert st["discord"]["send"]["rare"] is True          # filled from the defaults
    assert st["discord"]["mention"] == {"rare": True, "error": True, "stuck": True}
    assert st["ui"] == {"geometry": "", "page": "stats", "sidebar_collapsed": False,
                        "pin": False}
    assert st["anti_afk_after_min"] == 10


def test_unknown_keys_are_kept(data):
    write(paths.settings_path(), json.dumps({
        "from_a_newer_version": {"a": [1, 2]},
        "discord": {"new_thing": "x", "send": {"new_alert": False}}}))
    st = paths.load_settings()
    assert st["from_a_newer_version"] == {"a": [1, 2]}
    assert st["discord"]["new_thing"] == "x"
    assert st["discord"]["send"]["new_alert"] is False


def test_wrong_types_get_the_default(data):
    write(paths.settings_path(), json.dumps({
        "auto_rejoin": "yes", "anti_afk": 1, "anti_afk_after_min": True,
        "rare_min": "100", "rare_ping_min": None, "private_server_link": 5,
        "discord": "nope", "ui": {"pin": 1, "page": 3, "geometry": "800x600"},
        "vote_maps": "Void", "keep_awake": None, "check_updates": [True]}))
    st = paths.load_settings()
    d = paths.DEFAULT_SETTINGS
    for k in ("auto_rejoin", "anti_afk", "anti_afk_after_min", "rare_min",
              "rare_ping_min", "private_server_link", "discord", "vote_maps",
              "keep_awake", "check_updates"):
        assert st[k] == d[k], k
    assert st["ui"] == {"geometry": "800x600", "page": "home",
                        "sidebar_collapsed": False, "pin": False}


def test_numbers(data):
    write(paths.settings_path(), json.dumps({"anti_afk_after_min": 7.0,
                                             "rare_min": 2.5}))
    st = paths.load_settings()
    assert st["anti_afk_after_min"] == 7 and type(st["anti_afk_after_min"]) is int
    assert st["rare_min"] == 100000                        # not a whole number


def test_vote_maps_are_cleaned(data):
    write(paths.settings_path(), json.dumps(
        {"vote_maps": ["void", "Void", "Moon", 3, None, " OCEAN ", "Glitch"]}))
    assert paths.load_settings()["vote_maps"] == ["Void", "Ocean", "Glitch"]
    assert all(m in MAPS for m in paths.load_settings()["vote_maps"])


@pytest.mark.parametrize("raw, want", [
    ([5, -3], [5, -3]), ([1.6, 2], [2, 2]), ([1], [0, 0]), ([True, 0], [0, 0]),
    ("x", [0, 0]), ([1, 2, 3], [0, 0]), (["1", 2], [0, 0])])
def test_vote_click_offset(data, raw, want):
    write(paths.settings_path(), json.dumps({"vote_click_offset": raw}))
    assert paths.load_settings()["vote_click_offset"] == want


@pytest.mark.parametrize("raw, want", [
    (25, 25), (1, 1), (100, 100), (30, 30), (1000, 1000), (25.0, 25),
    ("all", "all"), ("All", "all"), (" ALL ", "all"),
    (0, 10), (-5, 10), (1001, 10), (2.5, 10), (True, 10), ("10", 10), ("every", 10),
    (None, 10), ([5], 10)])
def test_roll_count_is_a_count_or_all(data, raw, want):
    write(paths.settings_path(), json.dumps({"roll_count": raw}))
    got = paths.load_settings()["roll_count"]
    assert got == want and type(got) is type(want)


def test_roll_key_is_kept_as_saved(data):
    write(paths.settings_path(), json.dumps({"roll_key": "Ctrl+F9"}))
    assert paths.load_settings()["roll_key"] == "Ctrl+F9"
    write(paths.settings_path(), json.dumps({"roll_key": 8}))
    assert paths.load_settings()["roll_key"] == "F8"
    # the word 'all' is only allowed for roll_count
    write(paths.settings_path(), json.dumps({"rare_min": "all"}))
    assert paths.load_settings()["rare_min"] == 100000


def test_notepad_bom_is_fine(data):
    write(paths.settings_path(), json.dumps({"auto_vote": False}), encoding="utf-8-sig")
    assert paths.load_settings()["auto_vote"] is False


# ---- a damaged file ----------------------------------------------------------------
@pytest.mark.parametrize("raw", ['{"auto_vote": fal', "[1, 2]", '"text"', ""])
def test_corrupt_file_is_set_aside(data, raw):
    write(paths.settings_path(), raw)
    assert paths.load_settings() == paths.DEFAULT_SETTINGS
    assert not os.path.exists(paths.settings_path())
    kept = data / "settings.corrupt-1.json"
    assert kept.read_text(encoding="utf-8") == raw          # kept, not deleted


def test_bad_bytes_are_corrupt_too(data):
    os.makedirs(data)
    (data / "settings.json").write_bytes(b"\xff\xfe{\x00\x81")
    assert paths.load_settings() == paths.DEFAULT_SETTINGS
    assert (data / "settings.corrupt-1.json").exists()


def test_each_corrupt_file_gets_its_own_number(data):
    write(paths.settings_path(), "{broken one")
    paths.load_settings()
    write(paths.settings_path(), "{broken two")
    paths.load_settings()
    assert (data / "settings.corrupt-1.json").read_text() == "{broken one"
    assert (data / "settings.corrupt-2.json").read_text() == "{broken two"


# ---- saving ---------------------------------------------------------------------------
def test_save_round_trip(data):
    st = paths.load_settings()
    st["vote_maps"] = ["Dreamland", "Void"]
    st["discord"]["user_id"] = "876543210987654321"
    assert paths.save_settings(st) is True
    assert paths.load_settings() == st
    assert sorted(os.listdir(data)) == ["settings.json"]      # no .tmp left


def test_save_is_flushed_to_disk(data, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(paths.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    assert paths.save_settings({"a": 1})
    assert synced


def test_save_retries_a_busy_file(data, monkeypatch):
    paths.save_settings({"n": 1})
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("file in use")
        return real(src, dst)

    monkeypatch.setattr(paths.os, "replace", flaky)
    assert paths.save_settings({"n": 2}) is True
    assert len(calls) == 3
    assert json.loads((data / "settings.json").read_text())["n"] == 2


def test_save_gives_up_cleanly(data, monkeypatch):
    paths.save_settings({"n": 1})

    def busy(src, dst):
        raise PermissionError("file in use")

    monkeypatch.setattr(paths.os, "replace", busy)
    assert paths.save_settings({"n": 2}) is False
    assert json.loads((data / "settings.json").read_text())["n"] == 1   # old file intact
    assert sorted(os.listdir(data)) == ["settings.json"]                # no .tmp left


def test_save_never_half_writes(data):
    paths.save_settings({"n": 1})
    assert paths.save_settings({"n": 2, "bad": object()}) is False   # can't be JSON
    assert json.loads((data / "settings.json").read_text()) == {"n": 1}
    assert sorted(os.listdir(data)) == ["settings.json"]
    assert paths.save_settings(["not", "a", "dict"]) is False


def test_save_creates_the_folder(data):
    assert not data.exists()
    assert paths.save_settings(paths.load_settings())
    assert (data / "settings.json").exists()
