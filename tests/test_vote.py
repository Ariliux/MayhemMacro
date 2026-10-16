"""Vote screen options and the choice, on made-up OCR results.

Synthetic OcrLine / OcrWord lists laid out like the real vote screen
(tests/test_vote_real.py has the real frames): cards with the map name, a
VOTE button and an 'N Votes' count under it, in a 1920x1009 client. The one
real-OCR test here checks that ordinary gameplay frames are NOT taken for a
vote screen.
"""
import os

import pytest
from PIL import Image

from mayhem import MAPS
from mayhem.ocr import Ocr, OcrLine, OcrWord
from mayhem.vote import (VoteOption, card_votes, choose, click_point, find_options,
                         is_vote_screen)

CLIENT_H = 1009


def word(text: str, x: float, y: float, w: float = 160, h: float = 40) -> OcrWord:
    return OcrWord(text, x, y, w, h)


def line(*words: OcrWord) -> OcrLine:
    return OcrLine(" ".join(w.text for w in words), tuple(words))


def cue(x: float, y: float, votes: int = 0) -> list[OcrLine]:
    """What sits under a card's name (its box at x, y): the VOTE button and
    the vote count, the way the real OCR reads them ('OTE', 'O Votes')."""
    count = "O" if votes == 0 else str(votes)
    return [line(word("OTE", x + 40, y + 50, 80, 50)),
            line(word(count, x + 50, y + 130, 12, 14),
                 word("Vote" if votes == 1 else "Votes", x + 66, y + 130, 50, 14))]


def card(text: str, x: float, y: float, w: float = 160, h: float = 40,
         votes: int = 0) -> list[OcrLine]:
    """A vote card: the map name and what is under it."""
    return [line(word(text, x, y, w, h))] + cue(x + (w - 160) / 2, y, votes)


def names(options: list[VoteOption]) -> list[str]:
    return [o.name for o in options]


# ------------------------------------------------------------- find_options ---
def test_three_options_in_a_row_left_to_right():
    lines = card("Lava", 1500, 480) + card("Void", 300, 480) + card("Ocean", 900, 485)
    opts = find_options(lines, CLIENT_H)
    assert names(opts) == ["Void", "Ocean", "Lava"]
    assert is_vote_screen(opts)
    void = opts[0]
    assert (void.cx, void.cy) == (380, 500)
    assert void.box == (300, 480, 160, 40)


def test_options_on_one_ocr_line_are_split_per_word():
    lines = [line(word("Void", 300, 480), word("Ocean", 900, 480), word("Lava", 1500, 480))]
    lines += cue(300, 480) + cue(900, 480) + cue(1500, 480)
    assert names(find_options(lines, CLIENT_H)) == ["Void", "Ocean", "Lava"]


def test_two_rows_top_to_bottom_then_left_to_right():
    lines = (card("Dreamland", 800, 750) + card("Cave", 1200, 500) +
             card("Overworld", 200, 755) +
             card("Arctic", 400, 520))       # 20 px lower than Cave: same row
    assert names(find_options(lines, CLIENT_H)) == ["Arctic", "Cave", "Overworld", "Dreamland"]


def test_row_gap_is_40_px():
    same = find_options(card("Void", 900, 500) + card("Lava", 300, 539), CLIENT_H)
    apart = find_options(card("Void", 900, 500) + card("Lava", 300, 541), CLIENT_H)
    assert names(same) == ["Lava", "Void"]       # one row: left to right
    assert names(apart) == ["Void", "Lava"]      # two rows: top first


@pytest.mark.parametrize("seen, want", [
    ("Dreamlnad", "Dreamland"),
    ("dreamland", "Dreamland"),
    ("DREAMLAND", "Dreamland"),
    ("Dreamland!", "Dreamland"),
    ("Vold", "Void"),
    ("0cean", "Ocean"),
    ("Arctlc", "Arctic"),
    ("Basepiate", "Baseplate"),
    ("Glitch", "Glitch"),
])
def test_fuzzy_map_names(seen, want):
    opts = find_options(card(seen, 600, 500) + card("Cave", 1200, 500), CLIENT_H)
    assert names(opts) == [want, "Cave"]


def test_overworld_split_in_two_words():
    lines = [line(word("Over", 200, 500, 90), word("world", 300, 500, 110))]
    lines += cue(225, 500) + card("Void", 900, 500)
    opts = find_options(lines, CLIENT_H)
    assert names(opts) == ["Overworld", "Void"]
    over = opts[0]
    assert over.box == (200, 500, 210, 40)        # both words
    assert (over.cx, over.cy) == (305, 520)


@pytest.mark.parametrize("text", ["Necromancer", "Index", "Stats", "Shop", "Workers", "Boosts",
                                  "Bounty", "Settings", "Money", "Temporal", "Thermos",
                                  "Collect", "blocks!", "Pinned", "Recipe", "SomePlayer", "08:31"])
def test_everyday_words_are_not_maps(text):
    assert find_options(card(text, 600, 500), CLIENT_H) == []


def test_duplicates_keep_the_biggest():
    lines = (card("Dreamland", 1400, 800, 140, 24) +
             card("Dreamland", 300, 500, 260, 50) + card("Void", 1200, 500, 160, 50))
    opts = find_options(lines, CLIENT_H)
    assert names(opts) == ["Dreamland", "Void"]
    assert opts[0].box == (300, 500, 260, 50)


def test_map_word_in_top_hud_band_is_ignored():
    lines = (card("Overworld", 900, 60) +       # cy 80 < 17% of 1009
             card("Void", 300, 500) + card("Lava", 900, 500))
    assert names(find_options(lines, CLIENT_H)) == ["Void", "Lava"]
    # The band scales with the client height passed in.
    tall = card("Void", 1000, 300) + card("Lava", 900, 500)
    assert names(find_options(tall, 2000)) == ["Lava"]          # cy 320 < 340
    assert names(find_options(tall, 1009)) == ["Void", "Lava"]


def test_default_client_height_is_the_tested_setup():
    lines = card("Overworld", 900, 60) + card("Void", 300, 500)
    assert names(find_options(lines)) == ["Void"]


def test_no_text_no_options():
    assert find_options([]) == []
    assert find_options([OcrLine("", ())]) == []


def test_every_map_in_the_list_is_found():
    lines = [ln for i, m in enumerate(MAPS) for ln in card(m, 100 + 200 * i, 500)]
    assert names(find_options(lines, CLIENT_H)) == list(MAPS)


# ----------------------------------------------------------- is_vote_screen ---
def test_vote_screen_needs_two_different_maps():
    one = find_options(card("Dreamland", 900, 500), CLIENT_H)
    assert names(one) == ["Dreamland"]
    assert not is_vote_screen(one)
    assert not is_vote_screen([])
    twice = [VoteOption("Void", 1, 500, (0, 480, 2, 40)), VoteOption("Void", 9, 500, (8, 480, 2, 40))]
    assert not is_vote_screen(twice)
    assert is_vote_screen(find_options(card("Void", 300, 500) + card("Cave", 900, 500), CLIENT_H))


def test_a_sentence_that_names_a_map_is_not_an_option():
    banner = [line(word("Map", 600, 880), word("changed", 700, 880),
                   word("to", 800, 880), word("Dreamland", 900, 880))]
    assert find_options(banner + cue(900, 880), CLIENT_H) == []


def test_a_map_name_without_a_vote_button_under_it_is_not_an_option():
    lines = [line(word("Void", 300, 500)), line(word("Lava", 900, 500))]
    assert find_options(lines, CLIENT_H) == []
    # a neighbouring card's button doesn't count for it either
    assert names(find_options([line(word("Void", 300, 500))] + card("Lava", 610, 500),
                              CLIENT_H)) == ["Lava"]


def test_a_map_named_in_chat_never_beats_the_real_cards():
    cards = (card("Overworld", 564, 558, 180, 28) + card("Arctic", 913, 558, 90, 27) +
             card("Lava", 1238, 558, 70, 29))
    chat = [line(word("[xXgamerXx]:", 20, 190, 120, 20), word("dreamland", 150, 190, 90, 20),
                 word("pls", 245, 190, 30, 20)),
            line(word("Dreamland", 150, 260, 90, 20))] + cue(115, 260)   # alone, in the chat
    opts = find_options(chat + cards, CLIENT_H, 1920)
    assert names(opts) == ["Overworld", "Arctic", "Lava"]
    assert choose(opts, ["Dreamland", "Arctic"]).name == "Arctic"
    assert choose(opts, ["Glitch"]).name == "Overworld"


def test_chat_talk_about_maps_is_not_a_vote_screen():
    talk = [line(word("void", 30, 230, 40, 20), word("or", 75, 230, 20, 20),
                 word("lava?", 100, 230, 50, 20)),
            line(word("Void", 30, 260, 40, 20)), line(word("Lava", 30, 290, 40, 20))]
    talk += cue(0, 260) + cue(0, 290)
    assert not is_vote_screen(find_options(talk, CLIENT_H, 1920))


def test_card_vote_counts():
    lines = (card("Void", 300, 500, votes=1) + card("Lava", 900, 500, votes=0) +
             card("Cave", 1500, 500, votes=12))
    opts = find_options(lines, CLIENT_H)
    assert [card_votes(lines, o, CLIENT_H) for o in opts] == [1, 0, 12]
    void = opts[0]
    assert card_votes([ln for ln in lines if "Vote" not in ln.text], void, CLIENT_H) is None
    # '17', the countdown over the cards, is not a count
    assert card_votes([line(word("17", 360, 300, 30, 32))], void, CLIENT_H) is None


# ------------------------------------------------------------------- choose ---
@pytest.fixture
def two_rows() -> list[VoteOption]:
    lines = (card("Lava", 1500, 750) + card("Ocean", 900, 500) +
             card("Void", 300, 500) + card("Arctic", 300, 750))
    opts = find_options(lines, CLIENT_H)
    assert names(opts) == ["Void", "Ocean", "Arctic", "Lava"]
    return opts


def test_choose_first_ranked_that_is_an_option(two_rows):
    assert choose(two_rows, ["Lava", "Void"]).name == "Lava"
    assert choose(two_rows, ["Glitch", "Dreamland", "Ocean", "Void"]).name == "Ocean"


def test_choose_ranking_is_case_insensitive(two_rows):
    assert choose(two_rows, [" arctic "]).name == "Arctic"


def test_choose_none_ranked_present_takes_first_option(two_rows):
    assert choose(two_rows, ["Glitch", "Baseplate"]).name == "Void"     # top-left


def test_choose_empty_ranking_takes_first_option(two_rows):
    assert choose(two_rows, []).name == "Void"
    assert choose(two_rows, None).name == "Void"


def test_choose_no_options():
    assert choose([], ["Void"]) is None
    assert choose([], []) is None


# -------------------------------------------------------------- click_point ---
def test_click_point_is_the_centre_plus_offset():
    opt = VoteOption("Void", 380.4, 499.6, (300, 480, 160, 40))
    assert click_point(opt, (0, 0)) == (380, 500)
    assert click_point(opt, (10, -25)) == (390, 475)
    assert click_point(opt, [3, 4]) == (383, 504)           # settings JSON gives a list


@pytest.mark.parametrize("bad", [None, (), ("x", 1), [5]])
def test_click_point_bad_offset_means_no_offset(bad):
    opt = VoteOption("Void", 380, 500, (300, 480, 160, 40))
    assert click_point(opt, bad) == (380, 500)


# ------------------------------------------------ real frames (not votes) ---
FRAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "frames")
_OCR = Ocr()


@pytest.mark.skipif(not os.path.isfile(os.path.join(FRAMES, "215059.jpg")) or not _OCR.available,
                    reason="fixture frames or Windows OCR not available")
@pytest.mark.parametrize("frame", ["215059", "215112", "215124", "215157", "215239"])
def test_normal_gameplay_is_not_a_vote_screen(frame):
    """The whole client of an ordinary frame (quests, recipe board, buttons,
    a spawn banner) holds no map names at all."""
    with Image.open(os.path.join(FRAMES, frame + ".jpg")) as im:
        client = im.convert("RGB").crop((0, 23, 1920, 1032))
    opts = find_options(_OCR.read(client, 1.0), client.height)
    assert opts == [] and not is_vote_screen(opts)
