"""blocks: the rarity table and display names used for log-only alerts."""
import glob
import os

import pytest

from mayhem.blocks import KNOWN, display_name, rarity
from mayhem.robloxlog import parse_line

LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "logs")


def test_known_table():
    assert KNOWN["sporedelic"] == 100_000
    assert KNOWN["snakeblock"] == 505_000
    assert KNOWN["voidrinyx"] == 1_000_000
    assert KNOWN["resonance"] == 100_000
    assert KNOWN["opal"] == 10_000
    assert KNOWN["rare"] == 200
    for name, n in KNOWN.items():
        assert name == name.lower() and name.isalnum()
        assert isinstance(n, int) and n > 1


@pytest.mark.parametrize("raw, shown", [
    ("snakeblock", "SNAKEBLOCK"),
    ("sporedelic", "Sporedelic"),
    ("voidrinyx", "Voidrinyx"),
    ("Snake Block", "SNAKEBLOCK"),
    ("snakeblock_song", "SNAKEBLOCK"),
    ("RESONANCE", "Resonance"),
    ("  opal ", "Opal"),
    ("lucid", "Lucid"),
    ("vexen", "Vexen"),
    ("iris_inverted", "Iris Inverted"),
    ("new_thing_song", "New Thing"),
    ("Mega Block", "Mega Block"),
    ("NEWBLOCK", "NEWBLOCK"),
    ("", ""),
])
def test_display_name(raw, shown):
    assert display_name(raw) == shown


@pytest.mark.parametrize("name, n", [
    ("SNAKEBLOCK", 505_000),
    ("snakeblock", 505_000),
    ("Snake Block", 505_000),
    ("snakeblock_song", 505_000),
    ("Voidrinyx", 1_000_000),
    ("voidrinyx_song", 1_000_000),
    ("Sporedelic", 100_000),
    ("Resonance", 100_000),
    ("Opal", 10_000),
    ("Rare", 200),
    ("lucid", 250_000),
    ("Chromatic Vexen", None),
    ("", None),
])
def test_rarity(name, n):
    assert rarity(name) == n


def test_none_is_safe():
    assert rarity(None) is None          # type: ignore[arg-type]
    assert display_name(None) == ""      # type: ignore[arg-type]


@pytest.mark.skipif(not glob.glob(os.path.join(LOGS, "*.log")),
                    reason="the real Roblox logs are local only")
def test_every_song_block_in_the_real_logs_gets_a_name():
    songs = []
    for path in glob.glob(os.path.join(LOGS, "*.log")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            songs += [ev.value for ev in map(parse_line, fh) if ev and ev.kind == "rare_song"]
    assert sorted(songs) == ["lucid", "snakeblock", "sporedelic", "voidrinyx"]
    assert {s: display_name(s) for s in songs} == {
        "lucid": "Lucid", "snakeblock": "SNAKEBLOCK",
        "sporedelic": "Sporedelic", "voidrinyx": "Voidrinyx"}
    assert {s: rarity(s) for s in songs} == {
        "lucid": 250_000, "snakeblock": 505_000,
        "sporedelic": 100_000, "voidrinyx": 1_000_000}
