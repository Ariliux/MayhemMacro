"""Lifetime stats: stats.json, totals across every run.

Written whole (tmp, fsync, swap), with the previous copy kept as
stats.json.bak; a damaged file is set aside, never deleted. Time is added a
clamped step at a time by the app, so a clock that jumps can't add hours.
The file lives in paths.data_dir(); nothing is read or written at import.
"""
import json
import os
import re
import time

from mayhem import paths

STATS_KEEP_DAYS = 60
STATS_COUNTERS = ("runs", "errors", "rejoins", "rejoin_fails", "anti_afk",
                  "votes", "rares", "storage_full", "unclean_ends")


def new_stats() -> dict:
    st = {"v": 1, "total_secs": 0, "runs": 0, "longest_secs": 0,
          "longest_on": ""}
    for k in STATS_COUNTERS:
        st.setdefault(k, 0)
    st.update({"first_run": "", "last_run": "", "days": {}, "current": None})
    return st


def _num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and 0 <= v < 1e12)


def _clean_stats(data: dict) -> dict:
    """A loaded file in the v1 shape; anything missing or the wrong type is
    the default. Counters not in STATS_COUNTERS are dropped."""
    st = new_stats()
    for k in ("total_secs", "longest_secs") + STATS_COUNTERS:
        if _num(data.get(k)):
            st[k] = int(data[k]) if k in STATS_COUNTERS else data[k]
    for k in ("longest_on", "first_run", "last_run"):
        if isinstance(data.get(k), str):
            st[k] = data[k]
    days = data.get("days")
    if isinstance(days, dict):
        st["days"] = {d: v for d, v in days.items()
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)) and _num(v)}
    cur = data.get("current")
    if cur is not None:
        cur = cur if isinstance(cur, dict) else {}
        st["current"] = {"start": str(cur.get("start") or ""),
                         "secs": cur["secs"] if _num(cur.get("secs")) else 0,
                         "flushed_at": cur["flushed_at"]
                         if _num(cur.get("flushed_at")) else 0}
    return st


def save_stats(data: dict, path: str | None = None) -> bool:
    """Keep the newest 60 days, then write atomically with a .bak of the old
    file. False if it couldn't be saved."""
    path = path or paths.stats_path()
    try:
        days = data.get("days")
        if isinstance(days, dict) and len(days) > STATS_KEEP_DAYS:
            for k in sorted(days)[:-STATS_KEEP_DAYS]:
                del days[k]
    except Exception:
        return False
    return paths.atomic_write_json(path, data, indent=1, backup=True)


def load_stats(path: str | None = None) -> dict:
    """stats.json, else stats.json.bak, else a fresh start. A damaged
    stats.json is renamed to stats.json.corrupt-<time> so it can't overwrite
    the good .bak. A run that never ended cleanly (crash, power cut) still
    counts toward the longest run, and as one unclean end."""
    path = path or paths.stats_path()
    data, corrupt = None, False
    for p in (path, path + ".bak"):
        try:
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("not a stats file")
            data = raw
            break
        except FileNotFoundError:
            continue
        except Exception:
            if p == path:
                corrupt = True
    if corrupt:
        try:
            os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        except OSError:
            pass
    st = _clean_stats(data) if data is not None else new_stats()
    cur = st["current"]
    if cur is not None:
        if cur["secs"] > st["longest_secs"]:
            st["longest_secs"] = cur["secs"]
            st["longest_on"] = cur["start"][:10]
        st["unclean_ends"] += 1
        st["current"] = None
        save_stats(st, path)
    return st


def stats_add_time(st: dict, start: float, end: float) -> None:
    """Add [start, end) to the total and to each local day it covers, split
    at midnight."""
    if not end > start:
        return
    st["total_secs"] = st.get("total_secs", 0) + (end - start)
    days = st.setdefault("days", {})
    t = start
    while t < end:
        lt = time.localtime(t)
        nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                           0, 0, 0, 0, 0, -1))
        stop = min(end, nxt) if nxt > t else end
        key = time.strftime("%Y-%m-%d", lt)
        days[key] = days.get(key, 0) + (stop - t)
        t = stop


def stats_bump(st: dict, key: str, n: int = 1) -> None:
    """Add n to one counter. Only real counters: a typo raises KeyError here,
    not later as a silent failure in the middle of a run."""
    if key not in STATS_COUNTERS:
        raise KeyError(f"not a stats counter: {key}")
    st[key] = int(st.get(key, 0)) + max(0, int(n))


def today_secs(st: dict, now: float | None = None) -> float:
    """Seconds saved for today (local time)."""
    key = time.strftime("%Y-%m-%d", time.localtime(now))
    days = st.get("days")
    return float(days.get(key, 0)) if isinstance(days, dict) else 0.0


def reset_stats(path: str | None = None) -> tuple[dict, str]:
    """Start the stats over. The old files are renamed to
    stats.json.reset-<time> (and .bak), never deleted. Returns the fresh
    stats (already saved) and the name the old numbers were kept under.
    Raises OSError if the old file couldn't be moved out of the way."""
    path = path or paths.stats_path()
    kept = f"{path}.reset-{time.strftime('%Y%m%d-%H%M%S')}"
    if os.path.exists(path):
        os.replace(path, kept)
    if os.path.exists(path + ".bak"):
        os.replace(path + ".bak", kept + ".bak")
    st = new_stats()
    save_stats(st, path)
    return st, os.path.basename(kept)
