"""Fullscreen at the start: F11 once if Roblox is in a window, and never a
fight with Roblox's own switch or with you. All fakes - no key is pressed."""
from mayhem import engine as engine_mod, window
from test_engine import Rig

F11 = ("scan", engine_mod.SCAN_F11)


def rig(**kw) -> Rig:
    r = Rig(fullscreen=True, **kw)
    r.window.full = False                   # Roblox starts in a window
    real = r.inputs.tap_scan

    def tap(scan, hold=0.03):               # F11 toggles, like the real thing
        ok = real(scan, hold)
        if ok and scan == engine_mod.SCAN_F11 and r.window.f11_works:
            r.window.full = not r.window.full
        return ok

    r.window.f11_works = True
    r.inputs.tap_scan = tap
    return r


def presses(r: Rig) -> int:
    return r.inputs.calls.count(F11)


def test_windowed_at_the_start_gets_one_f11():
    r = rig().start()
    r.run(2.0)
    assert presses(r) == 0, "waits first: Roblox may switch itself"
    r.run(4.0)
    assert presses(r) == 1 and r.window.full
    r.run(60.0)
    assert presses(r) == 1
    assert any("pressed F11" in t for t in r.logs("info"))
    assert any("fullscreen now" in t for t in r.logs("info"))


def test_already_fullscreen_is_left_alone():
    r = rig()
    r.window.full = True
    r.start()
    r.run(60.0)
    assert presses(r) == 0 and not r.logs("warn")


def test_roblox_going_fullscreen_by_itself_is_not_flipped_back():
    r = rig().start()
    r.run(2.0)
    r.window.full = True                    # its own saved setting kicked in
    r.run(60.0)
    assert presses(r) == 0


def test_f11_that_does_nothing_is_tried_twice_then_left():
    r = rig()
    r.window.f11_works = False
    r.start()
    r.run(120.0)
    assert presses(r) == 2
    assert any("Press F11 in Roblox yourself" in t for t in r.logs("warn"))


def test_put_back_in_a_window_by_you_stays_that_way():
    r = rig().start()
    r.run(6.0)
    assert r.window.full
    r.window.full = False                   # you pressed F11 yourself
    r.run(120.0)
    assert presses(r) == 1


def test_a_new_roblox_window_after_a_rejoin_gets_its_own_go():
    r = rig().start()
    r.run(6.0)
    assert presses(r) == 1
    r.window.hwnd, r.window.full = 200, False
    r.run(6.0)
    assert presses(r) == 2 and r.window.full


def test_only_with_roblox_in_front():
    r = rig()
    r.window.fg = False
    r.start()
    r.run(30.0)
    assert presses(r) == 0
    r.window.fg = True
    r.run(2.0)
    assert presses(r) == 0, "the 3 s count only time spent in front"
    r.run(2.0)
    assert presses(r) == 1


def test_waits_while_you_are_using_the_pc():
    r = rig().start()
    for _ in range(20):                     # 10 s of you moving the mouse
        r.clock.touch()
        r.tick()
    assert presses(r) == 0
    r.run(3.0)
    assert presses(r) == 1


def test_switched_off_never_presses():
    r = rig()
    r.settings["fullscreen"] = False
    r.start()
    r.run(120.0)
    assert presses(r) == 0


def test_the_start_setup_waits_for_fullscreen():
    r = rig(center_cursor=True).start()
    r.run(30.0)
    moves = [i for i, c in enumerate(r.inputs.calls) if c[0] == "move"]
    f11 = r.inputs.calls.index(F11)
    assert moves and f11 < moves[0], "the cursor is centred after the switch, not before"
    sleeps = r.clock.sleeps
    assert engine_mod.FULLSCREEN_SETTLE in sleeps


def test_the_start_setup_never_waits_forever():
    r = rig(center_cursor=True)
    r.window.full = None                    # the window can't be read
    r.start()
    r.run(engine_mod.FULLSCREEN_WAIT + 5)
    assert presses(r) == 0
    assert any(c[0] == "move" for c in r.inputs.calls)


def test_the_real_check_only_looks_and_handles_no_window():
    assert window.is_fullscreen(0) is None
    assert window.is_fullscreen(0x7FFFFFF0) is None      # not a window
