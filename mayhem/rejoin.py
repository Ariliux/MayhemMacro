"""Getting back into the user's server: link parsing, closing Roblox, opening it.

The link the user saved (a private server link, a share link or a roblox://
link) is turned into roblox:// deep links and handed to Windows, which gives
them to the Roblox client or whatever launcher handles roblox:// links. No
cookies, no Roblox web API: the Roblox app is already logged in, and the link
is all it needs.

kill_roblox() and launch() are real side effects and call safety.guard()
before anything else. Everything else here is plain text work.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from mayhem import PLACE_ID, safety

# No link (or one that cannot work) = a public Block Mayhem X server.
PUBLIC_LINK = f"roblox://experiences/start?placeId={PLACE_ID}"
ROBLOX_EXE = "robloxplayerbeta.exe"

# The only links that ever reach Windows. The protocol handler gets the link
# inside a quoted command line, so a quote or a space could add arguments of
# its own; no real Roblox link needs either.
_SAFE_URI = re.compile(r"roblox://[A-Za-z0-9\-._~:/?\[\]@!$&()*+,;=%]{1,2000}", re.IGNORECASE)

# ------------------------------------------------------------ link parsing ---
# What can be wrong with a saved link, in the words the Server page shows.
_ODD = "That link has odd characters in it. Copy it again from Roblox."
_NOT_ROBLOX = "That link isn't from roblox.com. Paste your private server link from Roblox."
_NOT_A_LINK = ("That doesn't look like a link. Copy your private server link from Roblox "
               "and paste it here.")
_SHORT = ("That's a short link. Open it in your browser, then copy the full roblox.com "
          "link from the address bar.")
_OTHER_GAME = "That link is for a different game, not Block Mayhem X."
_NO_CODE = "The link is missing its code. Copy it again from Roblox."
_NOT_SERVER = "That share link isn't for a server. Copy the link of your private server instead."
_GAME_PAGE = ("That's the game's page, not a private server link. To use public servers, "
              "leave the box empty.")
_NOT_PRIVATE = "That isn't a private server link. Copy the link of your private server from Roblox."

# Wrappers a link picks up when it is copied out of a chat message.
_WRAPS = ('""', "''", "<>", "()", "[]", "``")
_TRAILING = ".,;:!)]}>'\"`"
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_BARE_WEB = re.compile(r"(?i)^(?:www\.|web\.|m\.)?roblox\.com/")
# key=value anywhere after the scheme. A roblox://placeID=..&linkCode=.. link
# has no '?' at all, so a plain query-string parser would find nothing in it.
_PAIR = re.compile(r"(?:^|[?&/;])([A-Za-z_]+)=([^&#\s]*)")
# Codes are letters, digits, '-' and '_'; anything after that is junk.
_CODE_CHARS = re.compile(r"[A-Za-z0-9_\-]+")
# Short links from the phone app hide the real link in one of these.
_SHORT_KEYS = ("af_dp", "af_web_dp", "deep_link_value")
_SHORT_DEPTH = 2


@dataclass(frozen=True)
class SavedLink:
    """What a saved link turned out to be."""
    kind: str           # none | private | share | public | bad
    code: str = ""      # the private server code or the share code
    given: str = ""     # a roblox:// link exactly as saved ('' for web links)
    problem: str = ""   # for 'bad': what is wrong with it


def _tidy(text: str) -> str:
    """The link without spaces, quotes or brackets around it and without the
    full stop or bracket that tends to follow it in a sentence."""
    s = str(text or "").strip()
    while len(s) >= 2 and s[0] + s[-1] in _WRAPS:
        s = s[1:-1].strip()
    return s.rstrip(_TRAILING)


def _pairs(link: str) -> dict[str, str]:
    """Every key=value in the link (keys in lower case, the first one wins)."""
    found: dict[str, str] = {}
    for key, value in _PAIR.findall(link.split("#", 1)[0]):
        found.setdefault(key.lower(), value)
    return found


def _place_of(path: str, pairs: dict[str, str]) -> str | None:
    """The game a link points at: /games/<id>/ in a web link, placeId= anywhere."""
    m = re.search(r"(?i)/games/(\d+)", path)
    if m:
        return m.group(1)
    value = pairs.get("placeid", "")
    return value if value.isdigit() else None


def _code(value: str) -> str:
    m = _CODE_CHARS.match(value.strip())
    return m.group(0) if m else ""


def _inside_short_link(link: str, depth: int) -> SavedLink:
    """A ro.blox.com link only helps when it carries the real link in it."""
    if depth < _SHORT_DEPTH:
        pairs = _pairs(link)
        for key in _SHORT_KEYS:
            if pairs.get(key):
                inner = read_link(unquote(pairs[key]), depth + 1)
                if inner.kind in ("private", "share"):
                    return inner
    return SavedLink("bad", problem=_SHORT)


def _what_it_holds(pairs: dict[str, str], path: str, given: str) -> SavedLink:
    """Sort a Roblox link by the code it carries (or why it carries none)."""
    place = _place_of(path, pairs)
    if place is not None and place != PLACE_ID:
        # A private server code only works in its own game.
        return SavedLink("bad", problem=_OTHER_GAME)
    for key, kind in (("privateserverlinkcode", "private"), ("linkcode", "private"),
                      ("code", "share")):
        if key not in pairs:
            continue
        code = _code(pairs[key])
        if not code:
            return SavedLink("bad", problem=_NO_CODE)
        if kind == "share" and pairs.get("type", "").strip().lower() not in ("", "server"):
            return SavedLink("bad", problem=_NOT_SERVER)
        return SavedLink(kind, code=code, given=given)
    if given and place:
        return SavedLink("public", given=given)     # e.g. roblox://experiences/start?placeId=
    where = path.lower()
    if place or where.startswith("/games"):
        return SavedLink("bad", problem=_GAME_PAGE)
    if where.startswith("/share"):
        return SavedLink("bad", problem=_NO_CODE)
    return SavedLink("bad", problem=_NOT_PRIVATE)


def read_link(text: str, depth: int = 0) -> SavedLink:
    """Work out what a pasted link is. Never raises."""
    link = _tidy(text)
    if not link:
        return SavedLink("none")
    if _BARE_WEB.match(link):
        link = "https://" + link                    # copied without the https://
    m = _SCHEME.match(link)
    scheme = m.group(1).lower() if m else ""
    if scheme == "roblox":
        if not _SAFE_URI.fullmatch(link):
            return SavedLink("bad", problem=_ODD)
        return _what_it_holds(_pairs(link), "", link)
    if scheme in ("http", "https"):
        try:
            parts = urlsplit(link)
            host = (parts.hostname or "").lower()
        except ValueError:
            return SavedLink("bad", problem=_NOT_ROBLOX)
        if host == "ro.blox.com":
            return _inside_short_link(link, depth)
        if host != "roblox.com" and not host.endswith(".roblox.com"):
            return SavedLink("bad", problem=_NOT_ROBLOX)
        return _what_it_holds(_pairs(link), parts.path, "")
    if "privateserverlinkcode=" in link.lower():
        # 'Game-Name?privateServerLinkCode=...': a web link with its front cut off.
        return _what_it_holds(_pairs(link), "", "")
    return SavedLink("bad", problem=_NOT_A_LINK)


def deep_links(link: str) -> list[str]:
    """The roblox:// links a rejoin tries, in order. Never empty.

    A roblox:// link that was saved as it is comes first. A private server
    code becomes roblox://placeID=<BMX>&linkCode=<code>, a share code the
    share_links form (a share code is not a link code, so it gets no placeID
    form). No link, or one that can't work (validate_link says why), means a
    public server. The engine takes the next one on each retry.
    """
    found = read_link(link)
    if found.kind == "private":
        # Roblox 0.744+ refuses the old placeID=..&linkCode=.. form ("Ambiguous
        # key combination ... LAUNCH_GAME") and just opens its home screen, so
        # the experiences/start form goes first; the old one stays as a fallback.
        code = quote(found.code, safe='')
        new = f"roblox://experiences/start?placeId={PLACE_ID}&linkCode={code}"
        old = f"roblox://placeID={PLACE_ID}&linkCode={code}"
        return list(dict.fromkeys(t for t in (new, found.given, old) if t))
    elif found.kind == "share":
        made = f"roblox://navigation/share_links?code={quote(found.code, safe='')}&type=Server"
    elif found.kind == "public":
        made = PUBLIC_LINK
    else:
        return [PUBLIC_LINK]
    return list(dict.fromkeys(t for t in (found.given, made) if t))


def validate_link(link: str) -> tuple[bool, str]:
    """(ok, why) for the Server page: is this link something rejoin can use?"""
    found = read_link(link)
    if found.kind == "bad":
        return False, found.problem
    return True, {
        "none": "No link saved, so rejoin will join a public server.",
        "public": "That link joins a public server.",
        "share": "Share link looks good. Rejoin will use your private server.",
    }.get(found.kind, "Private server link looks good. Rejoin will use it.")


def _mask_value(value: str) -> str:
    if len(value) >= 10:
        return value[:4] + "…" + value[-2:]
    if len(value) >= 6:
        return value[:2] + "…"
    return "…"


def mask_link(link: str) -> str:
    """The link with every code shortened, e.g. 'roblox.com/share?code=ab12…ef'.

    Safe for the log, the screen and Discord: a code is never shown whole.
    Anything that is not a Roblox link shows only where it points.
    """
    raw = _tidy(link)
    if not raw:
        return ""
    scheme, sep, rest = raw.partition("://")
    if not sep:
        scheme, rest = "", raw
    rest = rest.split("#", 1)[0]
    host = re.split(r"[/?]", rest, maxsplit=1)[0].lower()
    roblox = (scheme.lower() == "roblox" or host == "roblox.com"
              or host.endswith(".roblox.com") or read_link(raw).kind in ("private", "share"))
    if not roblox:
        # Pasted something else by mistake (a webhook, a message): never echo it.
        if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) and "@" not in host:
            return host + "/…"
        return "(hidden)"
    if host.startswith("www."):
        rest = rest[4:]

    def hide(m: re.Match) -> str:
        key, value = m.group(1), m.group(2)
        if key.lower() in ("placeid", "type"):
            return m.group(0)
        return f"{key}={_mask_value(value)}"

    shown = re.sub(r"([A-Za-z_]+)=([^&?#\s]*)", hide, rest)
    return ("roblox://" if scheme.lower() == "roblox" else "") + shown


# ------------------------------------------------------- closing Roblox ---
# Our own kernel32/advapi32 handles, so the argtypes set here never change the
# shared ctypes.windll ones.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
INVALID_HANDLE = ctypes.c_void_p(-1).value
KILL_WAIT_MS = 3000         # how long to wait for each killed client to be gone


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.TerminateProcess.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_adv.EqualSid.restype = wintypes.BOOL


def _snapshot() -> list[tuple[int, str]]:
    """(pid, exe name) of every process. Only reads the process list."""
    out: list[tuple[int, str]] = []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def _image_name(handle: int) -> str:
    """Full exe path of an open process, '' if Windows won't say."""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return buf.value
    return ""


def _token_user(handle: int) -> ctypes.Array | None:
    """The TOKEN_USER of an open process, as a buffer (its SID points into it)."""
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        need = wintypes.DWORD(0)
        _adv.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(need))
        if not need.value:
            return None
        buf = ctypes.create_string_buffer(need.value)
        if not _adv.GetTokenInformation(token, TOKEN_USER_CLASS, buf, need, ctypes.byref(need)):
            return None
        return buf
    finally:
        _k32.CloseHandle(token)


def _sid_of(buf: ctypes.Array) -> int:
    # TOKEN_USER starts with SID_AND_ATTRIBUTES, which starts with the SID pointer.
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]


def _same_user(handle: int) -> bool:
    """True if the process behind `handle` runs as the same Windows user as us."""
    theirs = _token_user(handle)
    mine = _token_user(_k32.GetCurrentProcess())
    if theirs is None or mine is None:
        return False
    return bool(_adv.EqualSid(_sid_of(theirs), _sid_of(mine)))


def _kill_if_mine(pid: int) -> bool:
    """Hard-kill one Roblox client, only if it is ours. True if it was killed."""
    h = _k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                         False, pid)
    if not h:
        return False                         # gone already, or not ours to touch
    try:
        # The pid may belong to something else by now: check the handle we hold.
        if os.path.basename(_image_name(h)).lower() != ROBLOX_EXE:
            return False
        if not _same_user(h):
            return False
        if not _k32.TerminateProcess(h, 1):
            return False
        # A launcher may ask "close the running instance?" when a link is
        # opened while a client still exists, and nobody is there to answer.
        # So wait until it has really gone before the next one is launched.
        _k32.WaitForSingleObject(h, KILL_WAIT_MS)
        return True
    finally:
        _k32.CloseHandle(h)


def kill_roblox() -> int:
    """Close every Roblox client (RobloxPlayerBeta.exe) of this Windows user.

    Only that exe: not the launcher, not Studio, no other program, nothing of
    another user. Returns how many were closed.
    """
    safety.guard("kill Roblox")
    killed = 0
    for pid, name in _snapshot():
        if name.lower() == ROBLOX_EXE and _kill_if_mine(pid):
            killed += 1
    return killed


def launch(uri: str) -> None:
    """Open a roblox:// link through Windows (the Roblox client or its launcher takes it).

    Refuses anything that is not a plain roblox:// link, so this can never be
    used to start some other program. It cannot tell whether Roblox opened:
    the caller watches the Roblox log for the join.
    """
    safety.guard("launch Roblox")
    if not isinstance(uri, str) or not _SAFE_URI.fullmatch(uri):
        raise ValueError("only roblox:// links can be launched")
    os.startfile(uri)
