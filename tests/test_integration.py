"""The app and the engine wired together for real - without starting a run.

On Start the app builds the real engine (mayhemmacro.default_engine) from the
real window, OCR, inputs, rejoin, Roblox log reader, input clock and picture
saver. These tests build exactly that object and check the seams between the
builders' parts: every name the engine calls on a dependency exists on the
real one, every event it emits means something to the app, every setting
either side reads is a real setting, and every real side effect sits behind
safety.guard(). Nothing here starts the worker thread, reads the screen or
the Roblox logs, sends input, opens Roblox or touches the network.
"""
import ast
import asyncio
import copy
import dataclasses
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import time
import types

import pytest

import mayhemmacro as M
from mayhem import discord as D
from mayhem import engine as engine_mod
from mayhem import inputs, paths, rejoin, safety, window
from mayhem import stats as S
from mayhem.ocr import Ocr
from mayhem.robloxlog import LogTail

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MISSING = object()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


ENGINE_SRC = _read(engine_mod.__file__)
APP_SRC = _read(M.__file__)


# ------------------------------------------------- the real engine, built ---
def _only_the_engine_guard(asked: list):
    """mayhemmacro's view of safety with ONE guard lifted: 'start the real
    engine'. Every other guard still raises, so nothing real can happen."""
    def guard(what: str) -> None:
        asked.append(what)
        if what != "start the real engine":
            safety.guard(what)
    return types.SimpleNamespace(guard=guard, testing=safety.testing,
                                 SideEffectBlocked=safety.SideEffectBlocked)


@pytest.fixture
def real_engine(monkeypatch):
    """The app's own factory, building the real Engine with real Deps. The
    shared input clock is put back the way it was afterwards."""
    asked: list[str] = []
    monkeypatch.setattr(M, "safety", _only_the_engine_guard(asked))
    monkeypatch.setattr(M, "_real_engine", [])
    clock_before = inputs.__dict__.pop("clock", _MISSING)
    got: list[dict] = []
    settings = copy.deepcopy(paths.DEFAULT_SETTINGS)
    eng = M.default_engine(lambda: settings, got.append)
    try:
        yield eng, got, asked
    finally:
        M.release_engine()
        if clock_before is _MISSING:
            inputs.__dict__.pop("clock", None)
        else:
            inputs.__dict__["clock"] = clock_before


def test_the_app_builds_the_real_engine_from_the_real_parts(real_engine):
    eng, got, asked = real_engine
    assert isinstance(eng, engine_mod.Engine)
    assert asked == ["start the real engine"]
    d = eng.deps
    assert d.window is window and d.inputs is inputs and d.rejoin is rejoin
    assert isinstance(d.ocr, Ocr) and isinstance(d.tail, LogTail)
    assert isinstance(d.clock, window.InputClock) and d.clock is inputs.clock
    assert d.save_picture is engine_mod.save_picture
    assert d.now is time.monotonic and d.sleep is time.sleep
    # built, not started: no thread, no log read, OCR not even loaded
    assert not eng.running and eng.state == "stopped" and eng._thread is None
    assert d.tail.logdir is None and d.tail.path is None and not d.tail._primed
    assert d.ocr._tried is False and d.ocr._loop is None
    assert got == []


def test_the_next_start_gets_the_same_engine_with_the_new_runs_listener(real_engine):
    eng, got, asked = real_engine
    later: list[dict] = []
    new_settings = {"auto_vote": False}

    def emit(ev):
        later.append(ev)
    again = M.default_engine(lambda: new_settings, emit)
    assert again is eng and M._real_engine == [eng]
    assert eng.get_settings() is new_settings and eng._emit_to is emit
    assert asked == ["start the real engine"] * 2


def test_closing_the_app_frees_the_engines_ocr(real_engine):
    eng, _, _ = real_engine
    loop = eng.deps.ocr._loop = asyncio.new_event_loop()   # as if it had read once
    M.release_engine()
    assert loop.is_closed() and eng.deps.ocr._loop is None


def test_release_engine_never_raises(monkeypatch):
    class Broken:
        def close(self):
            raise RuntimeError("no")
    monkeypatch.setattr(M, "_real_engine", [types.SimpleNamespace(deps=None),
                                            types.SimpleNamespace(
                                                deps=types.SimpleNamespace(ocr=Broken()))])
    M.release_engine()
    monkeypatch.setattr(M, "_real_engine", [])
    M.release_engine()


def test_every_real_side_effect_the_engine_can_reach_is_blocked_while_testing(real_engine):
    """The real deps the engine holds, called the way the engine calls them:
    each refuses at once (its guard comes first)."""
    eng, _, _ = real_engine
    d = eng.deps
    calls = [lambda: d.inputs.press_space(), lambda: d.inputs.click_at(10, 10),
             lambda: d.inputs.move_to(10, 10), lambda: d.window.bring_to_front(0),
             lambda: d.window.restore_front(0), lambda: d.rejoin.kill_roblox(),
             lambda: d.rejoin.launch(rejoin.PUBLIC_LINK)]
    for call in calls:
        with pytest.raises(safety.SideEffectBlocked):
            call()


# ------------------------------------------- names the engine uses on deps ---
DEP_FIELDS = {f.name for f in dataclasses.fields(engine_mod.Deps)}


def _is_self_deps(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "deps"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _dep_uses(src: str) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """What engine.py reads off each dependency: self.deps.<dep>.<name>, an
    alias (w = self.deps.window; w.<name>) and getattr/hasattr(<dep>, '<name>').
    Returns (names used, names called, Deps fields used)."""
    used: dict[str, set[str]] = {}
    called: dict[str, set[str]] = {}
    fields: set[str] = set()

    def dep_of(node, aliases) -> str | None:
        if isinstance(node, ast.Attribute) and _is_self_deps(node.value):
            return node.attr
        if isinstance(node, ast.Name):
            return aliases.get(node.id)
        return None

    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        aliases: dict[str, str] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                dep = dep_of(node.value, {})
                if dep:
                    aliases[node.targets[0].id] = dep
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                if _is_self_deps(node.value):
                    fields.add(node.attr)
                dep = dep_of(node.value, aliases)
                if dep:
                    used.setdefault(dep, set()).add(node.attr)
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute):
                    dep = dep_of(f.value, aliases)
                    if dep:
                        called.setdefault(dep, set()).add(f.attr)
                elif (isinstance(f, ast.Name) and f.id in ("getattr", "hasattr")
                        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                    dep = dep_of(node.args[0], aliases)
                    if dep:
                        used.setdefault(dep, set()).add(node.args[1].value)
    return used, called, fields


def _has(obj, name: str) -> bool:
    """hasattr without running a property (Ocr.available would load OCR)."""
    if isinstance(obj, types.ModuleType):
        return hasattr(obj, name)
    return name in vars(obj) or hasattr(type(obj), name)


def _member(obj, name: str):
    return getattr(obj if isinstance(obj, types.ModuleType) else type(obj), name)


def test_every_name_the_engine_uses_on_a_dependency_exists_on_the_real_one(real_engine):
    eng, _, _ = real_engine
    used, called, fields = _dep_uses(ENGINE_SRC)
    assert fields <= DEP_FIELDS
    # the scan really found the engine's calls (a broken scan would pass anything)
    assert {"roblox_hwnd", "is_foreground", "is_minimized", "client_rect", "grab",
            "bring_to_front", "restore_front"} <= used["window"]
    assert {"press_space", "click_at"} <= used["inputs"]
    assert {"deep_links", "kill_roblox", "launch", "validate_link"} <= used["rejoin"]
    assert {"prime", "poll", "in_bmx", "path", "place", "entered", "loaded",
            "disconnected"} <= used["tail"]
    assert {"user_idle_seconds"} <= used["clock"]
    assert {"read", "available", "error"} <= used["ocr"]
    for dep, names in used.items():
        real = getattr(eng.deps, dep)
        missing = sorted(n for n in names if not _has(real, n))
        assert not missing, f"engine uses {dep}.{missing}, the real {dep} has no such thing"
    for dep, names in called.items():
        real = getattr(eng.deps, dep)
        for n in names:
            assert callable(_member(real, n)), f"engine calls {dep}.{n}(), not callable"


def test_hud_and_vote_get_what_the_engine_hands_them():
    """The engine passes the OCR object into hud and vote: they only call
    read() on it, and the engine passes find_options the client height."""
    import inspect
    from mayhem import hud, vote
    assert list(inspect.signature(hud.read_top).parameters) == ["ocr", "client"]
    assert list(inspect.signature(hud.read_banner).parameters) == ["ocr", "client"]
    assert "client_h" in inspect.signature(vote.find_options).parameters
    assert "vote.find_options(lines, img.height, img.width)" in ENGINE_SRC
    for mod in (hud, vote):
        src = _read(mod.__file__)
        assert set(re.findall(r"\bocr\.(\w+)", src)) <= {"read"}


# ---------------------------------------------- events: engine -> the app ---
def _emitted_dicts(src: str) -> list[dict]:
    """Every dict literal engine.py passes to self._emit(...), with its
    constant values (anything computed is None)."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_emit" and node.args
                and isinstance(node.args[0], ast.Dict)):
            d = {}
            for k, v in zip(node.args[0].keys, node.args[0].values):
                if isinstance(k, ast.Constant):
                    d[k.value] = v.value if isinstance(v, ast.Constant) else None
            out.append(d)
    return out


def _engine_states(src: str) -> set[str]:
    """Every state the engine can report: _set_state('x', ...), the
    `state, why = 'x', ...` pairs, and 'stopped'."""
    states = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_set_state" and node.args
                and isinstance(node.args[0], ast.Constant)):
            states.add(node.args[0].value)
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple)
                and [getattr(t, "id", "") for t in node.targets[0].elts] == ["state", "why"]
                and isinstance(node.value, ast.Tuple)
                and isinstance(node.value.elts[0], ast.Constant)):
            states.add(node.value.elts[0].value)
    for d in _emitted_dicts(src):
        if d.get("type") == "state" and d.get("state"):
            states.add(d["state"])
    return states


SAMPLES = {
    "state": {"type": "state", "state": "running", "why": "In Block Mayhem X"},
    "hud": {"type": "hud", "timer": 511, "storage": [215, 350], "world": "Arctic"},
    "rare": {"type": "rare", "block": "Resonance", "denominator": 100000, "variant": "normal",
             "source": "banner", "ping": False, "picture": None, "world": "Arctic"},
    "vote": {"type": "vote", "options": ["Overworld", "Arctic"], "chose": "Arctic", "ok": True},
    "anti_afk": {"type": "anti_afk", "idle": 612.0, "how": "jump"},
    "rejoin": {"type": "rejoin", "phase": "ok", "try": 1, "why": "Roblox closed"},
    "storage_full": {"type": "storage_full", "n": 350, "m": 350},
    "collected": {"type": "collected", "block": "Sporedelic", "variant": "normal",
                  "denominator": 100000, "how": "chat"},
    "log": {"type": "log", "level": "warn", "text": "Roblox is behind other windows."},
}


def _plan(ev: dict, **settings):
    st = copy.deepcopy(paths.DEFAULT_SETTINGS)
    st.update(settings)
    return M.plan_event(ev, st, M.RunCtx(live=True, run_no=1))


def test_the_engine_emits_exactly_the_designed_event_types():
    types_ = {d.get("type") for d in _emitted_dicts(ENGINE_SRC)}
    assert types_ == set(SAMPLES)


def test_every_event_type_the_engine_emits_shows_up_in_the_app():
    for kind, ev in SAMPLES.items():
        p = _plan(ev)
        shown = p.feed or p.state or p.hud or p.log or p.mode
        assert shown, f"the app ignores the engine's '{kind}' event"


def test_every_key_the_engine_puts_in_an_event_is_one_the_app_knows():
    """The app reads these keys per type; the engine must send nothing else
    the app would silently drop (extra keys are a sign the names drifted)."""
    reads = {"state": {"state", "why"}, "hud": {"timer", "storage", "world"},
             "rare": {"block", "denominator", "variant", "source", "ping", "picture", "world"},
             "vote": {"options", "chose", "ok"}, "anti_afk": {"idle", "how"},
             "rejoin": {"phase", "try", "why", "wait"}, "storage_full": {"n", "m"},
             "collected": {"block", "variant", "denominator", "how"},
             "log": {"level", "text"}}
    for d in _emitted_dicts(ENGINE_SRC):
        kind = d["type"]
        extra = set(d) - {"type"} - reads[kind]
        assert not extra, f"engine '{kind}' event has keys the app never reads: {extra}"
        for key in reads[kind]:
            assert re.search(rf"ev(?:\.get\(|\[)[\"']{key}[\"']", APP_SRC), \
                f"the app never reads '{key}'"


def test_every_state_the_engine_reports_has_a_look_in_the_app():
    states = _engine_states(ENGINE_SRC)
    assert {"running", "paused", "waiting", "rejoining", "stopped"} == states
    for state in states:
        p = _plan({"type": "state", "state": state, "why": "x"})
        assert p.mode == state
        if state != "stopped":
            assert p.state and p.state[2] in ("GREEN", "AMBER", "ACCENT")


def test_every_rejoin_phase_and_log_level_is_handled():
    phases = {d.get("phase") for d in _emitted_dicts(ENGINE_SRC) if d.get("type") == "rejoin"}
    assert phases == {"start", "ok", "fail", "gave_up"}
    for phase in phases:
        p = _plan({"type": "rejoin", "phase": phase, "try": 2, "why": "x", "wait": 60.0})
        assert p.feed, phase
    assert _plan({"type": "rejoin", "phase": "gave_up", "try": 5, "why": "x"}).end[0] == "stuck"
    levels = set(re.findall(r'self\._log\("(\w+)"', ENGINE_SRC))
    assert levels == {"info", "warn", "error"}
    for level in levels:
        assert _plan({"type": "log", "level": level, "text": "hello"}).log == [(level, "hello")]


def test_every_stat_the_app_counts_is_a_real_stats_counter():
    """Session swallows a KeyError from stats_bump, so a typo would just
    never count. Check the names here instead."""
    events = list(SAMPLES.values()) + [
        {"type": "rejoin", "phase": p, "try": 1, "why": "x", "wait": 30.0}
        for p in ("start", "fail", "gave_up")] + [
        {"type": "run", "kind": k, "reason": r, "ran": 60.0}
        for k in ("start", "stop", "closed", "hourly") for r in ("stopped", "error", "stuck")]
    seen = set()
    for ev in events:
        seen |= set(_plan(ev).stats)
    assert seen <= set(S.STATS_COUNTERS)
    assert {"rares", "votes", "anti_afk", "rejoins", "rejoin_fails", "storage_full",
            "errors"} <= seen


# ----------------------------------------------------------- settings keys ---
APP_ONLY_UI = {"motion", "remember", "zoomed"}     # the app's own window choices


def test_every_setting_the_engine_reads_is_a_real_setting():
    keys = set(re.findall(r'_(?:flag|int)\(st, "(\w+)"', ENGINE_SRC))
    keys |= set(re.findall(r'\bst\.get\("(\w+)"', ENGINE_SRC))
    assert {"auto_rejoin", "anti_afk", "anti_afk_after_min", "auto_vote", "vote_maps",
            "vote_click_offset", "rare_min", "rare_ping_min", "alert_variants_always",
            "afk_bring_to_front", "private_server_link"} <= keys
    assert keys <= set(paths.DEFAULT_SETTINGS)


def test_every_setting_the_app_reads_is_a_real_setting():
    keys = set(re.findall(r'\b(?:s|settings|self\.settings)(?:\.get\(|\[|\.setdefault\()"(\w+)"',
                          APP_SRC))
    keys |= set(re.findall(r'_(?:switch_saved|rarity_picked)\("(\w+)"', APP_SRC))
    assert {"rare_alerts", "storage_alert", "attach_picture", "keep_awake",
            "private_server_link", "discord", "ui"} <= keys
    assert keys <= set(paths.DEFAULT_SETTINGS), keys - set(paths.DEFAULT_SETTINGS)


def test_the_discord_page_matches_the_discord_settings():
    defaults = paths.DEFAULT_SETTINGS["discord"]
    keys = set(re.findall(r'\bcfg(?:\.get\(|\[|\.setdefault\()"(\w+)"', APP_SRC))
    assert keys and keys <= set(defaults)
    groups = [g for g, _, _ in M.MayhemMacro.HOOK_EVENTS]
    assert set(groups) == set(defaults["send"]) == set(D.GROUPS)
    assert {g for g, _, at in M.MayhemMacro.HOOK_EVENTS if at} == set(defaults["mention"])
    assert D.webhook_defaults() == defaults


def test_the_apps_own_window_settings_live_under_ui_and_survive_a_reload(tmp_path):
    used = set(re.findall(r'_ui_(?:set|switched)\("(\w+)"', APP_SRC))
    used |= set(re.findall(r'\bui(?:\.get\(|\[)"(\w+)"', APP_SRC))
    known = set(paths.DEFAULT_SETTINGS["ui"])
    assert used - known <= APP_ONLY_UI, used - known - APP_ONLY_UI
    # not in DEFAULT_SETTINGS (DESIGN.md's list), but kept by load_settings
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ui": {"motion": False, "remember": False, "zoomed": True}}),
                    encoding="utf-8")
    ui = paths.load_settings(str(path))["ui"]
    assert ui["motion"] is False and ui["remember"] is False and ui["zoomed"] is True
    assert set(paths.DEFAULT_SETTINGS["ui"]) <= set(ui)


# ------------------------------------------------- one engine, many runs ---
def test_stop_and_start_reuse_one_engine_and_keep_the_hourly_rejoin_cap(monkeypatch):
    """F8 twice must not reset the 6-rejoins-an-hour cap, and a new run's
    events go only to the new run."""
    TE = pytest.importorskip("test_engine")
    r = TE.Rig()
    monkeypatch.setattr(M, "safety", _only_the_engine_guard([]))
    monkeypatch.setattr(M, "_real_engine", [r.engine])
    first: list[dict] = []
    second: list[dict] = []
    eng = M.default_engine(lambda: r.settings, first.append)
    assert eng is r.engine
    eng.start(thread=False)
    for n in range(6):
        r.tail.feed(TE.DC(277))
        r.tick()
        TE._launched_and_rejoined(r, n)
    assert len(r.rejoin.launches()) == 6
    eng.stop()
    assert first[-1] == {"type": "state", "state": "stopped", "why": "Stopped"}
    n_first = len(first)

    assert M.default_engine(lambda: r.settings, second.append) is eng
    eng.start(thread=False)
    r.tail.feed(TE.DC(277))
    r.run(60)
    assert len(r.rejoin.launches()) == 6                 # still capped
    assert len(first) == n_first                          # nothing reached the old run
    assert second[0] == {"type": "state", "state": "running", "why": "Starting"}
    assert any("Rejoined 6 times in the last hour" in e.get("text", "") for e in second)
    eng.stop()


def test_the_app_does_not_repeat_the_engines_bad_link_warning():
    """The engine warns about a link that can't work on its first tick; the
    app's Start used to say it too."""
    start = APP_SRC.index("    def toggle_run(self):")
    end = APP_SRC.index("    def _stop_run(self")
    assert "validate_link" not in APP_SRC[start:end]
    r = pytest.importorskip("test_engine").Rig(
        private_server_link="https://example.com/not-a-roblox-link").start()
    r.tick()
    assert any("public server" in t for t in r.logs("warn"))


# ------------------------------------------------ guards before side effects ---
# File -> the real side-effect calls that must only run after safety.guard().
SIDE_EFFECTS = {
    "mayhem/inputs.py": {"SendInput"},
    "mayhem/rejoin.py": {"TerminateProcess", "startfile"},
    "mayhem/window.py": {"SetForegroundWindow", "keybd_event", "ShowWindow",
                         "BringWindowToTop", "AttachThreadInput"},
    "mayhem/discord.py": {"urlopen"},
    # the request to GitHub, starting the new version, reopening the app and
    # swapping its files (copytree into .new, the renames that switch over)
    "mayhem/update.py": {"urlopen", "popen", "startfile", "copytree", "rename"},
    "mayhem/engine.py": {"startfile", "SendInput", "TerminateProcess", "urlopen"},
    # the real mouse and screen for rolling workers (clicks still guard in inputs.py)
    "mayhem/roller.py": {"click_at", "grab", "Ocr"},
    "mayhemmacro.py": {"startfile", "SetForegroundWindow", "ShowWindow",
                       "SetThreadExecutionState", "add_hotkey", "on_release_key"},
}


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _functions(tree: ast.AST) -> dict[str, list[ast.FunctionDef]]:
    """Top-level functions and methods by name. A nested function counts as
    part of the one it sits in."""
    out: dict[str, list] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.FunctionDef):
            out.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    out.setdefault(item.name, []).append(item)
    return out


def _calls(fn) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]


def _safe(name: str, targets: set[str], funcs: dict, seen: frozenset = frozenset()) -> bool:
    """Every call to one of `targets` inside `name` comes after a guard()
    call in it - or `name` is private and everything that calls it is safe
    the same way (so _send, only reached from guarded functions, is fine)."""
    if name in seen:
        return True
    seen = seen | {name}
    for fn in funcs[name]:
        calls = _calls(fn)
        guard_at = min((c.lineno for c in calls if _call_name(c) == "guard"), default=None)
        for c in calls:
            if _call_name(c) not in targets:
                continue
            if guard_at is not None and guard_at < c.lineno:
                continue
            if not name.startswith("_"):
                return False                     # public, and acts before any guard
            callers = [n for n, fns in funcs.items() if n != name
                       and any(_call_name(c2) == name for f in fns for c2 in _calls(f))]
            if not callers or not all(_safe(n, {name}, funcs, seen) for n in callers):
                return False
    return True


@pytest.mark.parametrize("rel", sorted(SIDE_EFFECTS))
def test_every_real_side_effect_comes_after_a_guard(rel):
    funcs = _functions(ast.parse(_read(os.path.join(ROOT, rel))))
    # window.py calls its own prototypes: _SetForegroundWindow, _ShowWindow...
    targets = SIDE_EFFECTS[rel] | {"_" + n for n in SIDE_EFFECTS[rel]}
    risky = {n for n, fns in funcs.items()
             if any(_call_name(c) in targets for f in fns for c in _calls(f))}
    if rel == "mayhem/engine.py":
        assert risky == set()                    # the engine only acts through its deps
        return
    assert risky, f"no side-effect call found in {rel}: the scan is broken"
    for name in sorted(risky):
        assert _safe(name, targets, funcs), f"{rel}: {name}() acts before a guard"


def test_only_discord_and_update_ever_reach_the_network():
    """Two ways out, both opt-out-able and both guarded: your own Discord
    webhook (mayhem/discord.py) and the update check (mayhem/update.py).
    Only update.py knows GitHub's API, and nothing else imports a network
    module (rejoin.py's urllib.parse only takes links apart)."""
    net = {"mayhem/discord.py", "mayhem/update.py"}
    net_modules = {"urllib.request", "http.client", "socket", "ssl", "requests"}
    seen = set()
    for rel in APP_FILES:
        name = rel.replace(os.sep, "/")
        text = _read(os.path.join(ROOT, rel))
        imported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
        if "urlopen" in text:
            seen.add(name)
        if name not in net:
            assert not imported & net_modules, (name, imported & net_modules)
        if name != "mayhem/update.py":
            assert "api.github.com" not in text, name
    assert seen == net


def test_the_guard_scan_catches_an_unguarded_call():
    src = ("def ok():\n    guard('x')\n    _send()\n"
           "def _send():\n    SendInput()\n"
           "def bad():\n    _send()\n")
    funcs = _functions(ast.parse(src))
    assert _safe("ok", {"_send"}, funcs)
    assert not _safe("_send", {"SendInput"}, funcs)          # bad() reaches it unguarded
    del funcs["bad"]
    assert _safe("_send", {"SendInput"}, funcs)


# --------------------------------------------------- what the user can read ---
def _strings(path: str) -> list[str]:
    """Every string constant in a file except docstrings (those are code notes)."""
    tree = ast.parse(_read(path))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


APP_FILES = ["mayhemmacro.py", "run_mayhem.pyw"] + [
    os.path.join("mayhem", f) for f in sorted(os.listdir(os.path.join(ROOT, "mayhem")))
    if f.endswith(".py")]


# The one place another app is named on purpose: Settings' hint that GlassMacro
# uses F8 too, so the roll key can be moved when both apps are open.
NAMED_ON_PURPOSE = {"GlassMacro also uses F8 · if both apps are open, pick another key."}


@pytest.mark.parametrize("rel", APP_FILES)
def test_no_other_apps_or_ai_credit_in_text_the_user_can_see(rel):
    bad = re.compile(r"(?i:glassmacro|rivals|claude|anthropic|openai|chatgpt"
                     r"|co-authored)|\bAI\b")
    hits = [s for s in _strings(os.path.join(ROOT, rel))
            if bad.search(s) and s not in NAMED_ON_PURPOSE]
    assert hits == []


def test_no_print_calls_in_the_app():
    """The exe has no console; and a print is where a link would leak."""
    for rel in APP_FILES:
        tree = ast.parse(_read(os.path.join(ROOT, rel)))
        prints = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "print"]
        assert prints == [], (rel, prints)


# ------------------------------------------------- requirements and launcher ---
def _requirements() -> dict[str, str]:
    pins = {}
    for line in _read(os.path.join(ROOT, "requirements.txt")).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, ver = line.partition("==")
            assert ver, f"not pinned: {line}"
            pins[name.strip().lower()] = ver.strip()
    return pins


def test_requirements_are_pinned_to_what_the_venv_has():
    for name, ver in _requirements().items():
        assert importlib.metadata.version(name) == ver, name


def test_every_package_the_app_imports_is_in_requirements():
    pins = _requirements()
    dists = importlib.metadata.packages_distributions()
    tops = set()
    for rel in APP_FILES:
        for node in ast.walk(ast.parse(_read(os.path.join(ROOT, rel)))):
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                tops.add(node.module.split(".")[0])
    third = {t for t in tops if t not in sys.stdlib_module_names and t not in ("mayhem",
                                                                              "mayhemmacro")}
    assert {"PIL", "customtkinter", "keyboard", "winrt"} <= third
    for top in third:
        for dist in dists.get(top, [top]):
            assert dist.lower() in pins, f"{top} ({dist}) is not in requirements.txt"


def test_nothing_the_app_imports_is_listed_as_test_only():
    """A build (or a user) that installs only the app's part of
    requirements.txt must still have every package the app imports."""
    text = _read(os.path.join(ROOT, "requirements.txt"))
    app_part = text.split("# only for the tests", 1)[0]
    app_pins = {ln.split("#", 1)[0].split("==")[0].strip().lower()
                for ln in app_part.splitlines() if ln.split("#", 1)[0].strip()}
    dists = importlib.metadata.packages_distributions()
    tops = set()
    for rel in APP_FILES:
        for node in ast.walk(ast.parse(_read(os.path.join(ROOT, rel)))):
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                tops.add(node.module.split(".")[0])
    third = {t for t in tops if t not in sys.stdlib_module_names
             and t not in ("mayhem", "mayhemmacro")}
    assert "numpy" in third
    for top in third:
        for dist in dists.get(top, [top]):
            assert dist.lower() in app_pins, f"{top} ({dist}) is only in the test part"


# ------------------------------------------------ what the public repo holds ---
def _repo_text_files() -> list[str]:
    """Every text file that goes in the repo (not the venv, caches, the
    local-only fixtures or files .gitignore keeps out)."""
    skip_dirs = {".venv", ".git", "__pycache__", ".pytest_cache", "fixtures", "build", "dist"}
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith((".py", ".pyw", ".md", ".txt", ".gitignore")) or f == "LICENSE":
                if f != "REUSE_MAP.md":
                    out.append(os.path.join(dirpath, f))
    return out


def test_the_repo_has_a_readme_and_an_mit_license():
    readme = _read(os.path.join(ROOT, "README.md"))
    assert "MayhemMacro" in readme and "[MIT](LICENSE)" in readme
    lic = _read(os.path.join(ROOT, "LICENSE"))
    assert lic.startswith("MIT License") and "Copyright (c) 2026 Ariliux" in lic


def test_no_personal_paths_or_borrowed_code_notes_in_the_repo():
    files = _repo_text_files()
    assert any(f.endswith("README.md") for f in files)
    for path in files:
        text = _read(path)
        low = text.lower()
        # an absolute path into someone's own folder, or this PC's user name
        for m in re.finditer(r"c:[\\/]+users[\\/]+([a-z0-9_.-]+)", low):
            assert m.group(1) in ("x", "someone", "testuserxyz"), (path, m.group(0))
        # code is ours or the author's own GlassMacro, never copied from elsewhere
        assert not re.search(r"(?i)\bport(?:ed)?\s+from\s+(?!glassmacro)", text), path
        assert not re.search(r"(?i)co-author(?:ed)?-by|generated\s+with", text), path


def test_gitignore_keeps_local_data_out():
    ignored = {ln.strip() for ln in _read(os.path.join(ROOT, ".gitignore")).splitlines()}
    for want in ("tests/fixtures/", "settings.json", "settings.corrupt-*.json", "stats.json",
                 "log.txt", "events/", "session_bar_preview.png", ".venv/", "__pycache__/"):
        assert want in ignored, want


def _launcher():
    path = os.path.join(ROOT, "run_mayhem.pyw")
    loader = importlib.machinery.SourceFileLoader("run_mayhem_under_test", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)                     # only defines things: main() is not run
    return mod


def test_the_launcher_knows_the_project_python():
    L = _launcher()
    assert L.VENV_PYTHONW.endswith(os.path.join(".venv", "Scripts", "pythonw.exe"))
    assert L.in_project_venv() == (os.path.normcase(os.path.abspath(sys.prefix))
                                   == os.path.normcase(os.path.join(ROOT, ".venv")))


def test_the_launcher_hands_over_to_the_venv_without_a_console():
    """Opened by some other Python, it starts itself again with .venv's
    pythonw (no console). Nothing is really started here: popen is a fake."""
    L = _launcher()
    started = []
    assert L.hand_over(popen=lambda args, **kw: started.append((args, kw)),
                       exists=lambda p: p == L.VENV_PYTHONW) is True
    (args, kw), = started
    assert args == [L.VENV_PYTHONW, os.path.join(ROOT, "run_mayhem.pyw")]
    assert kw["cwd"] == ROOT and args[0].endswith("pythonw.exe")
    assert L.hand_over(popen=lambda *a, **k: started.append(a),
                       exists=lambda p: False) is False      # no .venv: nothing to hand to
    assert len(started) == 1
