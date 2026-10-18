"""mayhem/roller.py: rolling workers, driven with a fake mouse, screen and clock.

Nothing real is clicked or read here: every dependency is a fake, and the
real ones (real_deps) refuse to be made while MAYHEM_TESTING=1.
"""
import threading

import pytest

from mayhem import roller, safety
from mayhem.roller import RollDeps, RollResult

SPOT = (1000, 600)


class Game:
    """The fake world: a clock, the cursor and the clicks.

    A click takes CLICK_TIME of fake time (inputs.click_at needs about that).
    `cursor_at` can move the cursor after a number of
    clicks, `fail_at` makes that click (1-based) not go through, and
    `stop_at` sets the stop event during that click.
    """
    CLICK_TIME = 0.22

    def __init__(self, cursor_at=None, fail_at=None, stop_at=None):
        self.t = 0.0
        self.pos = SPOT
        self.clicks: list[tuple[float, tuple[int, int]]] = []
        self.cursor_at = cursor_at or {}
        self.fail_at = fail_at
        self.stop_at = stop_at
        self.stop = threading.Event()

    def now(self):
        return self.t

    def sleep(self, s):
        assert s > 0
        self.t += s

    def cursor(self):
        return self.pos

    def click(self, x, y):
        n = len(self.clicks) + 1
        self.t += self.CLICK_TIME
        if n == self.stop_at:
            self.stop.set()
        if n == self.fail_at:
            return False
        self.clicks.append((round(self.t - self.CLICK_TIME, 6), (x, y)))
        if len(self.clicks) in self.cursor_at:
            self.pos = self.cursor_at[len(self.clicks)]
        return True

    def deps(self):
        return RollDeps(click=self.click, cursor=self.cursor,
                        sleep=self.sleep, now=self.now)

    def roll(self, count):
        return roller.roll(count, self.deps(), self.stop)


# ------------------------------------------------------------- a count ---
@pytest.mark.parametrize("count", [1, 5, 10, 25, 50, 100])
def test_rolls_the_count_at_the_cursor(count):
    g = Game()
    r = g.roll(count)
    assert r == RollResult(count, "done")
    assert len(g.clicks) == count
    assert all(at == SPOT for _, at in g.clicks)


def test_one_click_every_0_4_seconds():
    g = Game()
    g.roll(5)
    starts = [t for t, _ in g.clicks]
    assert starts == pytest.approx([0.0, 0.4, 0.8, 1.2, 1.6])
    assert roller.ROLL_EVERY == 0.4


def test_a_slow_click_never_brings_a_burst():
    g = Game()
    g.CLICK_TIME = 0.9                          # slower than the beat
    g.roll(3)
    starts = [t for t, _ in g.clicks]
    assert starts == pytest.approx([0.0, 0.9, 1.8])


def test_the_last_click_ends_it_without_waiting():
    g = Game()
    g.roll(1)
    assert g.t == pytest.approx(Game.CLICK_TIME)


def test_the_count_comes_from_the_setting():
    assert Game(stop_at=200).roll("all").why == "stopped"   # "all" has no count
    assert Game().roll("junk") == RollResult(10, "done")
    assert Game().roll(None) == RollResult(10, "done")


# -------------------------------------------------------------- stopping ---
def test_pressing_the_key_again_stops_at_once():
    g = Game(stop_at=4)
    r = g.roll(100)
    assert r == RollResult(4, "stopped")
    assert r.line == "Stopped summoning after 4 summons."


def test_a_stop_during_the_wait_is_noticed_within_a_step():
    g = Game()
    real_sleep = g.sleep

    def sleep(s):
        real_sleep(s)
        if len(g.clicks) == 2:
            g.stop.set()
    g.sleep = sleep
    r = g.roll(100)
    assert r.rolls == 2 and r.why == "stopped"
    assert g.t <= g.clicks[-1][0] + Game.CLICK_TIME + roller.PAUSE_STEP + 1e-9


def test_already_stopped_never_clicks():
    g = Game()
    g.stop.set()
    assert g.roll(10) == RollResult(0, "stopped")
    assert g.clicks == []


def test_moving_the_mouse_away_takes_over():
    g = Game(cursor_at={3: (SPOT[0] + 25, SPOT[1])})
    r = g.roll(50)
    assert r == RollResult(3, "moved")
    assert r.line == "Stopped: you moved the mouse after 3 summons."


def test_a_small_wobble_keeps_rolling():
    g = Game(cursor_at={2: (SPOT[0] + 12, SPOT[1] - 12)})       # 17 px
    assert g.roll(5) == RollResult(5, "done")
    assert all(at == SPOT for _, at in g.clicks)                 # still the first spot


def test_roblox_not_in_front_stops_it():
    g = Game(fail_at=6)
    r = g.roll(25)
    assert r == RollResult(5, "front")
    assert r.line == "Stopped: Roblox isn't in front after 5 summons."


def test_a_click_that_failed_because_the_mouse_moved_says_so():
    g = Game(fail_at=3)
    real = g.click

    def click(x, y):
        if len(g.clicks) == 2:
            g.pos = (SPOT[0], SPOT[1] + 40)       # the player grabbed the mouse mid-click
        return real(x, y)
    g.click = click
    assert g.roll(10) == RollResult(2, "moved")


# ------------------------------------------------------------------ all ---
def test_all_keeps_clicking_until_the_key_is_pressed_again():
    assert Game(stop_at=500).roll("all") == RollResult(500, "stopped")


def test_all_still_stops_for_the_mouse_and_roblox():
    assert Game(fail_at=5).roll("all") == RollResult(4, "front")
    assert Game(cursor_at={8: (0, 0)}).roll("all") == RollResult(8, "moved")


# ----------------------------------------------------------------- lines ---
@pytest.mark.parametrize("r, line", [
    (RollResult(25, "done"), "Summoned workers 25 times."),
    (RollResult(1, "done"), "Summoned workers 1 time."),
    (RollResult(1, "stopped"), "Stopped summoning after 1 summon."),
    (RollResult(0, "front"), "Didn't summon: Roblox isn't in front."),
    (RollResult(0, "stopped"), "Didn't summon: stopped right away."),
    (RollResult(0, "moved"), "Didn't summon: the mouse moved."),
    (RollResult(4, "odd"), "Stopped summoning after 4 summons."),
])
def test_result_lines(r, line):
    assert r.line == line


# ----------------------------------------------------------------- count ---
@pytest.mark.parametrize("value, want", [
    (1, 1), (25, 25), (100, 100), (30, 30), ("All", None), ("all", None), (" ALL ", None),
    ("50", 50), (0, 10), (-1, 10), (1001, 10), (True, 10), (None, 10), ("x", 10), (2.5, 10)])
def test_parse_count(value, want):
    assert roller.parse_count(value) == want


def test_count_menu_round_trip():
    assert roller.count_choices(10) == ["1", "5", "10", "25", "50", "100", "All"]
    assert roller.count_choices("all") == ["1", "5", "10", "25", "50", "100", "All"]
    assert roller.count_choices(30) == ["1", "5", "10", "25", "30", "50", "100", "All"]
    for text in roller.count_choices(10):
        saved = roller.count_setting(text)
        assert roller.count_text(saved) == text
    assert roller.count_setting("All") == "all" and roller.count_setting("25") == 25


# ------------------------------------------------------------- the gate ---
class Tick:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_holding_the_key_is_one_press():
    clock = Tick()
    gate = roller.PressGate(now=clock)
    assert gate.down() is True
    for _ in range(60):                       # Windows' key repeat, ~30 a second
        clock.t += 0.033
        assert gate.down() is False
    gate.up()
    clock.t += 0.2
    assert gate.down() is True                # pressed again: stop at once


def test_a_quick_second_press_counts():
    clock = Tick()
    gate = roller.PressGate(now=clock)
    assert gate.down()
    clock.t += 0.1
    gate.up()
    clock.t += 0.1
    assert gate.down()


def test_a_missed_key_up_only_costs_a_quiet_second():
    clock = Tick()
    gate = roller.PressGate(now=clock)
    assert gate.down()
    clock.t += 0.5
    assert not gate.down()                    # no key-up seen, too soon
    clock.t += roller.PressGate.QUIET
    assert gate.down()


# ------------------------------------------------------------ real deps ---
def test_the_real_mouse_and_screen_are_refused_while_testing():
    assert safety.testing()
    with pytest.raises(safety.SideEffectBlocked):
        roller.real_deps()
    roller.release()                          # nothing to free: fine
