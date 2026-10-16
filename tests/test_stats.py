"""stats.py: the lifetime stats file, all inside temp folders.

Ported from GlassMacro's test_webhook.py stats checks, with the MayhemMacro
counters and the paths.py data folder.
"""
import glob
import json
import os
import time

import pytest

from mayhem import paths, safety, stats


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setenv("MAYHEM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(paths, "RETRY_WAIT", 0)
    return paths.stats_path()


def read(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def test_counters_are_the_design():
    assert stats.STATS_COUNTERS == ("runs", "errors", "rejoins", "rejoin_fails",
                                    "anti_afk", "votes", "rares", "storage_full",
                                    "unclean_ends")
    st = stats.new_stats()
    assert all(st[k] == 0 for k in stats.STATS_COUNTERS)
    assert st["v"] == 1 and st["days"] == {} and st["current"] is None
    assert stats.STATS_KEEP_DAYS == 60


def test_default_path_is_in_the_data_folder(path):
    assert path == os.path.join(os.environ["MAYHEM_DATA_DIR"], "stats.json")
    st = stats.new_stats()
    st["rares"] = 3
    assert stats.save_stats(st)                  # no path: paths.stats_path()
    assert read(path)["rares"] == 3
    assert stats.load_stats()["rares"] == 3


def test_real_folder_is_blocked_while_testing(monkeypatch):
    monkeypatch.delenv("MAYHEM_DATA_DIR", raising=False)
    with pytest.raises(safety.SideEffectBlocked):
        stats.load_stats()
    with pytest.raises(safety.SideEffectBlocked):
        stats.save_stats(stats.new_stats())
    with pytest.raises(safety.SideEffectBlocked):
        stats.reset_stats()


def test_save_whole_with_a_backup(path):
    a = stats.new_stats()
    a["runs"] = 1
    assert stats.save_stats(a, path) and read(path)["runs"] == 1
    assert not os.path.exists(path + ".tmp")
    a["runs"] = 2
    stats.save_stats(a, path)
    assert read(path + ".bak")["runs"] == 1, "the previous file is kept as .bak"
    assert read(path)["runs"] == 2


def test_damaged_file_loads_the_backup(path):
    a = stats.new_stats()
    a["runs"] = 1
    stats.save_stats(a, path)
    a["runs"] = 2
    stats.save_stats(a, path)
    with open(path, "w") as fh:
        fh.write('{"runs": 5, "tot')                      # torn write
    b = stats.load_stats(path)
    assert b["runs"] == 1
    assert not os.path.exists(path)
    assert len(glob.glob(path + ".corrupt-*")) == 1, "renamed, not deleted"
    stats.save_stats(b, path)
    assert read(path + ".bak")["runs"] == 1, "the good .bak is not overwritten"


def test_not_a_stats_file_and_no_backup(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("[1, 2]")
    assert stats.load_stats(path) == stats.new_stats()


def test_no_file_at_all(path):
    assert stats.load_stats(path) == stats.new_stats()
    assert not os.path.exists(path), "nothing written"


def test_unclean_end_counts_once(path):
    c = stats.new_stats()
    c.update(longest_secs=100, longest_on="2026-01-01",
             current={"start": "2026-10-01 10:00", "secs": 5000, "flushed_at": 1})
    stats.save_stats(c, path)
    r = stats.load_stats(path)
    assert r["longest_secs"] == 5000 and r["longest_on"] == "2026-10-01"
    assert r["unclean_ends"] == 1 and r["current"] is None
    assert stats.load_stats(path)["unclean_ends"] == 1, "the recovery is saved"


def test_wrong_types_fall_back_to_defaults():
    r = stats._clean_stats({"runs": "lots", "days": {
        "2026-10-01": 5, "junk": 3, "2026-10-02": "x"}, "total_secs": -4,
        "longest_on": 7, "current": "?", "rares": True, "votes": 4.0,
        "loadouts": 9})
    assert r["runs"] == 0 and r["days"] == {"2026-10-01": 5}
    assert r["total_secs"] == 0 and r["longest_on"] == ""
    assert r["current"]["secs"] == 0
    assert r["rares"] == 0, "a bool is not a count"
    assert r["votes"] == 4 and type(r["votes"]) is int
    assert "loadouts" not in r, "GlassMacro counters are dropped"


def test_only_60_days_are_kept(path):
    many = stats.new_stats()
    many["days"] = {f"2026-{m:02d}-{dd:02d}": 1 for m in (7, 8, 9)
                    for dd in range(1, 29)}
    stats.save_stats(many, path)
    kept = read(path)["days"]
    assert len(kept) == 60 and min(kept) == "2026-07-25" and "2026-09-28" in kept


def test_time_across_midnight_is_split():
    midnight = time.mktime((2026, 10, 6, 0, 0, 0, 0, 0, -1))
    m = stats.new_stats()
    stats.stats_add_time(m, midnight - 2, midnight + 3)
    assert m["days"] == {"2026-10-05": 2, "2026-10-06": 3}
    assert m["total_secs"] == 5
    stats.stats_add_time(m, midnight + 10, midnight + 10)     # nothing
    stats.stats_add_time(m, midnight + 10, midnight + 5)      # backwards: nothing
    assert m["total_secs"] == 5


def test_today_secs():
    noon = time.mktime((2026, 10, 8, 12, 0, 0, 0, 0, -1))
    m = stats.new_stats()
    stats.stats_add_time(m, noon, noon + 90)
    assert stats.today_secs(m, noon) == 90
    assert stats.today_secs(m, noon + 86400) == 0
    assert stats.today_secs({"days": "?"}) == 0


def test_bump():
    st = stats.new_stats()
    stats.stats_bump(st, "rares")
    stats.stats_bump(st, "votes", 3)
    stats.stats_bump(st, "anti_afk", -5)                     # never goes down
    assert st["rares"] == 1 and st["votes"] == 3 and st["anti_afk"] == 0
    with pytest.raises(KeyError):
        stats.stats_bump(st, "loadouts")


def test_reset_keeps_the_old_numbers(path):
    a = stats.new_stats()
    a["runs"] = 7
    stats.save_stats(a, path)
    stats.save_stats(a, path)                                # makes a .bak too
    fresh, kept = stats.reset_stats(path)
    assert fresh == stats.new_stats()
    assert read(path) == stats.new_stats()
    folder = os.path.dirname(path)
    assert kept.startswith("stats.json.reset-") and os.sep not in kept
    assert read(os.path.join(folder, kept))["runs"] == 7
    assert read(os.path.join(folder, kept + ".bak"))["runs"] == 7
    assert not os.path.exists(path + ".bak")


def test_save_failure_returns_false(path):
    st = stats.new_stats()
    st["bad"] = object()
    assert stats.save_stats(st, path) is False
    assert stats.save_stats("not a dict", path) is False
    assert not os.path.exists(path)
