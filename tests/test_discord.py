"""discord.py: Discord alerts, fully offline.

Nothing here can reach Discord. conftest sets MAYHEM_TESTING and
MAYHEM_NO_SEND, and every test runs with urllib's urlopen and
socket.create_connection replaced by a tripwire that records the call and
fails the test. The sender cases use a fake transport passed straight into
WebhookSender, and a fake sleep. The webhook link below is made up.
Ported from GlassMacro's test_webhook.py, plus pictures and the BMX messages.
"""
import io
import json
import os
import re
import socket
import urllib.error
import urllib.request

import pytest
from PIL import Image

from mayhem import APP_VER, paths, safety
from mayhem import discord as D

HOOK_ID = "123456789012345678"
TOKEN = "fakeTOKEN_-" * 6 + "abcd"                # 70 made-up characters
URL = f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN}"
USER = "876543210987654321"
SHARE = "https://www.roblox.com/share?code=4f2a9c0d1e7b44c1a5d2e9f0b3c6d8aa&type=Server"
PRIVATE = ("https://www.roblox.com/games/75270037454060/Block-Mayhem-X"
           "?privateServerLinkCode=55512345678901234567890123456789")
DEEP = "roblox://placeID=75270037454060&linkCode=55512345678901234567890123456789"
FAKE_PNG = D.PNG_MAGIC + bytes(range(256)) * 4 + b"\r\n\r\n--\r\n"   # awkward bytes on purpose

NET: list = []


@pytest.fixture(autouse=True)
def tripwire(monkeypatch):
    """Any real network attempt fails the test, even one swallowed by the
    sender's own error handling (it is recorded and checked afterwards)."""
    NET.clear()

    def refuse(*a, **k):
        NET.append(a)
        pytest.fail("tests never touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    assert not NET, f"{len(NET)} real network call(s)"


@pytest.fixture
def sending(monkeypatch):
    """Lift MAYHEM_NO_SEND for one test, so the FAKE transport gets called.
    MAYHEM_TESTING stays on: the real network stays blocked."""
    monkeypatch.delenv("MAYHEM_NO_SEND", raising=False)
    assert safety.testing()


# ---- the fake transport ------------------------------------------------------------
class Resp:
    def __init__(self, code):
        self.status = code

    def close(self):
        pass


class FakeTransport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append((req, timeout))
        a = self.answers.pop(0) if self.answers else 204
        if isinstance(a, BaseException):
            raise a
        code, body, headers = (a + ({},))[:3] if isinstance(a, tuple) else (a, b"", {})
        if 200 <= code < 300:
            return Resp(code)
        raise urllib.error.HTTPError(req.full_url, code, "x", headers, io.BytesIO(body))

    def payloads(self):
        out = []
        for req, _ in self.calls:
            ctype = req.get_header("Content-type")
            if ctype == "application/json":
                out.append(json.loads(req.data.decode("utf-8")))
            else:
                parts = parse_multipart(req.data, ctype)
                out.append(json.loads(parts[0][1].decode("utf-8")))
        return out


def parse_multipart(body: bytes, ctype: str) -> list[tuple[dict, bytes]]:
    """Split a multipart/form-data body back into (headers, content) parts,
    checking the framing byte by byte."""
    m = re.fullmatch(r"multipart/form-data; boundary=(\S+)", ctype)
    assert m, ctype
    b = m.group(1).encode("ascii")
    assert body.startswith(b"--" + b + b"\r\n")
    assert body.endswith(b"\r\n--" + b + b"--\r\n")
    chunks = body.split(b"--" + b)
    assert chunks[0] == b"" and chunks[-1] == b"--\r\n"
    parts = []
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n")
        head, sep, content = chunk[2:-2].partition(b"\r\n\r\n")
        assert sep
        headers = {}
        for line in head.decode("ascii").split("\r\n"):
            k, _, v = line.partition(": ")
            headers[k.lower()] = v
        parts.append((headers, content))
    return parts


def sender(answers, log=None):
    ft = FakeTransport(answers)
    sleeps = []
    lines = [] if log is None else None
    s = D.WebhookSender(URL, transport=ft, sleep=sleeps.append, log=log or lines.append)
    s._start = lambda: None                       # no thread: pump() by hand
    return s, ft, sleeps, lines


def ev(title="t", mention=None, desc="d", picture=None):
    return D.make_event(D.build_embed("start", title, desc), mention, picture)


# ---- the link ----------------------------------------------------------------------------
GOOD = [URL, URL + "/", "  " + URL + "\n",
        f"https://ptb.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://canary.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://discordapp.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        f"https://discord.com/api/v10/webhooks/{HOOK_ID}/{TOKEN}"]
BAD = ["", None, "hello", URL.replace("https://", "http://"),
       f"https://discord.com.evil.example/api/webhooks/{HOOK_ID}/{TOKEN}",
       f"https://evil.example/discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
       f"https://discord.com/api/webhooks/{HOOK_ID[:16]}/{TOKEN}",
       f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN[:59]}",
       f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN[:-1]}!",
       URL + "?wait=true", URL + "/extra", URL + " " + URL,
       f"https://www.discord.com/api/webhooks/{HOOK_ID}/{TOKEN}"]


def test_links_normalise():
    assert all(D.normalize_hook(u) == URL for u in GOOD)
    assert not any(D.normalize_hook(u) for u in BAD)


def test_mask_shows_only_the_ends_of_the_id():
    masked = D.mask_hook(URL)
    assert masked.startswith("discord.com/") and masked.endswith("/••••")
    assert TOKEN not in masked and TOKEN[:8] not in masked
    assert HOOK_ID[:4] in masked and HOOK_ID[-4:] in masked and HOOK_ID not in masked
    assert D.mask_hook("nope") == ""


def test_scrub_webhook_and_paths():
    s = D.scrub(f"failed {URL} and {TOKEN} in C:\\Users\\someone\\x.png " + "y" * 400, URL)
    assert TOKEN not in s and HOOK_ID not in s and "C:\\" not in s and len(s) <= 300
    assert D.scrub("see https://github.com/x") == "see https://github.com/x"
    assert D.scrub("in C:\\Users\\someone\\x.png") == "in [path]"
    assert D.scrub("(D:/games/x.png)").startswith("([path]")
    assert D.scrub("share \\\\server\\share\\file.txt") == "share [path]"


@pytest.mark.parametrize("link", [SHARE, PRIVATE, DEEP, "ro.blox.com/Ebh5?af_dp=x",
                                  "www.roblox.com/share?code=abcdef123456&type=Server"])
def test_scrub_private_server_links(link):
    s = D.scrub(f"rejoining with {link} now")
    for secret in ("4f2a9c0d1e7b44c1a5d2e9f0b3c6d8aa", "55512345678901234567890123456789",
                   "abcdef123456", "Ebh5"):
        assert secret not in s, s
    assert s.startswith("rejoining with ") and s.endswith(" now")


def test_scrub_bare_link_codes():
    s = D.scrub("linkCode=999888777 privateServerLinkCode=123abc code=zz-top")
    assert "999888777" not in s and "123abc" not in s and "zz-top" not in s


def test_scrub_pc_and_user_names(monkeypatch):
    monkeypatch.setenv("USERNAME", "TestUserXyz")
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-FAKE01")
    s = D.scrub("testuserxyz on DESKTOP-FAKE01 said hi")
    assert s == "[name] on [name] said hi"


# ---- the message ------------------------------------------------------------------------
def test_embed_basics():
    e = D.build_embed("start", "Run started", "x", None, [("Lifetime", "2h")],
                      "MayhemMacro 0.1 · run #3")
    assert e["color"] == 0x5ECBFF and e["footer"]["text"].endswith("run #3")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", e["timestamp"])
    assert e["fields"] == [{"name": "Lifetime", "value": "2h", "inline": True}]
    assert D.build_embed("nope", "t", "")["color"] == D.HOOK_MUTED
    assert len(D.build_embed("x", "t" * 999, "d" * 9999)["title"]) == 256


def test_payload_mentions():
    e = D.build_embed("start", "t", "d")
    p1 = D.build_payload([e])
    p2 = D.build_payload([e], [USER])
    p3 = D.build_payload([e], ["@everyone", "abc", "<@&123>"])
    p4 = D.build_payload([e], [USER, None, USER])
    assert all(p["allowed_mentions"]["parse"] == [] for p in (p1, p2, p3, p4))
    assert p1["content"] == "" and "users" not in p1["allowed_mentions"]
    assert p2["content"] == f"<@{USER}>" and p2["allowed_mentions"]["users"] == [USER]
    assert p3["content"] == "" and "users" not in p3["allowed_mentions"]
    assert p4["content"] == f"<@{USER}>" and p4["allowed_mentions"]["users"] == [USER]
    assert p1["username"] == "MayhemMacro"
    assert set(p1) == {"username", "avatar_url", "content", "allowed_mentions", "embeds"}
    assert p1["avatar_url"].endswith("/docs/icon.png")


def test_webhook_defaults_match_settings():
    assert D.webhook_defaults() == paths.DEFAULT_SETTINGS["discord"]
    w = D.webhook_defaults()
    w["send"]["rare"] = False
    assert paths.DEFAULT_SETTINGS["discord"]["send"]["rare"] is True, "a copy"
    assert set(w["send"]) == set(D.GROUPS)
    assert set(w["mention"]) == set(D.MENTION_GROUPS)


# ---- the sender, with a fake transport --------------------------------------------
def test_one_post(sending):
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev())
    assert s.pump() == ["ok"]
    req, timeout = ft.calls[0]
    assert len(ft.calls) == 1 and req.get_method() == "POST" and req.full_url == URL
    assert timeout == 10
    assert req.get_header("User-agent") == f"MayhemMacro/{APP_VER}"
    assert req.get_header("Content-type") == "application/json"
    assert not lines and s.sent == 1


def test_429_waits_retry_after(sending):
    s, ft, sleeps, lines = sender([(429, b'{"retry_after": 1.5}'), 204])
    s.enqueue(ev())
    assert s.pump() == ["ok"] and len(ft.calls) == 2 and 1.75 in sleeps


def test_429_header_and_cap(sending):
    s, ft, sleeps, lines = sender([(429, b"", {"Retry-After": "2"}),
                                   (429, b'{"retry_after": 900}'), 204])
    s.enqueue(ev())
    assert s.pump() == ["ok"]
    assert 2.25 in sleeps and 60.25 in sleeps, sleeps


def test_429_gives_up_after_3_waits(sending):
    s, ft, sleeps, lines = sender([(429, b'{"retry_after": 0.1}')] * 9)
    s.enqueue(ev())
    assert s.pump() == ["dropped"] and len(ft.calls) == 4


def test_5xx_backs_off_and_logs_only_changes(sending):
    s, ft, sleeps, lines = sender([503, urllib.error.URLError("x"), 204])
    s.enqueue(ev())
    assert s.pump() == ["ok"]
    assert 2 in sleeps and 5 in sleeps
    assert lines == ["webhook: couldn't reach Discord - will retry",
                     "webhook: reaching Discord again"]


def test_outage_drops_the_batch(sending):
    s, ft, sleeps, lines = sender([503] * 9)
    s.enqueue(ev())
    assert s.pump() == ["dropped"] and len(ft.calls) == 6
    assert [x for x in sleeps if x in (2, 5, 15, 30, 60)] == [2, 5, 15, 30, 60]
    assert len(lines) == 1


@pytest.mark.parametrize("code", [401, 403, 404])
def test_rejected_link_is_dead_for_the_session(sending, code):
    s, ft, sleeps, lines = sender([code, 204])
    s.enqueue(ev())
    assert s.pump() == ["dead"] and s.dead and len(ft.calls) == 1
    assert len(lines) == 1 and f"({code})" in lines[0]
    assert TOKEN not in lines[0] and HOOK_ID not in lines[0]
    assert s.enqueue(ev()) is False and s.pump() == [] and len(ft.calls) == 1


def test_400_drops_only_that_message(sending):
    s, ft, sleeps, lines = sender([400, 204])
    s.enqueue(ev())
    assert s.pump() == ["dropped"] and not s.dead and len(ft.calls) == 1


def test_transport_exceptions_are_swallowed(sending):
    s, ft, sleeps, lines = sender([ValueError("odd"), OSError("boom"), TimeoutError(),
                                   RuntimeError()] + [503] * 9)
    s.enqueue(ev())
    assert s.pump() == ["dropped"]


def test_batching(sending):
    s, ft, sleeps, lines = sender([204, 204])
    for i in range(5):
        s.enqueue(ev(f"e{i}"))
    s.pump()
    assert len(ft.calls) == 1 and len(ft.payloads()[0]["embeds"]) == 5

    s, ft, sleeps, lines = sender([204] * 5)
    for i in range(12):
        s.enqueue(ev(f"e{i}"))
    s.pump()
    assert [len(p["embeds"]) for p in ft.payloads()] == [10, 2]

    s, ft, sleeps, lines = sender([204] * 5)
    for i in range(3):
        s.enqueue(ev(f"e{i}", desc="x" * 2500))
    s.pump()
    assert [len(p["embeds"]) for p in ft.payloads()] == [2, 1], "6000 characters a post"


def test_one_mention_in_a_batch(sending):
    s, ft, sleeps, lines = sender([204] * 3)
    s.enqueue(ev("a"))
    s.enqueue(ev("b", mention=USER))
    s.pump()
    p = ft.payloads()[0]
    assert p["allowed_mentions"] == {"parse": [], "users": [USER]}
    assert p["content"] == f"<@{USER}>"


def test_posts_are_paced(sending):
    s, ft, sleeps, lines = sender([204, 204])
    s.enqueue(ev())
    s.pump()
    s.enqueue(ev())
    s.pump()
    assert any(abs(x - D.HOOK_GAP) < 0.5 for x in sleeps), sleeps


def test_send_test(sending):
    s, ft, sleeps, lines = sender([204])
    assert s.post_now(D.build_payload([D.sample_message()])) == "ok"
    assert lines == ["webhook: test message sent"]
    p = ft.payloads()[0]
    assert p["embeds"][0]["title"] == "Test from MayhemMacro"

    s, ft, sleeps, lines = sender([(429, b'{"retry_after": 30}'), 204])
    s._hurry = True
    assert s.post_now(D.build_payload([ev()["embed"]])) == "dropped"
    assert len(ft.calls) == 1 and not sleeps, "a hurried Send test never sleeps"


def test_own_token_never_leaves(sending):
    s, ft, sleeps, lines = sender([204, 204])
    s.enqueue(ev("t", desc=f"oops the token is {TOKEN}"))
    s.pump()
    s.enqueue(ev("t", desc=f"again {TOKEN}", picture=FAKE_PNG))
    s.pump()
    for req, _ in ft.calls:
        assert TOKEN.encode() not in req.data
    assert "oops the token is ••••" in ft.payloads()[0]["embeds"][0]["description"]


def test_full_queue_drops_the_least_important():
    s, ft, sleeps, lines = sender([])
    for i in range(20):
        s.enqueue(ev(f"p{i}"), D.PRI_PERIODIC)
    for i in range(10):
        s.enqueue(ev(f"m{i}"), D.PRI_MILESTONE)

    def pri():
        return [p for p, _ in list(s._q.queue)]

    assert s._q.full() and s.enqueue(ev("n0"), D.PRI_NORMAL)
    assert pri().count(D.PRI_PERIODIC) == 19 and s.dropped == 1 and s._q.qsize() == 30
    titles = [e["embed"]["title"] for _, e in list(s._q.queue)]
    assert "p0" not in titles and "p1" in titles, "the OLDEST periodic one goes"
    for i in range(19):
        s.enqueue(ev(f"n{i + 1}"), D.PRI_NORMAL)
    assert pri().count(D.PRI_PERIODIC) == 0 and pri().count(D.PRI_MILESTONE) == 10
    s.enqueue(ev("n20"), D.PRI_NORMAL)
    assert pri().count(D.PRI_MILESTONE) == 9, "then milestones"
    assert s.enqueue(ev("late"), D.PRI_PERIODIC) is False
    assert s.dropped == 22 and s._q.qsize() == 30


def test_no_link_no_queue():
    s = D.WebhookSender("not a link", transport=FakeTransport([]))
    assert s.url == "" and s.enqueue(ev()) is False


# ---- the kill switches ----------------------------------------------------------------
def test_no_send_blocks_before_the_transport():
    assert os.environ.get("MAYHEM_NO_SEND")
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev())
    assert s.pump() == ["blocked"] and not ft.calls
    assert s.post_now(D.build_payload([ev()["embed"]]), FAKE_PNG) == "blocked"
    assert not ft.calls and not lines


def test_real_post_is_blocked_while_testing(sending):
    """No transport passed = the real network path. With MAYHEM_NO_SEND
    lifted, MAYHEM_TESTING alone must still stop it before urllib."""
    assert not os.environ.get("MAYHEM_NO_SEND")
    s = D.WebhookSender(URL, sleep=lambda x: pytest.fail("no waiting either"))
    s._start = lambda: None
    payload = D.build_payload([ev()["embed"]], [USER])
    assert s._post(payload) == "blocked"
    assert s._post(payload, [(D.PICTURE_NAME, FAKE_PNG)]) == "blocked"
    assert s.post_now(payload) == "blocked"
    assert s.post_now(payload, FAKE_PNG) == "blocked"
    s.enqueue(ev(picture=FAKE_PNG))
    s.enqueue(ev(picture=FAKE_PNG))                  # one picture a post: two posts
    assert s.pump() == ["blocked", "blocked"]
    assert not s.dead and s.sent == 0
    assert not NET


def test_http_itself_is_guarded():
    req = urllib.request.Request(URL, data=b"{}", method="POST")
    with pytest.raises(safety.SideEffectBlocked):
        D._http(req, 1)
    assert not NET


def test_blocked_guard_inside_a_transport_is_blocked(sending):
    """A transport that hits the guard (as _http does) reads as 'blocked',
    not as an outage to retry."""
    def guarded(req, timeout):
        safety.guard("Discord post")

    s = D.WebhookSender(URL, transport=guarded, sleep=lambda x: pytest.fail("no retry"))
    s._start = lambda: None
    s.enqueue(ev())
    assert s.pump() == ["blocked"]


def test_background_thread_and_flush():
    """The real thread, named for the app, with everything still blocked."""
    ft = FakeTransport([204])
    s = D.WebhookSender(URL, transport=ft, sleep=lambda x: None)
    try:
        assert s.enqueue(ev())
        assert s._thread is not None and s._thread.name == "MayhemMacro webhook"
        assert s._thread.daemon
        assert s.flush(5.0) is True
        assert not ft.calls, "MAYHEM_NO_SEND blocked it before the transport"
    finally:
        s.dead = True


# ---- pictures -----------------------------------------------------------------------------
def test_multipart_body_parses_back():
    payload = D.build_payload([D.build_embed("rare", "Resonance spawned!", "d")], [USER])
    body, ctype = D.multipart_body(payload, [("picture.png", FAKE_PNG)])
    parts = parse_multipart(body, ctype)
    assert len(parts) == 2
    h0, c0 = parts[0]
    assert h0["content-disposition"] == 'form-data; name="payload_json"'
    assert h0["content-type"] == "application/json"
    assert json.loads(c0.decode("utf-8")) == payload
    h1, c1 = parts[1]
    assert h1["content-disposition"] == 'form-data; name="files[0]"; filename="picture.png"'
    assert h1["content-type"] == "image/png"
    assert c1 == FAKE_PNG, "the picture bytes arrive exactly"


def test_multipart_also_parses_with_the_email_module():
    import email.parser
    import email.policy
    payload = D.build_payload([D.build_embed("rare", "x", "d")])
    body, ctype = D.multipart_body(payload, [("picture.png", FAKE_PNG)])
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    assert msg.is_multipart()
    names = [p.get_param("name", header="content-disposition") for p in msg.iter_parts()]
    assert names == ["payload_json", "files[0]"]


def test_multipart_boundary_never_in_the_data(monkeypatch):
    clash = "00" * 16
    data = D.PNG_MAGIC + f"MayhemMacro-{clash}".encode()
    tokens = iter([clash, "11" * 16])
    monkeypatch.setattr(D.secrets, "token_hex", lambda n: next(tokens))
    body, ctype = D.multipart_body({"a": 1}, [("picture.png", data)])
    assert ctype.endswith("MayhemMacro-" + "11" * 16)
    assert parse_multipart(body, ctype)[1][1] == data


def test_file_names_are_plain(monkeypatch):
    body, ctype = D.multipart_body({"a": 1}, [('C:\\Users\\x\\"evil".png', FAKE_PNG)])
    head = parse_multipart(body, ctype)[1][0]["content-disposition"]
    assert "\\" not in head and ":" not in head.split("filename=")[1]
    assert head.count('"') == 4


def test_event_with_a_picture_goes_multipart(sending):
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev("words only"))
    s.enqueue(ev("with picture", mention=USER, picture=FAKE_PNG))
    assert s.pump() == ["ok"] and len(ft.calls) == 1
    req = ft.calls[0][0]
    ctype = req.get_header("Content-type")
    assert ctype.startswith("multipart/form-data; boundary=")
    parts = parse_multipart(req.data, ctype)
    p = json.loads(parts[0][1])
    assert [e["title"] for e in p["embeds"]] == ["words only", "with picture"]
    assert "image" not in p["embeds"][0]
    assert p["embeds"][1]["image"] == {"url": "attachment://picture.png"}
    assert p["attachments"] == [{"id": 0, "filename": "picture.png"}]
    assert p["allowed_mentions"] == {"parse": [], "users": [USER]}
    assert parts[1][1] == FAKE_PNG


def test_one_picture_a_post(sending):
    s, ft, sleeps, lines = sender([204, 204])
    s.enqueue(ev("a", picture=FAKE_PNG))
    s.enqueue(ev("b", picture=FAKE_PNG))
    assert s.pump() == ["ok", "ok"]
    assert [[e["title"] for e in p["embeds"]] for p in ft.payloads()] == [["a"], ["b"]]


def test_pil_picture(sending):
    img = Image.new("RGB", (64, 40), (200, 30, 90))
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev(picture=img))
    s.pump()
    req = ft.calls[0][0]
    png = parse_multipart(req.data, req.get_header("Content-type"))[1][1]
    back = Image.open(io.BytesIO(png))
    assert back.format == "PNG" and back.size == (64, 40)
    assert back.convert("RGB").getpixel((5, 5)) == (200, 30, 90)


def test_too_big_picture_is_sent_without_it(sending):
    s, ft, sleeps, lines = sender([413, 204])
    s.enqueue(ev("big", mention=USER, picture=FAKE_PNG))
    assert s.pump() == ["ok"] and len(ft.calls) == 2
    second = ft.calls[1][0]
    assert second.get_header("Content-type") == "application/json"
    p = json.loads(second.data)
    assert p["embeds"][0]["title"] == "big" and "image" not in p["embeds"][0]
    assert "attachments" not in p
    assert p["allowed_mentions"] == {"parse": [], "users": [USER]}


def test_413_without_a_picture_is_dropped(sending):
    s, ft, sleeps, lines = sender([413, 204])
    s.enqueue(ev())
    assert s.pump() == ["dropped"] and len(ft.calls) == 1


def test_to_png(monkeypatch):
    assert D.to_png(None) is None
    assert D.to_png(b"GIF89a....") is None, "only PNG bytes"
    assert D.to_png(FAKE_PNG) == FAKE_PNG
    assert D.to_png("not a picture") is None
    assert D.to_png(Image.new("P", (8, 8))).startswith(D.PNG_MAGIC)
    # a noisy picture over the limit is halved until it fits
    noisy = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))
    monkeypatch.setattr(D, "HOOK_MAX_FILE", 60_000)
    png = D.to_png(noisy)
    assert png and len(png) <= 60_000
    assert Image.open(io.BytesIO(png)).size[0] < 256
    assert D.to_png(D.PNG_MAGIC + b"x" * 60_001) is None


def test_bad_picture_still_sends_the_words(sending):
    s, ft, sleeps, lines = sender([204])
    s.enqueue(ev("words", picture=b"not a png"))
    assert s.pump() == ["ok"]
    assert ft.calls[0][0].get_header("Content-type") == "application/json"


def test_send_test_with_a_picture(sending):
    s, ft, sleeps, lines = sender([204])
    assert s.post_now(D.build_payload([D.sample_message()]), FAKE_PNG) == "ok"
    p = ft.payloads()[0]
    assert p["embeds"][0]["image"] == {"url": "attachment://picture.png"}


# ---- the user's switches ------------------------------------------------------------------
def cfg(**kw):
    c = D.webhook_defaults()
    c.update(enabled=True, webhook=URL, user_id=USER)
    c.update(kw)
    return c


def test_should_send():
    assert all(D.should_send(cfg(), g) for g in D.GROUPS if g != "hourly")
    assert not D.should_send(cfg(), "hourly"), "hourly is off by default"
    assert not D.should_send(D.webhook_defaults(), "rare"), "off until set up"
    assert not D.should_send(cfg(enabled=False), "rare")
    assert not D.should_send(cfg(enabled="yes"), "rare")
    assert not D.should_send(cfg(webhook="https://example.com/x"), "rare")
    off = cfg()
    off["send"]["rare"] = False
    assert not D.should_send(off, "rare")
    assert not D.should_send(cfg(send="?"), "rare")
    assert not D.should_send(None, "rare")
    assert not D.should_send(cfg(), "nonsense")


def test_mention_for():
    assert D.mention_for(cfg(), "rare") == USER
    assert D.mention_for(cfg(), "rare", ping=False) is None, "below rare_ping_min"
    assert D.mention_for(cfg(), "stuck") == USER and D.mention_for(cfg(), "error") == USER
    for g in ("run", "rejoin", "storage", "hourly"):
        assert D.mention_for(cfg(), g) is None, g
    assert D.mention_for(cfg(user_id="123"), "rare") is None
    assert D.mention_for(cfg(user_id="@everyone"), "rare") is None
    assert D.mention_for(cfg(user_id=f" {USER} "), "rare") == USER
    off = cfg()
    off["mention"]["rare"] = False
    assert D.mention_for(off, "rare") is None
    assert D.mention_for(None, "rare") is None


def test_priorities():
    assert D.priority_for("hourly") == D.PRI_PERIODIC
    assert D.priority_for("run") == D.PRI_MILESTONE == D.priority_for("rejoin")
    for g in ("rare", "storage", "error", "stuck"):
        assert D.priority_for(g) == D.PRI_NORMAL


def test_make_event():
    e = D.build_embed("rare", "t", "d")
    assert D.make_event(e) == {"embed": e, "mention": None, "picture": None}
    assert D.make_event(e, USER, FAKE_PNG)["picture"] == FAKE_PNG


# ---- the BMX messages ----------------------------------------------------------------------
def fields(embed):
    return {f["name"]: f["value"] for f in embed.get("fields", [])}


def test_rare_banner():
    e = D.rare_message("Resonance", 100000, "normal", "Dreamland", "banner")
    assert e["title"].endswith("Resonance spawned!")
    assert fields(e) == {"Rarity": "**1 in 100,000**", "Tier": "\U0001F48E Rare", "World": "Dreamland"}
    assert e["color"] == D.tier(100_000)[1]
    assert e["footer"]["text"] == f"MayhemMacro {APP_VER}"


def test_rare_world_unknown_and_no_number():
    e = D.rare_message("Resonance", None, "normal", None, "banner")
    assert e["title"].endswith("Resonance spawned!") and fields(e) == {"Tier": "\U0001F48E Rare"}


def test_rare_from_the_log():
    e = D.rare_message("Sporedelic", 100000, "normal", "Void", "log_song")
    assert e["title"].endswith("Sporedelic appeared!")
    assert fields(e)["Rarity"] == "**1 in 100,000**"
    c = D.rare_message("vexen", None, "chromatic", None, "log_chromatic")
    assert c["title"].endswith("A Chromatic Vexen was on your server")
    assert "late" in c["description"] and c["color"] == D.tier(None)[1]
    c2 = D.rare_message("vexen", None, "normal", None, "log_chromatic")
    assert c2["title"].endswith("A Chromatic Vexen was on your server")


def test_rare_variants():
    e = D.rare_message("Resonance", 100000, "inverted", None, "banner")
    assert e["title"].endswith("Inverted Resonance spawned!") and e["color"] == D.tier(100000)[1]
    e = D.rare_message("Chromatic Vexen", 50000, "chromatic", None, "banner")
    assert e["title"].endswith("Chromatic Vexen spawned!"), "no doubled prefix"
    assert D.rare_message("", None)["title"].endswith("A rare block spawned!")


def test_run_messages():
    s = D.run_message("start", lifetime=7300, run_no=4)
    assert s["title"] == "Run started" and fields(s) == {"Lifetime": "2h 01m"}
    assert s["footer"]["text"] == f"MayhemMacro {APP_VER} · run #4"
    counts = {"Rares": 2, "Votes": 6, "AFK saves": 3, "Rejoins": 1}
    t = D.run_message("stop", ran=3725, counts=counts, lifetime=90000)
    assert t["title"] == "Run stopped"
    assert fields(t) == {"Ran": "1h 02m", "Rares": "2", "Votes": "6", "AFK saves": "3",
                         "Rejoins": "1", "Lifetime": "25h 00m"}
    assert D.run_message("closed", ran=50)["title"] == "MayhemMacro was closed"
    h = D.run_message("hourly", ran=7300, hours=2, counts=counts)
    assert h["title"] == "2h in Block Mayhem X" and fields(h)["Playtime"] == "2h 01m"
    assert D.run_message("hourly", ran=3700)["title"] == "1h in Block Mayhem X"
    with pytest.raises(ValueError):
        D.run_message("party")


def test_rejoin_messages():
    a = D.rejoin_message("start", tries=1, why="You got disconnected.")
    assert a["title"] == "Rejoining your server" and fields(a) == {"Try": "1"}
    assert D.rejoin_message("start", public=True)["title"] == "Rejoining a public server"
    b = D.rejoin_message("ok", tries=2)
    assert b["title"] == "Back in Block Mayhem X" and "2 tries" in b["description"]
    assert b["color"] == D.HOOK_GREEN
    c = D.rejoin_message("fail", tries=3, wait=120)
    assert c["title"] == "Rejoin didn't work" and "2m" in c["description"]
    d = D.rejoin_message("gave_up", tries=5)
    assert d["title"] == "Needs your help" and "5 tries" in d["description"]
    assert d["color"] == D.HOOK_RED
    with pytest.raises(ValueError):
        D.rejoin_message("dance")


def test_storage_stuck_error():
    s = D.storage_message(350, 350)
    assert s["title"] == "Storage is full" and "350/350" in s["description"]
    k = D.stuck_message("Couldn't rejoin after 5 tries in a row.")
    assert k["title"] == "Needs your help" and "The run stopped." in k["description"]
    e = D.error_message("stopped on an error: ValueError: x")
    assert e["title"] == "Stopped by an error" and "ValueError" in e["description"]
    assert D.error_message("", stopped=False)["title"] == "Something went wrong"


def test_messages_never_leak(monkeypatch):
    monkeypatch.setenv("USERNAME", "TestUserXyz")
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-FAKE01")
    bad = (f"{URL} {SHARE} {PRIVATE} {DEEP} C:\\Users\\TestUserXyz\\AppData\\x.log "
           f"on DESKTOP-FAKE01")
    embeds = [D.rare_message(bad, 5, "normal", bad, "banner"),
              D.rare_message(bad, None, "chromatic", bad, "log_chromatic"),
              D.rejoin_message("start", why=bad), D.rejoin_message("fail", why=bad),
              D.rejoin_message("gave_up", why=bad), D.stuck_message(bad),
              D.error_message(bad), D.storage_message(1, 2), D.sample_message()]
    blob = json.dumps(embeds)
    for secret in (TOKEN, HOOK_ID, "webhooks", "4f2a9c0d1e7b44c1a5d2e9f0b3c6d8aa",
                   "55512345678901234567890123456789", "TestUserXyz", "DESKTOP-FAKE01",
                   "AppData", "C:\\\\"):
        assert secret.lower() not in blob.lower(), secret
    for e in embeds:
        assert e["footer"]["text"].startswith("MayhemMacro ")


def test_whole_alert_through_the_sender(sending):
    """What the app will do for one rare banner, end to end (fake transport)."""
    c = cfg()
    s, ft, sleeps, lines = sender([204])
    assert D.should_send(c, "rare")
    embed = D.rare_message("Resonance", 100000, "normal", "Dreamland", "banner", run_no=1)
    s.enqueue(D.make_event(embed, D.mention_for(c, "rare", ping=False), FAKE_PNG),
              D.priority_for("rare"))
    assert s.pump() == ["ok"]
    p = ft.payloads()[0]
    assert p["username"] == "MayhemMacro" and p["content"] == ""
    assert p["allowed_mentions"] == {"parse": []}
    assert p["embeds"][0]["title"].endswith("Resonance spawned!")
    assert p["embeds"][0]["image"] == {"url": "attachment://picture.png"}


def test_rarity_tiers_and_collected_alerts():
    assert D.tier(250_000)[2] == "Rare" and D.tier(1_000_000)[2] == "Epic"
    assert D.tier(10_000_000)[2] == "Legendary" and D.tier(500_000_000)[2] == "Mythic"
    assert D.tier(3_000_000_000)[2] == "Godly" and D.tier(None)[2] == "Rare"
    e = D.rare_message("Beyond", 10_000_000, "normal", "Overworld", "banner")
    assert e["title"].startswith("\u2B50") and e["color"] == D.tier(10_000_000)[1]
    c = D.collected_message("Evergreen", 1_000_000, "inverted", "pickup", run_no=2)
    assert c["title"].endswith("Collected Inverted Evergreen!") and "worker" in c["description"]
    f = {x["name"]: x["value"] for x in c["fields"]}
    assert f == {"Rarity": "**1 in 1,000,000**", "Tier": "\U0001F52E Epic", "Variant": "\u26AA Inverted"}
