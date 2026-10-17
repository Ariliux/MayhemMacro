"""mayhem/roller.py: rolling workers, driven with a fake mouse, screen and clock.

Nothing real is clicked or read here: every dependency is a fake, and the
real ones (real_deps) refuse to be made while MAYHEM_TESTING=1.
"""
import threading

import pytest

from mayhem import roller, safety
from mayhem.roller import Gems, RollDeps, RollResult

SPOT = (1000, 600)


class Game:
    """The fake world: a clock, the cursor, the clicks and the gem counter.

    A click takes CLICK_TIME of fake time (inputs.click_at needs about that).
    `gems` is a list of what each gem read sees, used up one per read; the
    last one repeats. `cursor_at` can move the cursor after a number of
    clicks, `fail_at` makes that click (1-based) not go through, and
    `stop_at` sets the stop event during that click.
    """
    CLICK_TIME = 0.22

    def __init__(self, gems=None, cursor_at=None, fail_at=None, stop_at=None):
        self.t = 0.0
        self.pos = SPOT
        self.clicks: list[tuple[float, tuple[int, int]]] = []
        self.gems = list(gems or [Gems(None)])
        self.gem_reads = 0
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

    def read_gems(self):
        self.gem_reads += 1
        return self.gems.pop(0) if len(self.gems) > 1 else self.gems[0]

    def deps(self):
        return RollDeps(click=self.click, cursor=self.cursor, read_gems=self.read_gems,
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
    assert g.gem_reads == 0                     # only 'all' reads the gems


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
    assert Game().roll("all").why != "done"     # 'all' has no count
    assert Game().roll("junk") == RollResult(10, "done")
    assert Game().roll(None) == RollResult(10, "done")


# -------------------------------------------------------------- stopping ---
def test_pressing_the_key_again_stops_at_once():
    g = Game(stop_at=4)
    r = g.roll(100)
    assert r == RollResult(4, "stopped")
    assert r.line == "Stopped rolling after 4 rolls."


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
    assert r.line == "Stopped: you moved the mouse after 3 rolls."


def test_a_small_wobble_keeps_rolling():
    g = Game(cursor_at={2: (SPOT[0] + 12, SPOT[1] - 12)})       # 17 px
    assert g.roll(5) == RollResult(5, "done")
    assert all(at == SPOT for _, at in g.clicks)                 # still the first spot


def test_roblox_not_in_front_stops_it():
    g = Game(fail_at=6)
    r = g.roll(25)
    assert r == RollResult(5, "front")
    assert r.line == "Stopped: Roblox isn't in front after 5 rolls."


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
def K(v):
    return Gems(v)


def test_all_stops_when_the_gems_stop_going_down():
    # 1.1K at the start, down every check, then stuck at 800 (out of gems)
    g = Game(gems=[K(1100), K(1000), K(900), K(800), K(800), K(800), K(800)])
    r = g.roll("all")
    # checks after 3, 6, 9 clicks drop; after 12, 15, 18 they don't
    assert r == RollResult(18, "gems")
    assert r.line == "Stopped: out of gems after 18 rolls."
    assert g.gem_reads == 7


def test_all_keeps_going_while_the_gems_drop():
    gems = [K(10_000 - 30 * i) for i in range(40)]
    g = Game(gems=gems + [K(gems[-1].value)])
    r = g.roll("all")
    assert r.why == "gems" and r.rolls == 3 * (39 + roller.GEM_STALE_CHECKS)


def test_a_roll_animation_that_holds_the_number_for_two_checks_is_fine():
    g = Game(gems=[K(1100), K(1100), K(1100), K(1000), K(1000), K(1000), K(1000)])
    r = g.roll("all")
    assert r == RollResult(3 * 6, "gems")       # stopped at the 3rd flat check after 1000


def test_all_stops_on_not_enough():
    g = Game(gems=[K(1100), K(1000), Gems(1000, not_enough=True)])
    r = g.roll("all")
    assert r == RollResult(6, "gems")
    assert r.line == "Stopped: out of gems after 6 rolls."


def test_all_with_not_enough_already_showing_never_clicks():
    g = Game(gems=[Gems(40, not_enough=True)])
    r = g.roll("all")
    assert r == RollResult(0, "gems") and g.clicks == []
    assert r.line == "Didn't roll: you're out of gems."


def test_all_that_can_never_read_the_gems_gives_up():
    g = Game(gems=[Gems(None)])
    r = g.roll("all")
    assert r == RollResult(3 * roller.GEM_STALE_CHECKS, "blind")
    assert r.line == "Stopped: couldn't read your gems after 9 rolls."


def test_one_unreadable_read_is_not_the_end():
    g = Game(gems=[K(1100), K(1000), Gems(None), K(900), K(800), K(800), K(800), K(800)])
    r = g.roll("all")
    assert r == RollResult(3 * 7, "gems")


def test_a_misread_higher_number_is_no_drop():
    g = Game(gems=[K(1100), K(11000), K(11000), K(11000)])
    assert g.roll("all") == RollResult(9, "gems")


def test_all_still_stops_for_the_key_the_mouse_and_roblox():
    falling = [K(5000 - i) for i in range(100)]
    assert Game(gems=falling, stop_at=7).roll("all") == RollResult(7, "stopped")
    assert Game(gems=falling, fail_at=5).roll("all") == RollResult(4, "front")
    assert Game(gems=falling, cursor_at={8: (0, 0)}).roll("all") == RollResult(8, "moved")


# ----------------------------------------------------------------- lines ---
@pytest.mark.parametrize("r, line", [
    (RollResult(25, "done"), "Rolled workers 25 times."),
    (RollResult(1, "done"), "Rolled workers 1 time."),
    (RollResult(37, "gems"), "Stopped: out of gems after 37 rolls."),
    (RollResult(1, "stopped"), "Stopped rolling after 1 roll."),
    (RollResult(0, "front"), "Didn't roll: Roblox isn't in front."),
    (RollResult(0, "stopped"), "Didn't roll: stopped right away."),
    (RollResult(0, "moved"), "Didn't roll: the mouse moved."),
    (RollResult(4, "odd"), "Stopped rolling after 4 rolls."),
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
    assert roller._ocr == []                  # refused before anything was made
    roller.release()                          # nothing to free: fine
