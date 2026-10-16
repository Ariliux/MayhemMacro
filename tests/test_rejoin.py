"""rejoin.py: link parsing and masking, and that kill/launch never really happen.

The link codes below are made up. kill_roblox() and launch() are only ever
called with every way out (os.startfile, subprocess, the module's own kernel32
handle, the shared one) replaced by traps that fail the test.
"""
import ctypes
import os
import subprocess
import types

import pytest

from mayhem import PLACE_ID, rejoin
from mayhem.safety import SideEffectBlocked

PUBLIC = f"roblox://experiences/start?placeId={PLACE_ID}"
PS_CODE = "12345678901234567890123456789012"
SHARE_CODE = "ab12cd34ef56ab12cd34ef56ab12cd34"
PRIVATE_URI = f"roblox://placeID={PLACE_ID}&linkCode={PS_CODE}"
SHARE_URI = f"roblox://navigation/share_links?code={SHARE_CODE}&type=Server"
OTHER_GAME = "15532962292"


# ------------------------------------------------------------- deep links ---
@pytest.mark.parametrize("link, want", [
    # private server code links, with and without the game's name and extras
    (f"https://www.roblox.com/games/{PLACE_ID}/Block-Mayhem-X?privateServerLinkCode={PS_CODE}",
     [PRIVATE_URI]),
    (f"https://www.roblox.com/games/{PLACE_ID}?privateServerLinkCode={PS_CODE}", [PRIVATE_URI]),
    (f"https://www.roblox.com/games/{PLACE_ID}/Block-Mayhem-X?privateServerLinkCode={PS_CODE}"
     "&utm_source=discord&x=1#top", [PRIVATE_URI]),
    (f"https://roblox.com/games/{PLACE_ID}/Block-Mayhem-X?PRIVATESERVERLINKCODE={PS_CODE}",
     [PRIVATE_URI]),
    (f"http://web.roblox.com/games/{PLACE_ID}/x?privateServerLinkCode={PS_CODE}", [PRIVATE_URI]),
    (f"roblox.com/games/{PLACE_ID}?privateServerLinkCode={PS_CODE}", [PRIVATE_URI]),   # no https
    (f"www.roblox.com/games/{PLACE_ID}?privateServerLinkCode={PS_CODE}", [PRIVATE_URI]),
    (f"https://www.roblox.com/games/{PLACE_ID}?linkCode={PS_CODE}", [PRIVATE_URI]),
    (f"Block-Mayhem-X?privateServerLinkCode={PS_CODE}", [PRIVATE_URI]),     # the bare form, front cut off
    # share links: only the share_links form (a share code is not a link code)
    (f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server", [SHARE_URI]),
    (f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server&stamp=1712345678",
     [SHARE_URI]),
    (f"https://www.roblox.com/share?type=Server&code={SHARE_CODE}", [SHARE_URI]),
    (f"https://www.roblox.com/share-links?code={SHARE_CODE}&type=Server",
     [SHARE_URI]),
    (f"https://www.roblox.com/share?code={SHARE_CODE}", [SHARE_URI]),
    (f"HTTPS://WWW.ROBLOX.COM/share?CODE={SHARE_CODE}&TYPE=SERVER", [SHARE_URI]),
    # raw roblox:// links: used as given first, deduplicated
    (PRIVATE_URI, [PRIVATE_URI]),
    (f"roblox://placeId={PLACE_ID}&linkCode={PS_CODE}",
     [f"roblox://placeId={PLACE_ID}&linkCode={PS_CODE}", PRIVATE_URI]),
    (f"roblox://placeID={PLACE_ID}&linkCode={PS_CODE}&extra=1",
     [f"roblox://placeID={PLACE_ID}&linkCode={PS_CODE}&extra=1", PRIVATE_URI]),
    (SHARE_URI, [SHARE_URI]),
    (PUBLIC, [PUBLIC]),
    (f"roblox://placeID={PLACE_ID}", [f"roblox://placeID={PLACE_ID}", PUBLIC]),
    # empty = public server
    ("", [PUBLIC]),
    ("   \n", [PUBLIC]),
])
def test_deep_links(link, want):
    assert rejoin.deep_links(link) == want



@pytest.mark.parametrize("link", [
    f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server",
    f"https://www.roblox.com/share-links?code={SHARE_CODE}&type=Server",
    SHARE_URI,
])
def test_a_share_code_is_never_tried_as_a_link_code(link):
    # A share code only works through share_links; as a linkCode it can't
    # reach the private server, so a retry with it would be a wasted try.
    assert not any("linkCode" in c for c in rejoin.deep_links(link))

@pytest.mark.parametrize("link", [
    f'"https://www.roblox.com/share?code={SHARE_CODE}&type=Server"',
    f"<https://www.roblox.com/share?code={SHARE_CODE}&type=Server>",
    f"  https://www.roblox.com/share?code={SHARE_CODE}&type=Server.  ",
    f"(https://www.roblox.com/share?code={SHARE_CODE}&type=Server)",
])
def test_pasted_with_junk_around_it(link):
    assert rejoin.deep_links(link) == [SHARE_URI]


def test_junk_stuck_to_the_code_is_dropped():
    link = f"https://www.roblox.com/games/{PLACE_ID}?privateServerLinkCode={PS_CODE})!"
    assert rejoin.deep_links(link) == [PRIVATE_URI]


def test_phone_short_link_with_the_real_link_inside():
    inner = f"roblox%3A%2F%2Fnavigation%2Fshare_links%3Fcode%3D{SHARE_CODE}%26type%3DServer"
    link = f"https://ro.blox.com/Ebh5?af_dp={inner}&af_web_dp=x"
    assert rejoin.deep_links(link) == [SHARE_URI]
    assert rejoin.validate_link(link)[0]


GARBAGE = [
    "hello",
    "my private server pls",
    "https://example.com/?code=abc123&type=Server",
    "https://www.roblox.com.evil.example/share?code=abc123",
    "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop",
    "C:\\Windows\\System32\\notepad.exe",
    "file:///C:/Windows/System32/calc.exe",
    "javascript:alert(1)",
    'roblox://placeID=75270037454060&linkCode=1" --evil',
    "roblox://placeID=75270037454060&linkCode=1 2",
    "roblox://navigation/home",
    "roblox-player:1+launchmode:play",
    "https://www.roblox.com/",
    f"https://www.roblox.com/games/{PLACE_ID}/Block-Mayhem-X",                 # the game page
    f"https://www.roblox.com/games/{PLACE_ID}?privateServerLinkCode=",        # code missing
    "https://www.roblox.com/share?type=Server",
    f"https://www.roblox.com/share?code={SHARE_CODE}&type=ExperienceDetails",  # not a server
    f"https://www.roblox.com/games/{OTHER_GAME}/Other-Game?privateServerLinkCode={PS_CODE}",  # other game
    f"roblox://placeID={OTHER_GAME}&linkCode={PS_CODE}",
    f"roblox://experiences/start?placeId={OTHER_GAME}",
    "https://ro.blox.com/Ebh5",
]


@pytest.mark.parametrize("link", GARBAGE)
def test_unusable_links_fall_back_to_a_public_server(link):
    assert rejoin.deep_links(link) == [PUBLIC]
    ok, why = rejoin.validate_link(link)
    assert ok is False
    assert why and why[0].isupper() and why.endswith(".")


def test_never_empty_and_always_a_safe_roblox_link():
    links = GARBAGE + ["", PRIVATE_URI, SHARE_URI, PUBLIC,
                       f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server"]
    for link in links:
        cands = rejoin.deep_links(link)
        assert cands, link
        for c in cands:
            assert c.startswith("roblox://")
            assert rejoin._SAFE_URI.fullmatch(c), c


def test_none_is_treated_as_empty():
    assert rejoin.deep_links(None) == [PUBLIC]          # type: ignore[arg-type]


# --------------------------------------------------------------- validate ---
@pytest.mark.parametrize("link, ok, words", [
    ("", True, "public server"),
    (f"https://www.roblox.com/games/{PLACE_ID}/x?privateServerLinkCode={PS_CODE}", True, "looks good"),
    (f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server", True, "looks good"),
    (PRIVATE_URI, True, "looks good"),
    (PUBLIC, True, "public server"),
    ("hello", False, "doesn't look like a link"),
    ("https://example.com/x", False, "isn't from roblox.com"),
    (f"https://www.roblox.com/games/{OTHER_GAME}/Other-Game?privateServerLinkCode=1", False, "different game"),
    (f"https://www.roblox.com/games/{PLACE_ID}/Block-Mayhem-X", False, "game's page"),
    (f"https://www.roblox.com/games/{PLACE_ID}?privateServerLinkCode=", False, "missing its code"),
    (f"https://www.roblox.com/share?code={SHARE_CODE}&type=Profile", False, "isn't for a server"),
    ('roblox://placeID=1&linkCode="x"', False, "odd characters"),
    ("https://ro.blox.com/abc", False, "short link"),
])
def test_validate_link_reasons(link, ok, words):
    got_ok, why = rejoin.validate_link(link)
    assert got_ok is ok
    assert words in why
    # plain English: no codes, no jargon
    assert PS_CODE not in why and SHARE_CODE not in why
    for jargon in ("regex", "parse", "URI", "query", "None"):
        assert jargon not in why


# ------------------------------------------------------------------- mask ---
@pytest.mark.parametrize("link, want", [
    (f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server",
     "roblox.com/share?code=ab12…34&type=Server"),
    (f"https://www.roblox.com/games/{PLACE_ID}/Block-Mayhem-X?privateServerLinkCode={PS_CODE}",
     f"roblox.com/games/{PLACE_ID}/Block-Mayhem-X?privateServerLinkCode=1234…12"),
    (PRIVATE_URI, f"roblox://placeID={PLACE_ID}&linkCode=1234…12"),
    (SHARE_URI, "roblox://navigation/share_links?code=ab12…34&type=Server"),
    (PUBLIC, PUBLIC),
    ("https://www.roblox.com/share?code=abc12&type=Server", "roblox.com/share?code=…&type=Server"),
    ("https://www.roblox.com/share?code=abc1234&type=Server", "roblox.com/share?code=ab…&type=Server"),
    ("https://discord.com/api/webhooks/1234567890/abcdefghijklmnop", "discord.com/…"),
    ("my secret text 12345", "(hidden)"),
    ("", ""),
])
def test_mask_link(link, want):
    assert rejoin.mask_link(link) == want


def test_mask_never_shows_a_whole_code():
    links = [
        f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server&stamp=1712345678",
        f"https://www.roblox.com/games/{PLACE_ID}?privateServerLinkCode={PS_CODE}#frag",
        f"roblox://placeID={PLACE_ID}&linkCode={PS_CODE}",
        f"Block-Mayhem-X?privateServerLinkCode={PS_CODE}",
        f"https://ro.blox.com/Ebh5?af_dp=roblox%3A%2F%2Fnavigation%2Fshare_links%3Fcode%3D"
        f"{SHARE_CODE}%26type%3DServer",
        f"https://www.roblox.com/games/{PLACE_ID}?accessCode={SHARE_CODE}&linkCode={PS_CODE}",
    ]
    for link in links:
        shown = rejoin.mask_link(link)
        assert PS_CODE not in shown and SHARE_CODE not in shown, shown
        assert "1712345678" not in shown
        assert "…" in shown


# ----------------------------------------------------- kill / launch guard ---
@pytest.fixture
def traps(monkeypatch):
    """Every way kill_roblox/launch could touch the real world fails the test."""
    hits: list[str] = []

    def trap(name):
        def fail(*a, **k):
            hits.append(name)
            raise AssertionError(f"{name} was called")
        return fail

    monkeypatch.setattr(os, "startfile", trap("os.startfile"))
    monkeypatch.setattr(os, "kill", trap("os.kill"))
    monkeypatch.setattr(os, "system", trap("os.system"))
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, trap(f"subprocess.{name}"))
    monkeypatch.setattr(ctypes.windll.kernel32, "TerminateProcess", trap("kernel32.TerminateProcess"))
    monkeypatch.setattr(ctypes.windll.kernel32, "OpenProcess", trap("kernel32.OpenProcess"))
    monkeypatch.setattr(rejoin, "_snapshot", trap("_snapshot"))
    monkeypatch.setattr(rejoin, "_kill_if_mine", trap("_kill_if_mine"))

    class NoKernel:
        def __getattr__(self, name):
            hits.append(f"_k32.{name}")
            raise AssertionError(f"kernel32 {name} was called")
    monkeypatch.setattr(rejoin, "_k32", NoKernel())
    monkeypatch.setattr(rejoin, "_adv", NoKernel())
    return hits


def test_kill_roblox_is_blocked_while_testing(traps):
    with pytest.raises(SideEffectBlocked):
        rejoin.kill_roblox()
    assert traps == []


@pytest.mark.parametrize("uri", [PUBLIC, PRIVATE_URI, SHARE_URI, "C:\\Windows\\notepad.exe", ""])
def test_launch_is_blocked_while_testing(traps, uri):
    with pytest.raises(SideEffectBlocked):
        rejoin.launch(uri)
    assert traps == []


def test_guard_comes_first_with_the_right_names(traps, monkeypatch):
    seen: list[str] = []

    def guard(what):
        seen.append(what)
        raise SideEffectBlocked(what)
    monkeypatch.setattr(rejoin, "safety", types.SimpleNamespace(guard=guard))
    with pytest.raises(SideEffectBlocked):
        rejoin.kill_roblox()
    with pytest.raises(SideEffectBlocked):
        rejoin.launch(PUBLIC)
    assert seen == ["kill Roblox", "launch Roblox"]
    assert traps == []


def test_pure_functions_need_no_guard(traps):
    # parsing, checking and masking never touch the real world
    rejoin.deep_links(f"https://www.roblox.com/share?code={SHARE_CODE}&type=Server")
    rejoin.validate_link("hello")
    rejoin.mask_link(PRIVATE_URI)
    assert traps == []


# --------------------------------- past the guard, into fakes only ---
def test_kill_only_targets_roblox_player(monkeypatch, traps):
    monkeypatch.setattr(rejoin, "safety", types.SimpleNamespace(guard=lambda what: None))
    procs = [(4, "System"), (100, "explorer.exe"), (200, "RobloxPlayerBeta.exe"),
             (201, "robloxplayerbeta.EXE"), (202, "RobloxPlayerBeta.exe"),
             (300, "RobloxPlayerLauncher.exe"), (301, "RobloxStudioBeta.exe"),
             (302, "SomeLauncher.exe"), (303, "RobloxCrashHandler.exe"), (400, "python.exe")]
    asked: list[int] = []

    def kill_if_mine(pid):
        asked.append(pid)
        return pid != 202            # 202 belongs to another Windows user
    monkeypatch.setattr(rejoin, "_snapshot", lambda: procs)
    monkeypatch.setattr(rejoin, "_kill_if_mine", kill_if_mine)
    assert rejoin.kill_roblox() == 2
    assert asked == [200, 201, 202]


def test_launch_refuses_anything_but_a_roblox_link(monkeypatch, traps):
    monkeypatch.setattr(rejoin, "safety", types.SimpleNamespace(guard=lambda what: None))
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", opened.append)
    for bad in ("C:\\Windows\\System32\\notepad.exe", "https://example.com", "notepad",
                'roblox://placeID=1&linkCode=2" --evil', "roblox://a b", "", None):
        with pytest.raises(ValueError):
            rejoin.launch(bad)                              # type: ignore[arg-type]
    assert opened == []
    rejoin.launch(PRIVATE_URI)
    assert opened == [PRIVATE_URI]


# ------------------------- read-only checks of the Win32 plumbing ---
def test_process_entry_struct_size():
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "this size is for 64-bit Python"
    assert ctypes.sizeof(rejoin.PROCESSENTRY32W) == 568


def test_snapshot_lists_this_process():
    # Only reads the process list (like Task Manager); opens nothing.
    procs = dict(rejoin._snapshot())
    assert procs.get(os.getpid(), "").lower().startswith("python")


def test_same_user_on_our_own_process():
    # Our own process only: its exe name and its owner are read, nothing else.
    me = rejoin._k32.GetCurrentProcess()
    assert rejoin._same_user(me) is True
    assert os.path.basename(rejoin._image_name(me)).lower().startswith("python")


def test_own_kernel32_handle_not_the_shared_one():
    assert rejoin._k32 is not ctypes.windll.kernel32
