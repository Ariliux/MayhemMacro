"""Known Block Mayhem X blocks and how rare they are.

The spawn banner carries its own rarity ("Resonance has spawned! (1 / 100,000)"),
but the log does not: a music block only shows up there as "Could not find song
group for sound snakeblock_song". This table puts a number on those log-only
alerts. Only rarities read off the game are listed - a guessed number in a
Discord alert is worse than none, so an unknown block simply gets no number.
"""
import re

# How the game writes the name -> rarity denominator (1 in N). From the Block
# Mayhem X wiki's world pages (2026-10-14), plus Lucid (Arctic, read in game).
_TABLE: tuple[tuple[str, int], ...] = (
    # Overworld
    ("Iris", 1_000_000_000), ("Gaia", 350_000_000), ("Beyond", 10_000_000),
    ("Sovereign", 4_500_000), ("Entropy", 500_000), ("Evergreen", 100_000),
    ("Viridian", 50_000), ("Life", 25_000), ("Peace", 10_000),
    # Void
    ("Convergence", 3_000_000_000), ("Desolate", 200_000_000), ("Expanse", 75_000_000),
    ("Missing", 13_370_000), ("Tesseract", 6_000_000), ("Voidrinyx", 1_000_000),
    ("Nova", 100_000), ("Plasma", 50_000), ("Nil", 25_000), ("Moonrock", 10_000),
    # Ocean
    ("Unbinilium", 250_000_000), ("Jellyfish", 100_000_000), ("Whirlpool", 20_000_000),
    ("Ethereal", 3_000_000), ("Relic", 600_000), ("Luminescent", 100_000),
    ("Midnight", 50_000), ("Bubbly", 25_000), ("Aqua", 10_000),
    # Cave
    ("Caverna", 750_000_000), ("Prismatic", 500_000_000), ("Exagonios", 10_000_000),
    ("Geode", 5_000_000), ("Crystalline", 500_000), ("Resonance", 100_000),
    ("Azure", 50_000), ("Amethyst", 25_000), ("Opal", 10_000),
    # Lava
    ("Hypergiant", 1_000_000_000), ("Sacrinare", 666_666_666), ("Mercury", 100_000_000),
    ("Ignityx", 10_000_000), ("Thermos", 1_000_000), ("Temporal", 500_000),
    ("Eruption", 100_000), ("Scorched", 50_000), ("Vulcan", 25_000), ("Magma", 10_000),
    # Dreamland
    ("Cosana", 999_999_999), ("Social", 14_100_000), ("Puzzlar", 3_141_592),
    ("SNAKEBLOCK", 505_000), ("Sporedelic", 100_000), ("Mosaic", 50_000),
    ("Vexen", 25_000), ("Inked", 10_000),
    # Glitch (1 in 44 map)
    ("Amalgam", 111_111_111), ("Cyberspace", 100_000_000), ("Corrupt Drive", 10_000_000),
    ("Atonion", 1_000_000), ("Noise", 100_000), ("Deformity", 55_555), ("Error", 13_370),
    # Baseplate
    ("Thing", 100_000_000), ("TROLL", 66_666_666),
    # Arctic (no wiki page yet; read in game)
    ("Lucid", 250_000), ("Snowstorm", 100_000),
    # read in game, world not known yet
    ("Diamond", 100_000),
    ("Arboric", 100_000_000),
    ("Primordial", 111_111_111),         # chat: "has collected a Primordial with a 1 / 111,111,111 chance"
    ("Borealis", 3_500_000),             # chat, Arctic            # chat: "Inverted Arboric ... 1 / 1,000,000,000" (x10)
    # anywhere
    ("Rare", 200),
)

# Every block name on the Block Mayhem X wiki (2026-10), plus ones seen in game.
# Only used to fix OCR typos in names ('Luc d' -> 'Lucid'), never for rarity.
NAMES: tuple[str, ...] = (
    "Amalgam", "Amethyst", "Angel", "Aqua", "Arboric", "Arch Angel", "Atonion", "Aurora",
    "Azure", "Beyond", "Bubbly", "Caverna", "Common", "Convergence", "Corrupt Drive", "Cosana",
    "Cosmic", "Crystalline", "Cyberspace", "Deformity", "Desolate", "Diamond", "Doomsekkar",
    "Entropy", "Epic", "Error", "Eruption", "Ethereal", "Euphoria", "Evergreen", "Exagonios",
    "Expanse", "Fate", "Gaia", "Galactic Rift", "Geode", "Guardian", "Haunt", "Hypergiant",
    "Ignityx", "Immortal", "Inked", "Iris", "Jellyfish", "Legendary", "Life", "Lucid",
    "Luminescent", "Lunar", "Magma", "Mercury", "Midnight", "Missing", "Moonrock", "Mosaic",
    "Mythical", "Nebula", "Nil", "Noise", "Nova", "Omnipotent", "Opal", "Overflow Crystal",
    "Peace", "Plasma", "Prismatic", "Puzzlar", "Quantumyx", "Rare", "Relic", "Resonance",
    "Runic", "SNAKEBLOCK", "Sacrinare", "Scorched", "Singularity", "Sovereign", "Sporedelic",
    "Temporal", "Tesseract", "Thermos", "Thing", "True Andromeda", "Unbinilium", "Uncommon",
    "Vexen", "Viridian", "Voidrinyx", "Vulcan", "Whirlpool", "Zenith",
    "Social", "TROLL",
)

KNOWN: dict[str, int] = {re.sub(r"[^a-z0-9]", "", name.lower()): n for name, n in _TABLE}
_SPELLING: dict[str, str] = {re.sub(r"[^a-z0-9]", "", name.lower()): name for name, _ in _TABLE}


def _key(name: str) -> str:
    """'Snake Block', 'snakeblock_song' and ' SNAKEBLOCK ' all -> 'snakeblock'.
    OCR splits and joins words, so spaces and punctuation don't count."""
    low = (name or "").strip().lower()
    if low.endswith("_song"):
        low = low[:-len("_song")]
    return re.sub(r"[^a-z0-9]", "", low)


def fix_name(raw: str) -> str:
    """An OCR'd block name -> the real one when it is close ('Luc d' -> 'Lucid',
    'Snake Block' -> 'SNAKEBLOCK'); unknown names come back unchanged."""
    import difflib
    k = _key(raw)
    if not k:
        return raw
    keys = {_key(n): n for n in NAMES}
    for name, _ in _TABLE:
        keys.setdefault(_key(name), name)
    if k in keys:
        return keys[k]
    best = difflib.get_close_matches(k, list(keys), n=1, cutoff=0.75)
    if best:
        return keys[best[0]]
    # The first letter or two is what OCR gets wrong most ('Plamond' for
    # Diamond): compare the rest of the name too.
    if len(k) >= 6:
        rest = {key[1:]: key for key in keys if len(key) >= 6}
        best = difflib.get_close_matches(k[1:], list(rest), n=1, cutoff=0.82)
        if best:
            return keys[rest[best[0]]]
    return raw


def by_rarity(raw: str, variant: str, den: int | None) -> str:
    """A garbled name whose rarity read fine: the rarity narrows it down to a
    few blocks ('Voi&inpc' at 1 in 1,000,000 can only be a 1M block, and
    Voidrinyx is the closest of those; 'Erup€on wit'' -> Eruption). Known
    names, unknown rarities and names close to nothing come back as they are
    (after the usual typo fixing)."""
    import difflib
    fixed = fix_name(raw)
    if not den or is_known(fixed):
        return fixed
    times = VARIANT_TIMES.get(variant, 1)
    group = {_key(n): n for n, r in _TABLE if r * times == den}
    k = _key(raw)
    if not group or len(k) < 3:
        return fixed
    best = difflib.get_close_matches(k, list(group), n=1, cutoff=0.45)
    if best:
        return group[best[0]]
    for gk, name in group.items():           # the start of a name reads best
        if gk.startswith(k[:4]):
            return name
    return fixed


def is_known(name: str) -> bool:
    """A real block name (after typo fixing)."""
    fixed = fix_name(name)
    return _key(fixed) in {_key(n) for n in NAMES} or _key(fixed) in KNOWN


def snap(name: str, variant: str, den: int | None) -> tuple[str, int | None]:
    """Check an OCR'd rarity against the table. A number that matches the
    block in some variant tells the variant ('Snowstorm (1 / 1,000,000)' is
    Inverted); a number that matches nothing was misread ('1 / 250' for
    Lucid's 250,000), so the table's number for the read variant wins."""
    base = rarity(name)
    if not base or den is None:
        return variant, den
    if variant != "normal":
        # The word 'Inverted'/'Chromatic' was on screen: words read reliably,
        # digits are what get misread.
        return variant, base * VARIANT_TIMES.get(variant, 1)
    for v, times in VARIANT_TIMES.items():
        if den == base * times:
            return v, den
    return variant, base


def rarity(name: str) -> int | None:
    """1-in-N for a known block, None when we don't know it."""
    return KNOWN.get(_key(name))


VARIANT_TIMES = {"normal": 1, "inverted": 10, "chromatic": 25}   # wiki: Sporedelic 100k / 1M / 2.5M


def rarity_of(name: str, variant: str = "normal") -> int | None:
    """1-in-N for a known block in a variant (Inverted x10, Chromatic x25)."""
    base = rarity(name)
    return base * VARIANT_TIMES.get(variant, 1) if base else None


def display_name(raw: str) -> str:
    """The name to show: the game's own spelling when known ('SNAKEBLOCK'),
    else title case for a lower-case log name ('lucid' -> 'Lucid'). A name
    that already has capitals came off the screen and is kept as it is."""
    key = _key(raw)
    if key in _SPELLING:
        return _SPELLING[key]
    text = (raw or "").strip()
    if text.lower().endswith("_song"):
        text = text[:-len("_song")]
    text = " ".join(text.replace("_", " ").split())
    if text == text.lower():
        text = " ".join(w[:1].upper() + w[1:] for w in text.split())
    return text
