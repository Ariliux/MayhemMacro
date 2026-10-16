"""Discord alerts through the user's own webhook.

Opt-in only: nothing is sent until the user turns alerts on AND pastes their
own webhook link. A webhook link works like a password for one channel, so
it is never logged and is masked anywhere it is shown. MAYHEM_NO_SEND blocks
every post outright, and while testing (MAYHEM_TESTING=1) the real network
is never reached either: only a transport a test passes in gets called.

The app decides WHAT to send (one place applies the user's switches, see
should_send / mention_for); this module builds the messages and posts them.
"""
import copy
import io
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.error

from mayhem import APP_NAME, APP_VER
from mayhem import paths, safety

HOOK_RE = (r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/"
           r"(?:v\d{1,2}/)?webhooks/(\d{17,20})/([A-Za-z0-9_-]{60,100})/?")
HOOK_UA = f"{APP_NAME}/{APP_VER}"
HOOK_TIMEOUT = 10
HOOK_BATCH_WAIT = 2.0          # gather events this long into one message
HOOK_MAX_EMBEDS = 10           # Discord's limits for one message
HOOK_MAX_CHARS = 6000
HOOK_MAX_FILE = 8_000_000      # Discord takes 10 MB a message; stay well under
HOOK_GAP = 2.5                 # at most one post this often
HOOK_BACKOFF = (2, 5, 15, 30, 60)
HOOK_429_WAITS = 3
HOOK_ACCENT, HOOK_GREEN, HOOK_AMBER, HOOK_RED, HOOK_MUTED = (
    0x5ECBFF, 0x4ADE80, 0xFBBF24, 0xF87171, 0x56687E)
HOOK_RARE, HOOK_CHROMATIC, HOOK_INVERTED = 0xC084FC, 0xF472B6, 0xE2E8F0
HOOK_COLOURS = {"rare": HOOK_RARE, "chromatic": HOOK_CHROMATIC,
                "inverted": HOOK_INVERTED, "start": HOOK_ACCENT,
                "test": HOOK_ACCENT, "hourly": HOOK_GREEN,
                "rejoined": HOOK_GREEN, "rejoin": HOOK_AMBER,
                "rejoin_fail": HOOK_AMBER, "storage": HOOK_AMBER,
                "stop": HOOK_MUTED, "closed": HOOK_MUTED,
                "error": HOOK_RED, "stuck": HOOK_RED}
# The webhook's picture: the app icon, from the public repo (no user data).
AVATAR_URL = "https://raw.githubusercontent.com/Ariliux/MayhemMacro/main/docs/icon.png"

# Rarity tiers for rare-block alerts: (at least 1 in N, emoji, colour, name).
TIERS = ((1_000_000_000, "\U0001F308", 0xF472B6, "Godly"),
         (100_000_000, "\U0001F525", 0xEF4444, "Mythic"),
         (10_000_000, "\u2B50", 0xF5A524, "Legendary"),
         (1_000_000, "\U0001F52E", 0xA855F7, "Epic"),
         (0, "\U0001F48E", 0x38BDF8, "Rare"))
VARIANT_TAG = {"inverted": "\u26AA Inverted", "chromatic": "\U0001F308 Chromatic"}


def tier(denominator) -> tuple[str, int, str]:
    """(emoji, colour, name) for a rarity; unknown rarity counts as Rare."""
    try:
        n = int(denominator or 0)
    except (TypeError, ValueError):
        n = 0
    for floor, emoji, colour, name in TIERS:
        if n >= floor:
            return emoji, colour, name
    return TIERS[-1][1:]


# when the queue is full the least important go first
PRI_PERIODIC, PRI_MILESTONE, PRI_NORMAL = 0, 1, 2

# The alert switches on the Discord page (settings["discord"]["send"]) and
# the ones that can @mention the user (settings["discord"]["mention"]).
GROUPS = ("rare", "run", "rejoin", "storage", "error", "stuck", "hourly")
MENTION_GROUPS = ("rare", "error", "stuck")

PICTURE_NAME = "picture.png"   # fixed: a file name can never leak a path
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def webhook_defaults() -> dict:
    """The settings["discord"] block of a fresh install."""
    return copy.deepcopy(paths.DEFAULT_SETTINGS["discord"])


def _hook_parts(url) -> tuple[str, str] | None:
    m = re.fullmatch(HOOK_RE, str(url or "").strip())
    return m.groups() if m else None


def normalize_hook(url) -> str | None:
    """The canonical https://discord.com/api/webhooks/<id>/<token>, or None
    for anything that is not a Discord webhook link."""
    parts = _hook_parts(url)
    if not parts:
        return None
    return f"https://discord.com/api/webhooks/{parts[0]}/{parts[1]}"


def mask_hook(url) -> str:
    """'discord.com/…/webhooks/1234…5678/••••' - safe to show; '' if invalid."""
    parts = _hook_parts(url)
    if not parts:
        return ""
    wid = parts[0]
    return f"discord.com/…/webhooks/{wid[:4]}…{wid[-4:]}/••••"


def scrub(text, url=None) -> str:
    """Text that is safe to send: no webhook token or link, no Roblox server
    link or code, no paths, no PC or user name, at most 300 characters."""
    s = str(text or "")
    parts = _hook_parts(url)
    if parts:
        s = s.replace(parts[1], "••••")
    s = re.sub(r"https?://\S*webhooks/\S+", "[webhook link]", s)
    # private server links: roblox.com/share?code=..., ...?privateServerLinkCode=...,
    # roblox:// deep links and ro.blox.com short links
    s = re.sub(r"roblox://\S*", "[server link]", s, flags=re.I)
    s = re.sub(r"(?:https?://)?(?:[\w-]+\.)*(?:roblox\.com|ro\.blox\.com)/\S*",
               "[server link]", s, flags=re.I)
    s = re.sub(r"\b((?:privateServer)?linkCode|code)=[\w-]+", r"\1=••••", s, flags=re.I)
    s = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"<>|]*", "[path]", s)
    s = re.sub(r"\\\\[^\s'\"<>|]+", "[path]", s)
    for name in {os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME")}:
        if name and len(name) >= 3:
            s = re.sub(re.escape(name), "[name]", s, flags=re.I)
    return s if len(s) <= 300 else s[:297] + "..."


def build_embed(kind: str, title, desc, colour_int: int | None = None,
                fields=(), footer="") -> dict:
    """One Discord embed. The footer is only ever 'MayhemMacro <ver>' (plus
    the run number) - never the PC name, the user name or a path."""
    embed = {"title": str(title)[:256],
             "description": str(desc or "")[:4096],
             "color": int(colour_int if colour_int is not None
                          else HOOK_COLOURS.get(kind, HOOK_MUTED)),
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if fields:
        embed["fields"] = [{"name": str(n)[:256], "value": str(v)[:1024] or "-",
                            "inline": True} for n, v in list(fields)[:25]]
    if footer:
        embed["footer"] = {"text": str(footer)[:2048]}
    return embed


def build_payload(embeds, mention_ids=()) -> dict:
    """The message. Mentions are opt-in per user id; parse is always empty,
    so text in an embed can never ping @everyone or a role."""
    ids = [i for i in dict.fromkeys(str(m) for m in mention_ids if m)
           if re.fullmatch(r"\d{17,20}", i)]
    allowed: dict = {"parse": []}
    if ids:
        allowed["users"] = ids
    return {"username": APP_NAME, "avatar_url": AVATAR_URL,
            "content": " ".join(f"<@{i}>" for i in ids),
            "allowed_mentions": allowed,
            "embeds": list(embeds)}


def embed_chars(embed: dict) -> int:
    """What Discord counts toward its 6000-character limit."""
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len((embed.get("footer") or {}).get("text", ""))
    for f in embed.get("fields") or []:
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


# --------------------------------------------------------------- pictures ---
def to_png(picture) -> bytes | None:
    """PNG bytes for an optional picture: PNG bytes as they are, or a PIL
    image encoded (halved until it fits under HOOK_MAX_FILE). None when there
    is no usable picture - the alert then goes out without one."""
    if picture is None:
        return None
    if isinstance(picture, (bytes, bytearray)):
        data = bytes(picture)
        return data if data.startswith(PNG_MAGIC) and len(data) <= HOOK_MAX_FILE else None
    try:
        img = picture
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        for _ in range(4):
            buf = io.BytesIO()
            img.save(buf, "PNG")
            data = buf.getvalue()
            if len(data) <= HOOK_MAX_FILE:
                return data
            img = img.resize((max(1, img.width // 2), max(1, img.height // 2)))
    except Exception:
        return None
    return None


def multipart_body(payload: dict, files) -> tuple[bytes, str]:
    """A multipart/form-data body Discord accepts for a message with files:
    the message as 'payload_json', then each (name, png bytes) as 'files[i]'.
    Returns (body, content type with the boundary)."""
    js = json.dumps(payload).encode("utf-8")
    files = [(re.sub(r"[^A-Za-z0-9._-]", "_", str(n)) or PICTURE_NAME, bytes(d))
             for n, d in files]
    while True:
        boundary = f"{APP_NAME}-{secrets.token_hex(16)}"
        b = boundary.encode("ascii")
        if b not in js and not any(b in d for _, d in files):
            break
    parts = [b"--" + b + b"\r\n",
             b'Content-Disposition: form-data; name="payload_json"\r\n',
             b"Content-Type: application/json\r\n\r\n", js, b"\r\n"]
    for i, (name, data) in enumerate(files):
        parts += [b"--" + b + b"\r\n",
                  f'Content-Disposition: form-data; name="files[{i}]"; '
                  f'filename="{name}"\r\n'.encode("ascii"),
                  b"Content-Type: image/png\r\n\r\n", data, b"\r\n"]
    parts.append(b"--" + b + b"--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _attach(payload: dict, index: int) -> dict:
    """The payload with embed `index` showing picture.png and the attachment
    listed (Discord matches attachments[].id to files[id])."""
    out = dict(payload)
    embeds = [dict(e) for e in payload.get("embeds") or []]
    if 0 <= index < len(embeds):
        embeds[index]["image"] = {"url": f"attachment://{PICTURE_NAME}"}
    out["embeds"] = embeds
    out["attachments"] = [{"id": 0, "filename": PICTURE_NAME}]
    return out


def _detach(payload: dict) -> dict:
    """The payload without the picture (when Discord says it's too big)."""
    out = {k: v for k, v in payload.items() if k != "attachments"}
    out["embeds"] = [{k: v for k, v in e.items() if k != "image"}
                     for e in payload.get("embeds") or []]
    return out


# ------------------------------------------------------------ the network ---
def _http(req, timeout):
    """The one place a request leaves the app. urlopen is looked up on every
    call, so a test that fakes urllib.request.urlopen still catches it."""
    safety.guard("Discord post")
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)


class WebhookSender:
    """Posts alerts on its own daemon thread, started on the first enqueue.

    enqueue() never blocks. Events are batched (up to 2 s, 10 embeds, 6000
    characters and one picture a post), posts are paced, rate limits and
    outages are waited out, and a link Discord rejects stops everything for
    the session. Only state changes are logged, as fixed 'webhook:' lines
    that never contain the link or any word the log readers react to.
    """

    def __init__(self, url, *, transport=None, sleep=time.sleep, log=None):
        self.url = normalize_hook(url) or ""
        parts = _hook_parts(self.url)
        self._token = parts[1] if parts else ""
        self._transport = transport      # None = _http, looked up per post
        self._sleep = sleep
        self._log = log
        self._q: queue.Queue = queue.Queue(30)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending = 0
        self._hurry = False
        self._last_post: float | None = None
        self._failing = False
        self._retry = 1.0
        self.dead = False
        self.dropped = 0
        self.sent = 0

    def _say(self, line: str) -> None:
        if self._log:
            try:
                self._log(line)
            except Exception:
                pass

    def _done(self, n: int) -> None:
        with self._lock:
            self._pending = max(0, self._pending - n)

    # ---- queue ----
    def enqueue(self, event: dict, priority: int = PRI_NORMAL) -> bool:
        """Queue {"embed": ..., "mention": user id or None, "picture": PNG
        bytes, a PIL image or None}. Never blocks; False if it was dropped."""
        if self.dead or not self.url:
            return False
        item = (int(priority), event)
        try:
            with self._lock:
                self._q.put_nowait(item)
                self._pending += 1
        except queue.Full:
            q = self._q
            with self._lock, q.mutex:
                dq = q.queue
                if len(dq) < q.maxsize:          # the sender took one meanwhile
                    dq.append(item)
                    q.unfinished_tasks += 1
                    q.not_empty.notify()
                    self._pending += 1
                else:
                    low = min(range(len(dq)), key=lambda i: dq[i][0])
                    self.dropped += 1
                    if dq[low][0] > item[0]:
                        return False             # the new one matters least
                    del dq[low]
                    dq.append(item)
        self._start()
        return True

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._loop, daemon=True,
                                 name=f"{APP_NAME} webhook")
            self._thread = t
        t.start()

    def _loop(self) -> None:
        while not self.dead:
            try:
                first = self._q.get(timeout=60)
            except queue.Empty:
                continue
            try:
                self._run_batch(self._gather(first, HOOK_BATCH_WAIT))
            except Exception:
                pass
        self._discard()

    def _gather(self, first, wait: float) -> list[dict]:
        batch = [first[1]]
        end = time.monotonic() + wait
        while len(batch) < HOOK_MAX_EMBEDS:
            left = 0 if self._hurry else end - time.monotonic()
            try:
                item = (self._q.get(timeout=min(left, 0.1)) if left > 0
                        else self._q.get_nowait())
            except queue.Empty:
                if left > 0:
                    continue
                break
            batch.append(item[1])
        return batch

    def _discard(self) -> None:
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                break
        self._done(n)

    def pump(self) -> list[str]:
        """Send everything queued right now on the calling thread, without
        the batching wait. Returns the statuses (tests use it)."""
        out: list[str] = []
        while True:
            try:
                first = self._q.get_nowait()
            except queue.Empty:
                return out
            out += self._run_batch(self._gather(first, 0))

    @staticmethod
    def _groups(events):
        """Split a batch into posts: 10 embeds, 6000 characters and at most
        one picture each."""
        group, chars, pic = [], 0, False
        for ev in events:
            c = embed_chars(ev["embed"])
            has = ev.get("picture") is not None
            if group and (len(group) >= HOOK_MAX_EMBEDS
                          or chars + c > HOOK_MAX_CHARS or (has and pic)):
                yield group
                group, chars, pic = [], 0, False
            group.append(ev)
            chars += c
            pic = pic or has
        if group:
            yield group

    def _run_batch(self, events: list[dict]) -> list[str]:
        out = []
        try:
            for group in self._groups(events):
                if self.dead:
                    out.append("dead")
                    break
                payload = build_payload([e["embed"] for e in group],
                                        [e.get("mention") for e in group])
                files = []
                for i, e in enumerate(group):
                    png = to_png(e.get("picture"))
                    if png:
                        payload = _attach(payload, i)
                        files = [(PICTURE_NAME, png)]
                        break
                out.append(self._send(payload, files))
        finally:
            self._done(len(events))
        return out

    # ---- posting ----
    def _pace(self) -> None:
        if self._last_post is None or self._hurry:
            return
        gap = HOOK_GAP - (time.monotonic() - self._last_post)
        if gap > 0:
            self._sleep(gap)

    def _send(self, payload: dict, files=()) -> str:
        """'ok', 'blocked', 'dropped' or 'dead'."""
        waits = backoffs = 0
        while not self.dead:
            self._pace()
            code = self._post(payload, files)
            if code == "blocked":
                return "blocked"
            self._last_post = time.monotonic()
            if code in (200, 204):
                self.sent += 1
                if self._failing:
                    self._failing = False
                    self._say("webhook: reaching Discord again")
                return "ok"
            if code == 429:
                waits += 1
                if self._hurry or waits > HOOK_429_WAITS:
                    return "dropped"          # Send test / close: never wait
                self._sleep(self._retry + 0.25)
                continue
            if code in (401, 403, 404):
                self.dead = True
                self._say(f"webhook: Discord says the link no longer works "
                          f"({code}) - check the Discord page")
                return "dead"
            if code == 413 and files:
                # the picture was too big: the words still matter, send those
                payload, files = _detach(payload), ()
                continue
            if 400 <= code < 500:
                return "dropped"              # a bad message: never resent
            # 5xx, no network, a timeout
            if not self._failing:
                self._failing = True
                self._say("webhook: couldn't reach Discord - will retry")
            if backoffs >= len(HOOK_BACKOFF) or self._hurry:
                return "dropped"
            self._sleep(HOOK_BACKOFF[backoffs])
            backoffs += 1
        return "dead"

    def _request(self, payload: dict, files=()):
        """The POST. The sender's own token is masked out of the message
        text as a last line of defence, whatever the caller put in it."""
        import urllib.request
        if self._token:
            payload = json.loads(json.dumps(payload).replace(self._token, "••••"))
        if files:
            data, ctype = multipart_body(payload, files)
        else:
            data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
        return urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"User-Agent": HOOK_UA, "Content-Type": ctype})

    def _post(self, payload: dict, files=()):
        """The HTTP status as an int (0 = no answer), or 'blocked'."""
        if os.environ.get("MAYHEM_NO_SEND"):
            return "blocked"
        send = self._transport
        if send is None:
            if safety.testing():
                return "blocked"              # tests never reach the network
            send = _http
        req = self._request(payload, files)
        try:
            r = send(req, HOOK_TIMEOUT)
            try:
                code = getattr(r, "status", None) or r.getcode()
            finally:
                try:
                    r.close()
                except Exception:
                    pass
            return int(code)
        except safety.SideEffectBlocked:
            return "blocked"
        except urllib.error.HTTPError as e:
            self._retry = 1.0
            if e.code == 429:
                self._retry = self._retry_after(e)
            try:
                e.close()
            except Exception:
                pass
            return int(e.code)
        except Exception:
            return 0

    @staticmethod
    def _retry_after(e) -> float:
        secs = None
        try:
            secs = float(json.loads(e.read().decode("utf-8"))["retry_after"])
        except Exception:
            try:
                secs = float(e.headers.get("Retry-After"))
            except Exception:
                pass
        return min(60.0, max(0.0, secs)) if secs is not None else 1.0

    def post_now(self, payload: dict, picture=None) -> str:
        """One message right now on the calling thread - the Discord page's
        Send test. Still blocked by MAYHEM_NO_SEND and while testing."""
        png = to_png(picture)
        if png:
            status = self._send(_attach(payload, 0), [(PICTURE_NAME, png)])
        else:
            status = self._send(payload)
        if status == "ok":
            self._say("webhook: test message sent")
        return status

    def flush(self, timeout: float = 2.0) -> bool:
        """Give queued alerts up to `timeout` seconds to go out (on close)."""
        self._hurry = True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                left = self._pending
            if left <= 0 or self.dead:
                return True
            if self._thread is None or not self._thread.is_alive():
                break
            time.sleep(0.05)
        with self._lock:
            return self._pending <= 0


# ------------------------------------------------------ the user's switches ---
def should_send(cfg, group: str) -> bool:
    """settings["discord"] says yes to this group: alerts on, a valid link,
    and that alert ticked."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return False
    if not normalize_hook(cfg.get("webhook")):
        return False
    send = cfg.get("send")
    return isinstance(send, dict) and send.get(group) is True


def mention_for(cfg, group: str, ping: bool = True) -> str | None:
    """The user id to @mention for this alert, or None. Only when the caller
    wants a ping, '@ me' is ticked for the group and the id is a real one."""
    if not ping or group not in MENTION_GROUPS or not isinstance(cfg, dict):
        return None
    mention = cfg.get("mention")
    if not isinstance(mention, dict) or mention.get(group) is not True:
        return None
    uid = str(cfg.get("user_id") or "").strip()
    return uid if re.fullmatch(r"\d{17,20}", uid) else None


def priority_for(group: str) -> int:
    """Which alerts give way first when the queue is full."""
    if group == "hourly":
        return PRI_PERIODIC
    if group in ("run", "rejoin"):
        return PRI_MILESTONE
    return PRI_NORMAL


def make_event(embed: dict, mention: str | None = None, picture=None) -> dict:
    """What WebhookSender.enqueue takes."""
    return {"embed": embed, "mention": mention, "picture": picture}


# ---------------------------------------------------------- the messages ---
def span(secs) -> str:
    """'2h 14m' for a duration, '14m' under an hour, '40s' under a minute."""
    s = int(max(0, secs or 0))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def footer_text(run_no: int | None = None) -> str:
    return f"{APP_NAME} {APP_VER}" + (f" · run #{run_no}" if run_no else "")


def _block_name(block) -> str:
    """'snakeblock' -> 'SNAKEBLOCK' through the known-blocks table when it
    is there; otherwise a lower-case name just gets a capital letter."""
    raw = scrub(block).strip()
    if not raw:
        return "A rare block"
    try:
        from mayhem.blocks import display_name
        name = str(display_name(raw) or "").strip()
        if name:
            return name
    except Exception:
        pass
    return raw[:1].upper() + raw[1:] if raw.islower() else raw


def _rarity(denominator) -> str:
    try:
        d = int(denominator)
    except (TypeError, ValueError):
        return ""
    return f"1 in {d:,}" if d > 1 else ""


def rare_message(block, denominator, variant="normal", world=None,
                 source="banner", *, run_no=None) -> dict:
    """A rare block alert. source: 'banner' (seen on screen), 'log_song'
    (its music started) or 'log_chromatic' (Roblox notes those late)."""
    name = _block_name(block)
    variant = str(variant or "normal").lower()
    if source == "log_chromatic":
        variant = "chromatic"
    if variant in ("inverted", "chromatic") and not name.lower().startswith(variant):
        name = f"{variant.title()} {name}"
    emoji, colour, tier_name = tier(denominator)
    if source == "log_chromatic":
        article = "An" if name[:1].lower() in "aeiou" else "A"
        title = f"{article} {name} was on your server"
        desc = "Roblox only notes these a few minutes late, so it may be gone already."
    elif source == "log_song":
        title = f"{name} appeared!"
        desc = "Its music started playing on your server."
    else:
        title = f"{name} spawned!"
        desc = "It's on your server right now."
    fields = []
    if _rarity(denominator):
        fields.append(("Rarity", f"**{_rarity(denominator)}**"))
    fields.append(("Tier", f"{emoji} {tier_name}"))
    if variant in VARIANT_TAG:
        fields.append(("Variant", VARIANT_TAG[variant]))
    if world:
        fields.append(("World", scrub(world)))
    return build_embed("rare", f"{emoji} {title}", desc, colour, fields, footer_text(run_no))


def collected_message(block, denominator, variant="normal", how="chat", *, run_no=None) -> dict:
    """A rare block that was actually picked up: by you (chat) or a worker."""
    name = _block_name(block)
    variant = str(variant or "normal").lower()
    if variant in ("inverted", "chromatic") and not name.lower().startswith(variant):
        name = f"{variant.title()} {name}"
    emoji, colour, tier_name = tier(denominator)
    who = "A worker picked it up." if how == "pickup" else "It's in your inventory."
    fields = []
    if _rarity(denominator):
        fields.append(("Rarity", f"**{_rarity(denominator)}**"))
    fields.append(("Tier", f"{emoji} {tier_name}"))
    if variant in VARIANT_TAG:
        fields.append(("Variant", VARIANT_TAG[variant]))
    return build_embed("rare", f"\u2705 Collected {name}!", who, colour, fields, footer_text(run_no))


def run_message(kind: str, *, ran=None, counts=None, lifetime=None,
                hours=None, run_no=None) -> dict:
    """kind: 'start', 'stop', 'closed' (the app was closed mid-run) or
    'hourly'. counts = {"Rares": 2, "Votes": 5, ...} shown as fields."""
    count_fields = [(str(k), str(v)) for k, v in (counts or {}).items()]
    life = [("Lifetime", span(lifetime))] if lifetime is not None else []
    if kind == "start":
        title, desc = "Run started", "Keeping you in Block Mayhem X."
        fields = life
    elif kind == "stop":
        title, desc = "Run stopped", ""
        fields = [("Ran", span(ran))] + count_fields + life
    elif kind == "closed":
        title, desc = f"{APP_NAME} was closed", "The run ended with it."
        fields = [("Ran", span(ran))] + count_fields + life
    elif kind == "hourly":
        h = int(hours) if hours else max(1, int((ran or 0) // 3600))
        title, desc = f"{h}h in Block Mayhem X", "Still going."
        fields = [("Playtime", span(ran))] + count_fields + life
    else:
        raise ValueError(f"unknown run message: {kind}")
    return build_embed(kind, title, desc, None, fields, footer_text(run_no))


def rejoin_message(phase: str, *, tries: int = 1, why="", wait=None,
                   public: bool = False, run_no=None) -> dict:
    """phase: 'start', 'ok', 'fail' or 'gave_up' (the same as the engine's
    rejoin events). public=True when no private server link is set."""
    why = scrub(why)
    where = "a public server" if public else "your server"
    if phase == "start":
        title = f"Rejoining {where}"
        desc = why or "Roblox closed or you got disconnected."
        return build_embed("rejoin", title, desc, None, [("Try", str(tries))],
                           footer_text(run_no))
    if phase == "ok":
        desc = f"Rejoined {where}." + (f" It took {tries} tries." if tries > 1 else "")
        return build_embed("rejoined", "Back in Block Mayhem X", desc, None, (),
                           footer_text(run_no))
    if phase == "fail":
        desc = why or "Roblox didn't get into the game."
        if wait:
            desc += f" Trying again in {span(wait)}."
        return build_embed("rejoin_fail", "Rejoin didn't work", desc, None,
                           [("Try", str(tries))], footer_text(run_no))
    if phase == "gave_up":
        return stuck_message(why or f"Couldn't rejoin after {tries} tries in a row.",
                             run_no=run_no)
    raise ValueError(f"unknown rejoin phase: {phase}")


def storage_message(n: int, m: int, *, run_no=None) -> dict:
    return build_embed("storage", "Storage is full",
                       f"Your storage is full ({int(n)}/{int(m)}). New blocks "
                       f"won't fit until you make room.", None, (),
                       footer_text(run_no))


def stuck_message(why="", *, run_no=None) -> dict:
    desc = (scrub(why) + "\n" if why else "") + \
        "The run stopped. Check on your PC when you can."
    return build_embed("stuck", "Needs your help", desc, None, (),
                       footer_text(run_no))


def error_message(line="", *, stopped: bool = True, run_no=None) -> dict:
    title = "Stopped by an error" if stopped else "Something went wrong"
    desc = scrub(line) or "The Log page says what happened."
    return build_embed("error", title, desc, None, (), footer_text(run_no))


def sample_message() -> dict:
    """The Discord page's Send test (not called test_* so pytest leaves it alone)."""
    return build_embed("test", f"Test from {APP_NAME}",
                       "Alerts reach this channel. Nothing else was sent.",
                       None, (), f"{APP_NAME} {APP_VER} · test")
