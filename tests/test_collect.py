"""Collected blocks: chat lines and worker pickup banners (from the user's
2026-10-14 screenshots), and a spawn banner whose odds got covered."""
import pytest

from mayhem import blocks, hud


def test_chat_collect_lines_wrapped_like_the_real_chat():
    lines = ["SomePlayer has collected a Inverted Resonance with a 1 /",
             "1,000,000 chance!",
             "Sporedelic has spawned for SomePlayer!",
             "SomePlayer has collected a Sporedelic with a 1 / 100,000",
             "chance!"]
    finds = hud.parse_collects(lines)
    assert [(f.who, f.block, f.variant, f.denominator, f.how) for f in finds] == [
        ("SomePlayer", "Resonance", "inverted", 1_000_000, "chat"),
        ("SomePlayer", "Sporedelic", "normal", 100_000, "chat")]


def test_chat_ocr_noise():
    # Vexen is 1 in 25,000 (wiki), so Chromatic Vexen is 1 in 625,000.
    f = hud.parse_collects(["SomeFriend has coIlected a Chromatic Vexen with a l / 625,OOO chance!"])
    assert [(x.who, x.block, x.variant, x.denominator) for x in f] == [
        ("SomeFriend", "Vexen", "chromatic", 625_000)]


@pytest.mark.parametrize("slip", [
    "SomeFriend has collected a Opal wrth a 1 / 10,000 chance!",      # 'with' misread
    "SomeFriend has collected a Opal with a 1 / 10,000 chanoe!",      # 'chance' misread
    "SomeFriend has collected a Opal with a 1 / l0,000 chance!",      # a digit misread
])
def test_one_misread_message_never_swallows_the_next_one(slip):
    finds = hud.parse_collects([
        slip, "Sporedelic has spawned!",
        "SomePlayer has collected a Inverted Resonance with a 1 / 1,000,000 chance!"])
    assert ("SomePlayer", "Resonance", "inverted", 1_000_000) in \
        [(f.who, f.block, f.variant, f.denominator) for f in finds]
    assert all(len(f.block.split()) <= 4 and "!" not in f.block for f in finds)


@pytest.mark.parametrize("seen, same", [
    ("SomePlayer", True), ("somepiayer", True), ("SomePIayer", True), ("SomePlayeR", True),
    ("S0mePlayer", True), ("SomePlaye", True), ("OtherKid22", False), ("Someone", False),
    ("", False),
])
def test_your_name_matches_through_ocr_slips(seen, same):
    assert hud.same_player(seen, "SomePlayer") is same


def test_spawn_lines_are_not_collects():
    assert hud.parse_collects(["Sporedelic has spawned for SomePlayer!"]) == []


def test_worker_pickup_banner_clean_and_garbled():
    for text in ("Void King just picked up a Chromatic Mosaic block!",
                 "void King Just picKea up a cnromatic Mosaic DlocK!"):
        f = hud.parse_pickup(text)
        assert f is not None and (f.block, f.variant, f.how) == ("Mosaic", "chromatic", "pickup")
    assert hud.parse_pickup("Void King has activated Ritual and generated a Rare block!") is None


def test_variant_rarity_from_the_table():
    assert blocks.rarity_of("Sporedelic") == 100_000
    assert blocks.rarity_of("Sporedelic", "inverted") == 1_000_000
    assert blocks.rarity_of("SNAKEBLOCK", "chromatic") == 12_625_000
    assert blocks.rarity_of("NotABlock", "chromatic") is None


def test_covered_banner_odds_fall_back_to_the_table():
    b = hud.parse_banner("Sporedelic has spawned! (1/")
    assert b is not None and b.block == "Sporedelic"
    assert blocks.rarity_of(b.block, b.variant) == 100_000


def test_a_shrunken_spawn_banner_is_zoomed_into_and_marked_small():
    """The banner pops in small and gets pushed up and shrunk by the next one
    (2026-10-14: 'Inverted Luminescent ... 1,000,000' read as '100,000')."""
    import os
    from PIL import Image
    from conftest import FIXTURES
    from mayhem.ocr import Ocr
    path = os.path.join(FIXTURES, "frames", "215059.jpg")
    ocr = Ocr()
    if not os.path.exists(path) or not ocr.available:
        import pytest
        pytest.skip("needs the local frames and Windows OCR")
    client = Image.open(path).convert("RGB").crop((0, 23, 1920, 1032))
    text = client.crop((660, 835, 1265, 880))              # 'Resonance has spawned! (1 / 100,000)'
    small = text.resize((text.width * 2 // 5, text.height * 2 // 5), Image.LANCZOS)
    client.paste(client.crop((660, 760, 1265, 805)), (660, 835))   # wipe the full-size line
    client.paste(small, (840, 790))
    _, b = hud.read_banner(ocr, client)
    assert b is not None and b.small
    assert (b.block, b.denominator, b.variant) == ("Resonance", 100_000, "normal")


def test_ocr_typos_become_the_wiki_spelling():
    for typo, real in (("Reljc", "Relic"), ("Luc d", "Lucid"), ("Lümjnescent", "Luminescent"),
                       ("Voidr1nyx", "Voidrinyx"), ("Snake Block", "SNAKEBLOCK"),
                       ("Corrupt Dr1ve", "Corrupt Drive"), ("Sacr1nare", "Sacrinare")):
        assert blocks.fix_name(typo) == real, typo
    assert blocks.rarity("Relic") == 600_000
    assert blocks.rarity_of("Luminescent", "inverted") == 1_000_000
    b = hud.parse_banner("Reljc has spawned! (1 / 600,000)")
    assert blocks.fix_name(b.block) == "Relic"


def test_fixes_from_the_first_long_run():
    """2026-10-14, 1.5 h in Arctic/Ocean/Overworld."""
    for typo in ("Nowstorm", "Riowstorm", "Sfiowstorm", "ShowStorm", "Snowstoim"):
        assert blocks.fix_name(typo) == "Snowstorm", typo
    assert blocks.fix_name("Syergreen") == "Evergreen"
    assert blocks.snap("Lucid", "normal", 250) == ("normal", 250_000)          # dropped ',000'
    assert blocks.snap("Snowstorm", "normal", 1_000_000) == ("inverted", 1_000_000)
    assert hud._split_variant("Inverte#vergreen") == ("Evergreen", "inverted")
    f = hud.parse_pickup("Void King just picked up a Inverte#vergreen block!")
    assert (f.block, f.variant, f.denominator) == ("Evergreen", "inverted", 1_000_000)


def test_diamond_misreads_snap_to_its_real_odds():
    """2026-10-16: Diamond (1 in 100,000) alerted as 1 in 5,100,000 and 109,900."""
    for misread in (5_100_000, 109_900, 10_000, 1_000):
        assert blocks.snap("Diamond", "normal", misread) == ("normal", 100_000), misread
    assert blocks.snap("Diamond", "normal", 1_000_000) == ("inverted", 1_000_000)
    assert blocks.fix_name("Plamond") == "Diamond"
