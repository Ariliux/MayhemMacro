"""The app's logic without a window: what each engine event means
(plan_event), which Discord alerts go out (alerts_for - the ONE place the
user's switches are applied), queueing them (dispatch) and the run's
bookkeeping (Session: counters, stats.json, the sender).

No Tk window is made here, nothing is sent (senders are fakes, and a
tripwire fails any test that would build a real one or reach the network),
and stats.json only ever lives in pytest's tmp_path.
"""
import copy
import json
import os
import subprocess
import sys
import urllib.request

import pytest

import mayhemmacro as M
from mayhem import APP_NAME, MAPS, discord as D, paths, safety

HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "T0k3n_" * 11
TOKEN = HOOK.rsplit("/", 1)[1]
UID = "987654321098765432"


class FakeSender:
    """Records what would have been posted."""
    made: list = []

    def __init__(self, url):
        self.url = url
        self.events: list = []
        self.dead = False
        self.discarded = 0
        self.flushed = None
        FakeSender.made.append(self)

    def enqueue(self, event, priority=D.PRI_NORMAL):
        if self.dead:
            return False
        self.events.append((event, priority))
        return True

    def _discard(self):
        self.discarded += 1

    def flush(self, timeout=2.0):
        self.flushed = timeout
        return True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A real WebhookSender or any urlopen fails the test on the spot."""
    calls = []

    def trip(*a, **k):
        calls.append(a)
        raise AssertionError("tried to reach the network")

    class NoRealSender:
        def __init__(self, *a, **k):
            calls.append(a)
            raise AssertionError("built a real WebhookSender")

    monkeypatch.setattr(urllib.request, "urlopen", trip)
    monkeypatch.setattr(D, "WebhookSender", NoRealSender)
    FakeSender.made = []
    yield
    assert calls == []


def settings(**over) -> dict:
    """Defaults with Discord on, a (made-up) link, and an id to @mention."""
    s = copy.deepcopy(paths.DEFAULT_SETTINGS)
    s["discord"].update(enabled=True, webhook=HOOK, user_id=UID)
    for k, v in over.items():
        if k == "send":
            s["discord"]["send"].update(v)
        elif k == "mention":
            s["discord"]["mention"].update(v)
        else:
            s[k] = v
    return s


LIVE = M.RunCtx(live=True, run_no=7, world="Dreamland", ran=125.0,
                counts={"rares": 2, "votes": 3, "afk": 4, "rejoins": 1},
                lifetime=7200.0)


def rare(den=100_000, variant="normal", source="banner", block="Resonance",
         ping=False, picture=None):
    return {"type": "rare", "block": block, "denominator": den, "variant": variant,
            "source": source, "ping": ping, "picture": picture}


# ------------------------------------------------------------- helpers ---
def test_rarity_text_and_back():
    assert M.rarity_text(100000) == "1 in 100,000"
    assert M.rarity_text("505000") == "1 in 505,000"
    assert M.rarity_text(None) == "" and M.rarity_text(1) == "" and M.rarity_text("x") == ""
    for n in M.RARE_STEPS:
        assert M.rarity_from_text(M.rarity_text(n)) == n
    assert M.rarity_from_text("") is None


def test_rarity_choices_keep_a_hand_set_value():
    assert M.rarity_choices(100_000) == [M.rarity_text(n) for n in M.RARE_STEPS]
    odd = M.rarity_choices(505_000)
    assert "1 in 505,000" in odd and len(odd) == len(M.RARE_STEPS) + 1
    assert odd == sorted(odd, key=M.rarity_from_text)
    assert M.rarity_choices("junk") == [M.rarity_text(n) for n in M.RARE_STEPS]


def test_clock_and_vote_countdown():
    assert M.clock_text(511) == "08:31"
    assert M.clock_text(0) == "00:00"
    assert M.clock_text(None) == "—"
    assert M.vote_left(511, 100.0, 112.0) == 499
    # passed: rolls on to the next 10-minute vote
    assert M.vote_left(10, 100.0, 130.0) == 580
    assert M.vote_left(None, 100.0, 130.0) is None
    assert M.vote_left(10, None, 130.0) is None


@pytest.mark.parametrize("raw, want", [(10, 10), (1, 2), (99, 15), ("7", 7), (7.6, 8),
                                       (None, 10), ("abc", 10), (2, 2), (15, 15)])
def test_clamp_minutes(raw, want):
    assert M.clamp_minutes(raw) == want


def test_ranking_tick_untick_and_move():
    r = M.rank_toggle([], "Dreamland", True)
    r = M.rank_toggle(r, "Arctic", True)
    r = M.rank_toggle(r, "Lava", True)
    assert r == ["Dreamland", "Arctic", "Lava"]          # ticking adds to the bottom
    assert M.rank_toggle(r, "Arctic", False) == ["Dreamland", "Lava"]
    assert M.rank_toggle(r, "Dreamland", True) == ["Arctic", "Lava", "Dreamland"]
    assert M.rank_toggle(r, "Narnia", True) == r          # not a map
    assert M.rank_move(r, "Lava", -1) == ["Dreamland", "Lava", "Arctic"]
    assert M.rank_move(r, "Dreamland", -1) == r            # already first
    assert M.rank_move(r, "Lava", 1) == r                  # already last
    assert M.rank_move(r, "Void", 1) == r                  # not ranked
    assert r == ["Dreamland", "Arctic", "Lava"]            # inputs never changed


def test_clean_ranking():
    assert M.clean_ranking(["dreamland", "DREAMLAND", "Narnia", 5, "arctic"]) == \
        ["Dreamland", "Arctic"]
    assert M.clean_ranking("Dreamland") == []
    assert M.clean_ranking(list(MAPS)) == list(MAPS)


def test_block_label():
    assert M.block_label("snakeblock") == "SNAKEBLOCK"
    assert M.block_label("sporedelic") == "Sporedelic"
    assert M.block_label("Relic", "inverted") == "Inverted Relic"
    assert M.block_label("vexen", "chromatic") == "Chromatic Vexen"
    assert M.block_label("Chromatic Vexen", "chromatic") == "Chromatic Vexen"
    assert M.block_label("nil", "chromatic") == ""
    assert M.block_label(None) == ""


def test_rare_thresholds():
    s = settings(rare_min=100_000, rare_ping_min=1_000_000)
    assert M.rare_passes(100_000, "normal", s)            # exactly counts
    assert not M.rare_passes(50_000, "normal", s)
    assert M.rare_passes(50_000, "inverted", s)           # variants always...
    assert not M.rare_passes(50_000, "inverted", settings(alert_variants_always=False))
    assert M.rare_passes(None, "normal", s)               # no number: always
    assert M.rare_wants_ping(1_000_000, False, s)
    assert not M.rare_wants_ping(505_000, False, s)
    assert M.rare_wants_ping(505_000, True, s)            # the engine flagged it
    assert not M.rare_wants_ping(None, False, s)


# ------------------------------------------------- alerts_for: the gates ---
def test_nothing_is_sent_unless_a_real_run_is_live():
    s = settings(send={"hourly": True})
    idle = M.RunCtx(live=False)
    for ev in (rare(), {"type": "storage_full", "n": 350, "m": 350},
               {"type": "rejoin", "phase": "gave_up", "try": 5},
               {"type": "run", "kind": "start"}, {"type": "run", "kind": "hourly", "hours": 1}):
        assert M.alerts_for(ev, s, idle) == []
        assert M.alerts_for(ev, s, LIVE) != []


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"webhook": ""}, {"webhook": "https://example.com/hook"},
    {"enabled": "yes"}])
def test_no_alert_without_discord_set_up(change):
    s = settings()
    s["discord"].update(change)
    assert M.alerts_for(rare(), s, LIVE) == []
    assert M.alerts_for({"type": "run", "kind": "start"}, s, LIVE) == []


def test_each_alert_follows_its_own_tick():
    for group, ev in (("rare", rare()),
                      ("storage", {"type": "storage_full", "n": 350, "m": 350}),
                      ("rejoin", {"type": "rejoin", "phase": "ok", "try": 1}),
                      ("stuck", {"type": "rejoin", "phase": "gave_up", "try": 5}),
                      ("run", {"type": "run", "kind": "start"})):
        on = M.alerts_for(ev, settings(send={group: True}), LIVE)
        assert [a.group for a in on] == [group]
        assert M.alerts_for(ev, settings(send={group: False}), LIVE) == []


def test_rare_alert_content_and_switches():
    s = settings()
    [a] = M.alerts_for(rare(), s, LIVE)
    assert a.group == "rare" and a.priority == D.PRI_NORMAL
    assert a.embed["title"].endswith("Resonance spawned!")
    fields = {f["name"]: f["value"] for f in a.embed["fields"]}
    assert fields == {"Rarity": "**1 in 100,000**", "Tier": "\U0001F48E Rare", "World": "Dreamland"}
    assert a.embed["footer"]["text"].endswith("run #7")
    assert a.mention is None                               # 1/100,000 < @ me floor
    assert a.picture is None
    assert M.alerts_for(rare(), settings(rare_alerts=False), LIVE) == []
    assert M.alerts_for(rare(den=10_000), s, LIVE) == []   # under 'rarer than'
    assert len(M.alerts_for(rare(den=10_000), settings(rare_min=10_000), LIVE)) == 1


def test_rare_ping_needs_rarity_tick_and_id():
    big = rare(den=1_000_000, block="Voidrinyx")
    assert M.alerts_for(big, settings(), LIVE)[0].mention == UID
    assert M.alerts_for(rare(ping=True), settings(), LIVE)[0].mention == UID
    assert M.alerts_for(big, settings(mention={"rare": False}), LIVE)[0].mention is None
    no_id = settings()
    no_id["discord"]["user_id"] = "12345"                  # not a real id
    assert M.alerts_for(big, no_id, LIVE)[0].mention is None
    assert M.alerts_for(big, settings(rare_ping_min=10_000_000), LIVE)[0].mention is None


def test_rare_variants_and_log_sources():
    inv = M.alerts_for(rare(den=5_000, variant="inverted", block="Relic"), settings(), LIVE)
    assert inv[0].embed["title"].endswith("Inverted Relic spawned!")
    assert M.alerts_for(rare(den=5_000, variant="inverted", block="Relic"),
                        settings(alert_variants_always=False), LIVE) == []
    song = M.alerts_for(rare(den=None, source="log_song", block="snakeblock"), settings(), LIVE)
    assert song[0].embed["title"].endswith("\U0001F48E SNAKEBLOCK appeared!")
    late = M.alerts_for(rare(den=None, source="log_chromatic", block="vexen"), settings(), LIVE)
    assert late[0].embed["title"].endswith("A Chromatic Vexen was on your server")
    unnamed = M.alerts_for(rare(den=None, source="log_chromatic", block="nil"), settings(), LIVE)
    assert "nil" not in json.dumps(unnamed[0].embed).lower()


def test_picture_only_when_switched_on():
    pic = object()
    assert M.alerts_for(rare(picture=pic), settings(), LIVE)[0].picture is None
    assert M.alerts_for(rare(picture=pic), settings(attach_picture=True), LIVE)[0].picture is pic


def test_storage_alert_follows_the_alerts_page_switch():
    ev = {"type": "storage_full", "n": 350, "m": 350}
    [a] = M.alerts_for(ev, settings(), LIVE)
    assert a.group == "storage" and "350/350" in a.embed["description"]
    assert M.alerts_for(ev, settings(storage_alert=False), LIVE) == []


def test_rejoin_alerts():
    s = settings()
    start = M.alerts_for({"type": "rejoin", "phase": "start", "try": 2, "why": "Kicked."}, s, LIVE)
    assert start[0].group == "rejoin" and start[0].mention is None
    assert start[0].priority == D.PRI_MILESTONE
    assert start[0].embed["title"] == "Rejoining a public server"     # no link saved
    s2 = settings(private_server_link="https://www.roblox.com/share?code=abcdef123456&type=Server")
    start2 = M.alerts_for({"type": "rejoin", "phase": "start", "try": 1}, s2, LIVE)
    assert start2[0].embed["title"] == "Rejoining your server"
    stuck = M.alerts_for({"type": "rejoin", "phase": "gave_up", "try": 5}, s, LIVE)
    assert stuck[0].group == "stuck" and stuck[0].mention == UID
    assert stuck[0].embed["title"] == "Needs your help"
    assert M.alerts_for({"type": "rejoin", "phase": "gave_up", "try": 5},
                        settings(mention={"stuck": False}), LIVE)[0].mention is None
    assert M.alerts_for({"type": "rejoin", "phase": "huh"}, s, LIVE) == []


def test_run_alerts_start_stop_error_hourly():
    s = settings()
    [a] = M.alerts_for({"type": "run", "kind": "start"}, s, LIVE)
    assert a.group == "run" and a.embed["title"] == "Run started"
    [b] = M.alerts_for({"type": "run", "kind": "stop", "ran": 3725, "reason": "stopped"}, s, LIVE)
    fields = {f["name"]: f["value"] for f in b.embed["fields"]}
    assert b.embed["title"] == "Run stopped" and fields["Ran"] == "1h 02m"
    assert fields["Rares"] == "2" and fields["AFK saves"] == "4"
    [c] = M.alerts_for({"type": "run", "kind": "stop", "reason": "error",
                        "detail": "it broke"}, s, LIVE)
    assert c.group == "error" and c.mention == UID and "it broke" in c.embed["description"]
    # error alerts off: the ordinary stop message still goes
    [d] = M.alerts_for({"type": "run", "kind": "stop", "reason": "error"},
                       settings(send={"error": False}), LIVE)
    assert d.group == "run"
    [e] = M.alerts_for({"type": "run", "kind": "closed", "ran": 60}, s, LIVE)
    assert e.embed["title"] == f"{APP_NAME} was closed"
    assert M.alerts_for({"type": "run", "kind": "hourly", "hours": 2}, s, LIVE) == []  # off by default
    [h] = M.alerts_for({"type": "run", "kind": "hourly", "hours": 2, "ran": 7300},
                       settings(send={"hourly": True}), LIVE)
    assert h.priority == D.PRI_PERIODIC and h.embed["title"].startswith("2h")


def test_no_secret_reaches_an_embed():
    """The webhook token, the server link's code and the PC's names never
    show up in what would be posted."""
    link = "https://www.roblox.com/share?code=SECRETCODE123456&type=Server"
    s = settings(private_server_link=link, send={"hourly": True})
    nasty = f"{HOOK} {link} roblox://placeID=1&linkCode=SECRETCODE123456 C:\\Users\\x\\a.txt"
    evs = [rare(block=nasty), {"type": "rejoin", "phase": "start", "why": nasty},
           {"type": "rejoin", "phase": "gave_up", "why": nasty},
           {"type": "run", "kind": "stop", "reason": "error", "detail": nasty}]
    for ev in evs:
        for a in M.alerts_for(ev, s, LIVE):
            text = json.dumps(a.embed)
            assert TOKEN not in text and "SECRETCODE123456" not in text
            assert "C:\\\\Users" not in text


def test_alerts_for_does_not_change_its_inputs():
    s, ev = settings(), rare(den=1_000_000)
    s0, ev0 = copy.deepcopy(s), copy.deepcopy(ev)
    M.alerts_for(ev, s, LIVE)
    M.plan_event(ev, s, LIVE)
    assert s == s0 and ev == ev0


# ------------------------------------------------- plan_event: the rest ---
def test_plan_for_each_engine_event_kind():
    s = settings()
    p = M.plan_event(rare(), s, LIVE)
    assert [r.key for r in p.feed] == ["rare"] and p.tiles == ["rares"] and p.stats == ["rares"]
    assert p.toast == ("Resonance spawned! · 1 in 100,000", "rare")
    assert p.rare["name"] == "Resonance" and p.rare["odds"] == "1 in 100,000"

    p = M.plan_event(rare(den=10_000), s, LIVE)            # too common to alert
    assert p.toast is None and p.alerts == [] and p.feed and p.tiles == ["rares"]

    p = M.plan_event({"type": "vote", "options": ["Void", "Lava"], "chose": "Lava", "ok": True}, s, LIVE)
    assert p.feed[0].title == "Voted for Lava" and p.tiles == ["votes"] and p.stats == ["votes"]
    p = M.plan_event({"type": "vote", "options": ["Void"], "chose": "Void", "ok": False}, s, LIVE)
    assert p.feed[0].key == "votefail" and p.tiles == [] and p.log == []

    p = M.plan_event({"type": "anti_afk", "idle": 612.0, "how": "jump"}, s, LIVE)
    assert p.tiles == ["afk"] and p.stats == ["anti_afk"] and "10m" in p.feed[0].sub
    p = M.plan_event({"type": "anti_afk", "idle": 900, "how": "front_jump"}, s, LIVE)
    assert "forward" in p.feed[0].sub

    p = M.plan_event({"type": "rejoin", "phase": "ok", "try": 3}, s, LIVE)
    assert p.tiles == ["rejoins"] and p.stats == ["rejoins"] and "3 tries" in p.feed[0].sub
    p = M.plan_event({"type": "rejoin", "phase": "fail", "try": 2}, s, LIVE)
    assert p.stats == ["rejoin_fails"] and p.tiles == []
    p = M.plan_event({"type": "rejoin", "phase": "gave_up", "try": 5, "why": "no luck"}, s, LIVE)
    assert p.end == ("stuck", "no luck") and p.toast[1] == "err"

    p = M.plan_event({"type": "storage_full", "n": 350, "m": 350}, s, LIVE)
    assert p.stats == ["storage_full"] and p.toast == ("Storage is full · 350/350", "warn")
    p = M.plan_event({"type": "storage_full", "n": 350, "m": 350},
                     settings(storage_alert=False), LIVE)
    assert p.toast is None and p.alerts == [] and p.feed


def test_plan_state_and_log():
    s = settings()
    p = M.plan_event({"type": "state", "state": "paused", "why": ""}, s, LIVE)
    assert p.mode == "paused" and p.state[0] == "Paused" and p.state[2] == "AMBER"
    assert p.feed[0].key == "pause"
    p = M.plan_event({"type": "state", "state": "running", "why": "In your server."}, s, LIVE)
    assert p.state == ("Running", "In your server.", "GREEN") and not p.feed
    assert M.plan_event({"type": "state", "state": "stopped"}, s, LIVE).mode == "stopped"
    assert M.plan_event({"type": "state", "state": "dancing"}, s, LIVE).mode is None
    p = M.plan_event({"type": "log", "level": "info", "text": "hello"}, s, LIVE)
    assert p.log == [("info", "hello")] and not p.feed
    p = M.plan_event({"type": "log", "level": "warn", "text": "behind other windows"}, s, LIVE)
    assert p.feed[0].key == "warn" and p.error is None
    p = M.plan_event({"type": "log", "level": "error", "text": "boom"}, s, LIVE)
    assert p.feed[0].key == "err" and p.error == "boom" and p.end is None
    assert M.plan_event({"type": "log", "level": "shout", "text": "x"}, s, LIVE).log == [("info", "x")]


def test_plan_hud_and_world_changes():
    s = settings()
    p = M.plan_event({"type": "hud", "timer": 511, "storage": [215, 350], "world": "Dreamland"},
                     s, LIVE)
    assert p.hud == {"timer": 511, "storage": (215, 350), "world": "Dreamland"}
    assert p.world is None and not p.feed                  # same world as before
    p = M.plan_event({"type": "hud", "timer": None, "storage": None, "world": "Arctic"}, s, LIVE)
    assert p.world == "Arctic" and p.feed[0].title == "Now in Arctic"
    first = M.RunCtx(live=True, world=None)
    p = M.plan_event({"type": "hud", "world": "Lava"}, s, first)
    assert p.world == "Lava" and not p.feed                # the first reading isn't a change
    bad = M.plan_event({"type": "hud", "timer": "08:31", "storage": [1, "x"], "world": 5}, s, LIVE)
    assert bad.hud == {"timer": None, "storage": None, "world": None}


@pytest.mark.parametrize("ev", [None, 5, "rare", {}, {"type": "nope"}, {"type": "rare"},
                                {"type": "vote"}, {"type": "rejoin"}, {"type": "anti_afk",
                                                                       "idle": "lots"},
                                {"type": "storage_full"}, {"type": "run", "kind": "?"}])
def test_odd_events_never_raise(ev):
    p = M.plan_event(ev, settings(), LIVE)
    assert isinstance(p, M.Plan)


def test_run_plans():
    s = settings()
    p = M.plan_event({"type": "run", "kind": "stop", "ran": 90, "reason": "error"}, s, LIVE)
    assert p.stats == ["errors"] and p.feed[0].title == "Stopped on an error"
    assert p.feed[0].colour == "RED"
    p = M.plan_event({"type": "run", "kind": "stop", "ran": 90, "reason": "stuck"}, s, LIVE)
    assert p.feed[0].title == "Stopped: couldn't rejoin" and p.stats == []
    p = M.plan_event({"type": "run", "kind": "hourly", "hours": 3}, s, LIVE)
    assert p.toast == ("3h in Block Mayhem X · still going", "ok")


def test_feed_colours_are_real_constants():
    seen = set()
    for ev in (rare(), rare(variant="inverted"), rare(source="log_chromatic", den=None),
               {"type": "vote", "chose": "Void", "ok": True}, {"type": "vote", "ok": False},
               {"type": "anti_afk", "idle": 1}, {"type": "storage_full", "n": 1, "m": 1},
               {"type": "log", "level": "warn", "text": "w"},
               {"type": "log", "level": "error", "text": "e"},
               {"type": "state", "state": "paused"},
               {"type": "hud", "world": "Lava"},
               {"type": "run", "kind": "start"}, {"type": "run", "kind": "stop"},
               {"type": "run", "kind": "hourly"},
               *({"type": "rejoin", "phase": ph} for ph in ("start", "ok", "fail", "gave_up"))):
        for row in M.plan_event(ev, settings(), LIVE).feed:
            seen.add(row.colour)
            assert M._colour(row.colour).startswith("#") and M._colour(row.colour) != "", row
    assert {"RARE", "INVERT", "CHROMA", "GREEN", "AMBER", "RED", "ACCENT", "SUBTLE"} <= seen
    assert M._colour("NOT_A_COLOUR") == M.SUBTLE and M._colour("APP_NAME") == M.SUBTLE


def test_every_feed_key_has_a_filter_home():
    keys = {"rare", "vote", "votefail", "world", "err", "warn", "pause", "rejoin",
            "rejoinfail", "stuck", "storage"}
    filtered = set().union(*(v for v in M.MayhemMacro.FEED_FILTERS.values() if v))
    assert keys <= filtered


# ----------------------------------------------------------- dispatch ---
def test_dispatch_queues_on_the_saved_link_with_priority():
    s = settings(send={"hourly": True})
    alerts = M.alerts_for(rare(den=1_000_000), s, LIVE) + \
        M.alerts_for({"type": "run", "kind": "hourly", "hours": 1}, s, LIVE)
    made = {}

    def sender_for(url):
        return made.setdefault(url, FakeSender(url))
    assert M.dispatch(alerts, s, sender_for) == 2
    [sender] = made.values()
    assert sender.url == D.normalize_hook(HOOK)
    (ev1, p1), (ev2, p2) = sender.events
    assert set(ev1) == {"embed", "mention", "picture"} and ev1["mention"] == UID
    assert p1 == D.PRI_NORMAL and p2 == D.PRI_PERIODIC


def test_dispatch_without_a_link_or_with_a_broken_sender():
    alerts = M.alerts_for(rare(), settings(), LIVE)
    s = settings()
    s["discord"]["webhook"] = ""
    assert M.dispatch(alerts, s, lambda url: pytest.fail("no sender without a link")) == 0
    assert M.dispatch([], settings(), lambda url: pytest.fail("nothing to send")) == 0

    class Broken(FakeSender):
        def enqueue(self, *a, **k):
            raise RuntimeError("queue on fire")
    assert M.dispatch(alerts, settings(), Broken) == 0     # swallowed, never raises


# ------------------------------------------------------------ Session ---
@pytest.fixture
def stats_file(tmp_path):
    return str(tmp_path / "stats.json")


def make_session(s, stats_file):
    box = {"settings": s}
    sess = M.Session(lambda: box["settings"], make_sender=FakeSender, stats_path=stats_file)
    return sess, box


def saved(stats_file):
    with open(stats_file, encoding="utf-8") as fh:
        return json.load(fh)


def test_session_does_nothing_lasting_before_a_real_start(stats_file):
    sess, _ = make_session(settings(), stats_file)
    p = sess.handle(rare(den=1_000_000), 100.0)
    assert p.feed and sess.counts["rares"] == 1        # the window may show it
    assert p.alerts == [] and FakeSender.made == []     # but nothing is sent
    assert sess.stats["rares"] == 0 and not os.path.exists(stats_file)
    assert sess.tick(200.0) is None and sess.end(300.0) is None
    assert not os.path.exists(stats_file)


def test_session_full_run(stats_file):
    sess, _ = make_session(settings(send={"hourly": True}), stats_file)
    t0 = 1_000_000.0
    p = sess.begin(t0)
    assert sess.live and sess.run_no == 1 and p.feed[0].key == "start"
    on_disk = saved(stats_file)
    assert on_disk["runs"] == 1 and on_disk["current"]["secs"] == 0
    [sender] = FakeSender.made
    assert [e["embed"]["title"] for e, _ in sender.events] == ["Run started"]

    for ev in (rare(), rare(den=None, source="log_song", block="snakeblock"),
               {"type": "vote", "options": ["Lava"], "chose": "Lava", "ok": True},
               {"type": "anti_afk", "idle": 610, "how": "jump"},
               {"type": "rejoin", "phase": "fail", "try": 1},
               {"type": "rejoin", "phase": "ok", "try": 2},
               {"type": "storage_full", "n": 350, "m": 350},
               {"type": "hud", "world": "Arctic"}):
        sess.handle(ev, t0 + 10)
    assert sess.counts == {"rares": 2, "votes": 1, "afk": 1, "rejoins": 1}
    st = sess.stats
    assert (st["rares"], st["votes"], st["anti_afk"], st["rejoins"], st["rejoin_fails"],
            st["storage_full"]) == (2, 1, 1, 1, 1, 1)
    assert sess.world == "Arctic"

    # an hour in: one hourly alert, then not again in the same hour
    t = t0
    hourly = []
    while t < t0 + 3600 + 3:
        t += 1.0
        p = sess.tick(t)
        if p is not None:
            hourly.append(p)
    assert len(hourly) == 1 and hourly[0].toast[0].startswith("1h")
    assert 3590 <= sess.stats["total_secs"] <= 3610     # 1 s steps, never more than 5
    p = sess.end(t + 1, ran=t + 1 - t0)
    assert not sess.live and p.feed[0].title == "Stopped"
    titles = [e["embed"]["title"] for e, _ in sender.events]
    assert titles[-2:] == ["1h in Block Mayhem X", "Run stopped"]
    on_disk = saved(stats_file)
    assert on_disk["current"] is None and on_disk["longest_secs"] >= 3590
    assert on_disk["rares"] == 2 and on_disk["runs"] == 1
    assert sess.end(t + 5) is None                       # only once


def test_session_time_steps_are_clamped(stats_file):
    sess, _ = make_session(settings(), stats_file)
    sess.begin(1000.0)
    sess.tick(1000.0 + 7200)                             # the clock jumped 2 h
    assert sess.stats["total_secs"] <= 5.0


def test_session_stuck_and_error_ends(stats_file):
    sess, _ = make_session(settings(), stats_file)
    sess.begin(10.0)
    sess.handle({"type": "rejoin", "phase": "gave_up", "try": 5, "why": "no luck"}, 20.0)
    sess.engine_stopped("stopped", 21.0)
    assert sess.end_reason == "stuck"                    # keeps its own reason
    sess.end(22.0)
    [sender] = FakeSender.made
    groups = [e["embed"]["title"] for e, _ in sender.events]
    assert groups == ["Run started", "Needs your help", "Run stopped"]

    sess.begin(100.0)
    assert sess.end_reason == "stopped" and sess.last_error is None
    # the engine's errors say "the macro keeps going": a line alone ends nothing
    sess.handle({"type": "log", "level": "error", "text": "the screen reader crashed"}, 101.0)
    assert sess.end_reason == "stopped"
    # ...but if the worker then DIES, that line is the best reason there is
    sess.engine_stopped("The run stopped unexpectedly.", 105.0, crashed=True)
    assert (sess.end_reason, sess.end_detail) == ("error", "the screen reader crashed")
    p = sess.end(106.0)
    assert p.alerts[0].group == "error" and p.alerts[0].mention == UID
    assert sess.stats["errors"] == 1


def test_session_engine_stopping_itself_is_not_an_error(stats_file):
    sess, _ = make_session(settings(), stats_file)
    sess.begin(10.0)
    sess.handle({"type": "log", "level": "error", "text": "a hiccup"}, 11.0)
    sess.engine_stopped("Roblox closed and rejoin is off.", 12.0)
    assert sess.end_reason == "stopped" and "rejoin is off" in sess.end_detail
    sess.engine_stopped("x", 700.0, crashed=True)          # the error is old by now
    assert (sess.end_reason, sess.end_detail) == ("error", "x")


def test_engine_warning_after_its_own_event_is_not_a_second_row(stats_file):
    """The engine sends storage_full, then 'Storage is full (350/350)...' as
    a warning; a failed rejoin or vote the same. One feed row each."""
    sess, _ = make_session(settings(), stats_file)
    sess.begin(10.0)
    p1 = sess.handle({"type": "storage_full", "n": 350, "m": 350}, 20.0)
    p2 = sess.handle({"type": "log", "level": "warn",
                      "text": "Storage is full (350/350). New blocks won't fit."}, 20.01)
    assert [r.key for r in p1.feed] == ["storage"] and p2.feed == []
    assert p2.log == [("warn", "Storage is full (350/350). New blocks won't fit.")]
    sess.handle({"type": "rejoin", "phase": "gave_up", "try": 5, "why": "stuck"}, 30.0)
    p3 = sess.handle({"type": "log", "level": "error", "text": "stuck. The run stopped."}, 30.0)
    assert p3.feed == [] and p3.error
    # a warning on its own (or a while later) is a row
    p4 = sess.handle({"type": "log", "level": "warn", "text": "Roblox is behind"}, 40.0)
    assert [r.key for r in p4.feed] == ["warn"]
    sess.handle({"type": "vote", "options": ["Lava"], "chose": "Lava", "ok": True}, 50.0)
    p5 = sess.handle({"type": "log", "level": "warn", "text": "may not have counted"}, 50.0)
    assert [r.key for r in p5.feed] == ["warn"]          # a good vote's warning is news


def test_engine_state_texts_read_as_sentences():
    s = settings()
    p = M.plan_event({"type": "state", "state": "running", "why": "Starting"}, s, LIVE)
    assert p.state == ("Starting", "Looking for Block Mayhem X…", "GREEN")
    p = M.plan_event({"type": "state", "state": "waiting", "why": "Roblox isn't open"}, s, LIVE)
    assert p.state == ("Waiting", "Roblox isn't open.", "ACCENT")
    p = M.plan_event({"type": "state", "state": "paused",
                      "why": "Roblox is behind other windows"}, s, LIVE)
    assert p.state[1] == "Roblox is behind other windows." and p.feed[0].key == "pause"


def test_rare_alert_world_comes_from_the_event():
    [a] = M.alerts_for(dict(rare(), world="Lava"), settings(), LIVE)
    assert {f["name"]: f["value"] for f in a.embed["fields"]}["World"] == "Lava"
    [b] = M.alerts_for(dict(rare(), world=None), settings(), LIVE)    # unsure since the vote
    assert "World" not in {f["name"] for f in b.embed["fields"]}


def test_session_sender_follows_the_link(stats_file):
    sess, box = make_session(settings(), stats_file)
    sess.begin(10.0)
    first = sess.sender
    other = HOOK.replace("123456789012345678", "111111111111111111")
    box["settings"] = settings()
    box["settings"]["discord"]["webhook"] = other
    sess.handle(rare(), 11.0)
    assert sess.sender is not first and sess.sender.url == other
    assert first.dead and first.discarded == 1           # nothing goes to the old link
    sess.kill_sender()
    assert sess.sender is None
    sess.flush_sender()                                  # no sender: no error


def test_session_begin_closes_a_lingering_run(stats_file):
    sess, _ = make_session(settings(), stats_file)
    sess.begin(10.0)
    sess.begin(20.0)
    assert sess.run_no == 2 and saved(stats_file)["runs"] == 2
    titles = [e["embed"]["title"] for e, _ in FakeSender.made[0].events]
    assert titles == ["Run started", "Run stopped", "Run started"]


def test_session_survives_a_broken_stats_folder(tmp_path):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")
    sess = M.Session(lambda: settings(), make_sender=FakeSender,
                     stats_path=str(blocker / "stats.json"))
    sess.begin(1.0)                                      # can't save: still runs
    sess.handle(rare(), 2.0)
    assert sess.end(3.0) is not None


# ----------------------------------------- the window's wiring, no window ---
class StandIn:
    """Just enough of the app for its event methods, without Tk."""

    def __init__(self, stats_file):
        import queue
        self._run_seq = 3
        self.running = True
        self._events = queue.Queue()
        self.logged, self.applied, self.stopped = [], [], []
        self.session = M.Session(lambda: settings(), make_sender=FakeSender,
                                 stats_path=stats_file)
        self.session.begin(1000.0)

    def log(self, text, level="info"):
        self.logged.append((level, text))

    def _apply_plan(self, plan):
        self.applied.append(plan)

    def _stop_run(self, by_engine=False, why=""):
        self.stopped.append((by_engine, why))
        self.running = False

    def _on_event(self, seq, ev):
        return M.MayhemMacro._on_event(self, seq, ev)

    def _drain_events(self, limit=1000):
        return M.MayhemMacro._drain_events(self, limit)

    def _end_session(self, *a):
        return M.MayhemMacro._end_session(self, *a)


def test_events_of_this_run_are_planned_and_drawn(stats_file):
    app = StandIn(stats_file)
    M.MayhemMacro._on_event(app, 3, rare())
    assert len(app.applied) == 1 and app.session.counts["rares"] == 1
    M.MayhemMacro._on_event(app, 3, {"type": "state", "state": "stopped", "why": "done"})
    assert app.stopped == [(True, "done")]


def test_a_stopped_runs_last_words_only_reach_the_log(stats_file):
    app = StandIn(stats_file)
    M.MayhemMacro._on_event(app, 2, rare())                       # an older run
    M.MayhemMacro._on_event(app, 2, {"type": "log", "level": "warn", "text": "bye"})
    M.MayhemMacro._on_event(app, 2, {"type": "state", "state": "stopped"})
    assert app.applied == [] and app.stopped == [] and app.session.counts["rares"] == 0
    assert app.logged == [("warn", "bye")]
    app.running = False                                           # stopped by the user
    M.MayhemMacro._on_event(app, 3, {"type": "state", "state": "stopped"})
    M.MayhemMacro._on_event(app, 3, "not a dict")
    assert app.applied == [] and app.stopped == []
    # A rare the engine was still holding goes out as it stops: it belongs
    # to this run, whose end alert isn't sent yet...
    M.MayhemMacro._on_event(app, 3, rare())
    assert len(app.applied) == 1 and app.session.counts["rares"] == 1
    # ...but nothing counts any more once the run has ended.
    app.session.end(2000.0)
    M.MayhemMacro._on_event(app, 3, rare())
    assert len(app.applied) == 1 and app.session.counts["rares"] == 1


class Closing(StandIn):
    """StandIn plus what _close() touches, after a normal Stop."""

    def __init__(self, stats_file):
        super().__init__(stats_file)
        self.running = False
        self._engine = None
        self._hotkey_handle = None
        self._win_save = self._save_job = None
        self.session_start = None
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def test_closing_right_after_stop_still_sends_the_stop_alert(stats_file):
    app = Closing(stats_file)
    sender = app.session.sender              # made for the run's start alert
    assert sender is not None
    app.session.end(1100.0)                  # Stop, 200 ms before the window closes
    assert not app.session.live and sender.flushed is None
    M.MayhemMacro._close(app)
    assert sender.flushed == 2.0             # the queued 'Run stopped' goes out first
    assert app.destroyed


def test_f2_right_after_f1_stops_the_run():
    """F2 goes straight to stop: toggle_run's one-second debounce (against a
    held key) must not swallow the F2 that undoes an F1 pressed by mistake."""
    class App:
        running = True
        _last_toggle = 10 ** 12              # an F1 "just now": toggle_run would refuse
        stops = 0

        def _ui(self, fn):
            fn()

        def _stop_run(self):
            self.stops += 1
            self.running = False

        def toggle_run(self):
            raise AssertionError("F2 must not go through the debounce")
    app = App()
    M.MayhemMacro._hotkey_stop(app)
    M.MayhemMacro._hotkey_stop(app)          # already stopped: nothing
    assert app.stops == 1


def test_the_end_alert_waits_for_the_engine_to_finish_stopping(stats_file):
    import threading
    app = StandIn(stats_file)
    app.running = False
    later = []
    app.after = lambda ms, fn: later.append(fn)
    gate = threading.Event()
    app._stopping = threading.Thread(target=gate.wait)
    app._stopping.start()
    M.MayhemMacro._end_session(app, 1100.0, 100.0, 3)
    assert app.session.live and len(later) == 1             # not yet: still stopping
    app._events.put((3, rare()))                             # the held rare, on its way out
    gate.set()
    app._stopping.join()
    later.pop()()
    assert not app.session.live
    assert app.session.counts["rares"] == 1                  # counted before the end


def test_emit_is_safe_from_another_thread(stats_file):
    import threading
    app = StandIn(stats_file)
    emit = M.MayhemMacro._emit_for(app, 7)
    threads = [threading.Thread(target=lambda i=i: emit({"type": "log", "text": str(i)}))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = [app._events.get_nowait() for _ in range(20)]
    assert {seq for seq, _ in got} == {7}
    assert sorted(int(ev["text"]) for _, ev in got) == list(range(20))


def test_engine_alive_reads_its_running_flag():
    class E:
        running = True
    assert M.MayhemMacro._engine_alive(E())
    E.running = False
    assert not M.MayhemMacro._engine_alive(E())
    assert M.MayhemMacro._engine_alive(object())      # no flag: assume alive

    import threading
    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()

    class Died:
        running = True                                # never got to say so
        _thread = done
    assert not M.MayhemMacro._engine_alive(Died())


def test_the_real_engine_takes_what_the_app_passes():
    """Engine(get_settings, emit, deps=None) - the app's factory calls it with
    two positional arguments; refused while testing before anything is built."""
    import inspect
    from mayhem import engine
    params = list(inspect.signature(engine.Engine.__init__).parameters)
    assert params[:3] == ["self", "get_settings", "emit"]
    for name in ("start", "stop", "tick"):
        assert callable(getattr(engine.Engine, name))


# ------------------------------------- the real engine's events, end to end ---
def _rig(**kw):
    """The engine builder's fake-wired Engine (tests/test_engine.py), its
    events stamped with its fake clock."""
    TE = pytest.importorskip("test_engine")
    r = TE.Rig(**kw)
    got: list[tuple[float, dict]] = []
    r.engine._emit_to = lambda ev: got.append((r.clock.t, ev))
    return TE, r, got


def _replay(got, stats_file, **over):
    sess, _ = make_session(settings(**over), stats_file)
    sess.begin(got[0][0] - 1 if got else 0.0)
    plans = [(ev, sess.handle(ev, t)) for t, ev in got]
    return sess, plans


def test_real_engine_run_reads_right_in_the_app(stats_file):
    TE, r, got = _rig(private_server_link=TE_PRIVATE())
    r.start()
    r.tick(2)
    r.ocr.script["storage"] = ["350/350"]
    r.run(62)                                           # full for a minute: one alert
    r.tail.feed(TE.SONG("snakeblock_song"))
    r.run(5)
    r.tail.feed(TE.DC(277))
    r.tick()
    r.tail.switch(TE.NEW_LOG.format(1))
    r.tail.feed(TE.JOIN(), TE.ENTERED)
    r.tick()
    r.tail.feed(TE.LOADED)
    r.tick()
    sess, plans = _replay(got, stats_file, private_server_link=TE_PRIVATE())
    feed = [row.key for _, p in plans for row in p.feed]
    assert feed.count("storage") == 1 and "warn" in feed     # 'Disconnected...' is news
    storage_warns = [row for _, p in plans for row in p.feed
                     if row.key == "warn" and "Storage is full" in row.title]
    assert storage_warns == []                          # said once, not twice
    titles = [row.title for _, p in plans for row in p.feed]
    assert "SNAKEBLOCK appeared" in titles and "Back in Block Mayhem X" in titles
    assert sess.counts["rares"] == 1 and sess.counts["rejoins"] == 1
    assert sess.stats["storage_full"] == 1 and sess.stats["rejoins"] == 1
    states = [p.state for _, p in plans if p.state]
    assert states[0] == ("Starting", "Looking for Block Mayhem X…", "GREEN")
    assert ("Running", "In Block Mayhem X.", "GREEN") in states
    sent = [e["embed"]["title"] for e, _ in FakeSender.made[0].events]
    assert {"Storage is full", "\U0001F48E SNAKEBLOCK appeared!", "Rejoining your server",
            "Back in Block Mayhem X"} <= set(sent)
    assert all(p.log == [] for ev, p in plans if ev["type"] != "log")


def test_real_engine_giving_up_ends_the_run_as_stuck(stats_file):
    TE, r, got = _rig(private_server_link=TE_PRIVATE())
    r.start()
    r.tail.feed(TE.DC(277))
    r.run_until(lambda: not r.engine.running, limit=3000)
    sess, plans = _replay(got, stats_file)
    assert plans[-1][1].mode == "stopped"
    assert sess.end_reason == "stuck"
    sess.engine_stopped(plans[-1][0]["why"], got[-1][0])
    p = sess.end(got[-1][0] + 1)
    assert p.feed[0].title == "Stopped: couldn't rejoin"
    sent = [e["embed"]["title"] for e, _ in FakeSender.made[0].events]
    assert sent.count("Needs your help") == 1 and sent[-1] == "Run stopped"
    assert sess.stats["rejoin_fails"] == 5
    errors = [row for _, pl in plans for row in pl.feed if row.key == "err"]
    assert errors == []                                 # the engine's 'gave up' line isn't a 2nd row


def TE_PRIVATE():
    import test_engine
    return test_engine.PRIVATE


# ------------------------------------------------- safety and identity ---
def test_side_effects_are_off_while_testing(tmp_path):
    assert safety.testing()
    assert M.keep_awake(True) is False and M.keep_awake(False) is False
    with pytest.raises(safety.SideEffectBlocked):
        M.default_engine(lambda: {}, lambda ev: None)
    # F8 is never hooked while testing (returns before touching keyboard)
    assert M.MayhemMacro._register_hotkey(object()) is None


def test_log_file_helpers(tmp_path):
    log = tmp_path / "logs" / "log.txt"
    assert M.write_log_line("hello", str(log))
    assert log.read_text(encoding="utf-8").endswith("  hello\n")
    assert not M.rotate_log(str(log))
    log.write_bytes(b"x" * 2000)
    assert M.rotate_log(str(log), limit=1000)
    assert (tmp_path / "logs" / "log.old.txt").exists() and not log.exists()
    assert M.write_log_line("x", str(tmp_path / "f" / "log.txt"))
    (tmp_path / "blocked").write_text("x")
    assert M.write_log_line("x", str(tmp_path / "blocked" / "log.txt")) is False


def test_single_instance_with_a_private_name():
    name = f"Local\\MayhemMacroTest.{os.getpid()}"
    assert M.claim_single_instance(name, focus=False) is True
    assert M.claim_single_instance(name, focus=False) is False
    assert M.SINGLE_INSTANCE_MUTEX == "Local\\MayhemMacro.SingleInstance"
    assert "GlassMacro" not in M.SINGLE_INSTANCE_MUTEX + M.APP_USER_MODEL_ID


def test_source_rules():
    with open(M.__file__, encoding="utf-8") as fh:
        src = fh.read()
    low = src.lower()
    for word in ("claude", "anthropic", "openai", "chatgpt", "github", "api.github"):
        assert word not in low, word
    assert "self.title(APP_NAME)" in src                 # FindWindowW finds it by title
    assert "Made by" in src and "Ariliux" in src


def test_every_page_has_both_icons_and_ten_pages():
    info = M.MayhemMacro.PAGE_INFO
    assert list(info) == ["home", "activity", "alerts", "vote", "server", "discord",
                          "stats", "log", "settings", "about"]
    for key, (title, sub, group, glyph, alt) in info.items():
        assert title and sub and group and glyph and alt


def test_import_makes_no_files(tmp_path):
    """A fresh Python imports the app with an empty data folder that must
    stay empty (nothing at import time)."""
    data = tmp_path / "data"
    env = dict(os.environ, MAYHEM_TESTING="1", MAYHEM_NO_SEND="1",
               MAYHEM_DATA_DIR=str(data))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", "import mayhemmacro"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not data.exists()


def test_session_bar_rarest_and_rares_are_this_runs_spawns_and_collects():
    gaia = {"name": "Gaia", "odds": "1 in 350M", "colour": "RARE", "den": 350_000_000}
    opal = {"name": "Opal", "odds": "1 in 10K", "colour": "RARE", "den": 10_000}
    big = [{"name": f"B{i}", "den": 20_000_000} for i in range(3)]
    floor = 10_000_000
    # nothing this run: nothing shown (an earlier run's rares don't count)
    assert M.bar_rares([], [], floor) == (None, 0)
    # a collected Opal doesn't hide a far rarer spawn
    assert M.bar_rares([gaia], [opal], floor)[0][0] == "Gaia"
    # three 10M+ spawns, then a worker picks up an Opal: still 3
    assert M.bar_rares(big, [], floor)[1] == 3
    assert M.bar_rares(big, [opal], floor)[1] == 3
    # a spawn that was then collected counts once
    assert M.bar_rares([gaia], [gaia], floor)[1] == 1
    # no rarity known: the newest spawn's name
    assert M.bar_rares([{"name": "Mystery"}], [], floor) == (("Mystery", "", None), 0)


def test_session_bar_helpers():
    assert M.short_odds(10_000_000) == "10M+"
    assert M.short_odds(50_000_000) == "50M+"
    assert M.short_odds(1_000_000_000) == "1B+"
    assert M.short_odds(100_000) == "100K+"
    assert M.bar_rare_floor({}) == 10_000_000
    assert M.bar_rare_floor({"bar_rare_min": 50_000_000}) == 50_000_000
    assert M.bar_rare_floor({"bar_rare_min": "junk"}) == 10_000_000
    assert M.rarity_from_text(M.rarity_text(50_000_000) + "+".rstrip("+")) == 50_000_000
