# How MayhemMacro works

MayhemMacro keeps you in the Roblox game **Block Mayhem X** (place id `75270037454060`)
while you're away. This page explains how it does that, for anyone reading or changing the
code. The [README](README.md) is the user guide.

It's a Python 3.14 app for Windows 10/11 with a CustomTkinter window. One engine thread
does the work; the window only shows what the engine reports.

## The rules everything follows

- **Never get in the way.** Keys and clicks only go to Roblox, only while Roblox is the
  window in front and the session is Block Mayhem X, and never while you're using the PC
  (no input if there was real input in the last 5 s). Input the macro made itself in the
  last 1.5 s doesn't count as yours. The screen is only read while Roblox is in front and
  not minimised. The one exception is [rolling workers](#rolling-workers): you start it
  with the roll key while you're at the PC, and it stops as soon as you move the mouse.
- **Disruptive steps wait until you're away**: no keyboard or mouse input for 60 s and no
  full-screen program (a video, another game, a presentation) in front. Closing and
  reopening Roblox for a rejoin, and pulling Roblox to the front for an anti-AFK jump
  (`afk_bring_to_front`, off by default), only happen then. After pulling Roblox forward it
  jumps and puts your window back (ALT tap + `SetForegroundWindow`, `AttachThreadInput` as
  a fallback, never Alt+Tab).
- **Roblox reads the mouse through Raw Input**, so cursor moves are relative `SendInput`
  steps, never `SetCursorPos` alone. Space is sent as a scan code held for 0.15 s.
- **The only program it closes or opens is Roblox**: `RobloxPlayerBeta.exe` of the current
  Windows user, and only to rejoin during a run you started. A run started while Roblox is
  in another game stops instead of touching that game; so does joining another game
  mid-run. The one exception is an update you said Yes to: then it starts the new
  `MayhemMacro.exe`, closes, and the new version reopens the app (see [Updates](#updates)).
- **Network:** your own Discord webhook, only if you turn alerts on, and one read-only
  request to GitHub's API for this repo's newest release when the app opens and every
  12 hours (`check_updates`, on by default, switched off on About or Settings). Updates
  only come from this repo's releases and are only installed when their sha256 matches
  the fingerprint GitHub lists. Rejoining opens your private server link through the
  `roblox://` protocol: no Roblox web API, no cookies.
- **Privacy:** the webhook URL, the private server link and your Discord id are never
  logged or shown unmasked. No picture is uploaded unless "Attach a picture of Roblox" is
  on, and then only a crop of the Roblox window.
- **Tests never cause real side effects.** Every real side effect calls
  `mayhem.safety.guard()` first, which refuses while `MAYHEM_TESTING=1`
  (`tests/conftest.py` sets it, plus a temp data folder).

## What it watches

### The Roblox log

Roblox writes one log per client in `%LOCALAPPDATA%\Roblox\logs`. The macro follows the
newest `*_Player_*_last.log` (never the `CrashHandler` ones). Lines look like
`2026-10-09T01:28:38.925Z,7.925577,2358,6[,Level] [FLog::Category] message`.

| kind | the line | what it means |
|---|---|---|
| join | `! Joining game '<uuid>' place <id> at <ip>` | a new session; Block Mayhem X if the id matches |
| entered | `[FLog::SessionTransitionFSM] Entered play session.` | in the server |
| loaded | `[FLog::CreatorOutput] ✅ \| CLIENT: GuiController module loaded` | the game's UI is up |
| left | `[FLog::SessionTransitionFSM] Tearing down.` | the session ended |
| disconnect | `Connection lost`, `Sending disconnect with reason: <n>`, `Disconnection Notification. Reason: <n>`, `id_connection_lost`, `lost connection` | disconnected or kicked |
| rare_song | `Could not find song group for sound <name>_song` | a music block appeared |
| world_music | the same warning without `_song` (`Dreamland`, `Arctic`) | the world's music started |
| chromatic | `[Chromatic] Thread stopped for block: <name>_chromatic ...` | a Chromatic block *was* on the server (written minutes late) |
| idle | `Player has been idle for <seconds> seconds` | Roblox's idle clock (after 2 min, then every 30 s) |

`[DFLog::NetworkClient] Client:Disconnect` is **not** a disconnect: Roblox writes it on
every normal teleport and leave. When a run starts, the log so far is read to learn the
current state, but nothing in it is reported as new.

### The screen

The screen is read with Windows' own OCR (`Windows.Media.Ocr` through pywinrt), which
gives every word with its box. Everything is measured as a fraction of the Roblox **client
rect** (the game picture without the title bar), so Roblox may be windowed or full screen.
1920x1080 at 100% scaling is the only tested setup.

| what | where (client fractions x0, y0, x1, y1) | read as |
|---|---|---|
| vote timer | 0.532, 0.089, 0.612, 0.139 | the only `mm:ss` |
| storage | 0.395, 0.089, 0.470, 0.139 | `n/m` |
| gems | 0.547, 0.026, 0.623, 0.091 | `950`, `1.1K`, `12.86K` (K/M/B) |
| spawn banner | 0.15, 0.72, 0.85, 0.89 | `<Block> has spawned! (1 / <N>)` |
| chat | 0.0, 0.04, 0.34, 0.46 | `<player> has collected a <Block> with a 1 / <N> chance!` |
| pinned recipe | 0.76, 0.24, 1.0, 0.50 | the "Pinned Recipe" header and its arrow button |

OCR is noisy on this font, so the readers are tolerant: the timer and storage need two
reads that agree; a banner's block name is fixed against the list of known blocks
(`Reljc` → Relic) and a misread rarity is checked against the block's known rarity
(`1 / 250` → Lucid's 250,000, a 10x or 25x number → Inverted or Chromatic). Shrunken
banner text (a banner popping in or pushed up by the next one) is read again zoomed in.

## The run loop

The engine (`mayhem/engine.py`) ticks about twice a second:

1. Read the new log lines and update the state.
2. Look at the Roblox window and at you (idle time, full-screen apps).
3. Rejoin if needed (below).
4. With Roblox in front in Block Mayhem X: read the top HUD (every 2 s, every 0.5 s when
   the vote timer is low), the banner (every 1 s) and the chat (every 2 s).
5. Once per run, and again after each join: open the pinned recipe (only when its arrow
   points left, ◀, so an open panel is never closed), put the cursor in the middle of the
   screen and jump. The first time it waits 1.5 s after the Start key; after a rejoin it
   waits for the usual 5 s rule, and after a join you made, until you're away.
6. Anti-AFK, rare alerts, the HUD and the status.

Only one action runs at a time: the anti-AFK jump, a vote click, the setup and a rejoin
share one lock.

### Rare blocks

- A spawn banner alerts when its rarity is at least `rare_min`, with an @mention at
  `rare_ping_min`. Inverted and Chromatic always alert when `alert_variants_always` is on.
- The same banner is only one alert for 30 s after it was last read, unless the new read
  names a variant the old one didn't (a Chromatic Vexen right after a normal Vexen is a
  new block).
- A banner read without its number, or read small, waits up to 3 s for a better read. It
  goes out as it is when no better read can come: Roblox went behind other windows, a new
  server was joined, the run stopped.
- A music block's log line waits 3 s for its banner (which has the rarity and a picture);
  a banner and a log line for the same block within 60 s are one alert.
- A late Chromatic note is matched to a Chromatic banner of that block from the last
  15 minutes, otherwise it's a "was on your server" alert.
- Collected blocks (chat lines of your own account, worker pickup banners) go to the
  activity feed and the session bar. What's already in the chat when a run starts isn't
  new.

### Anti-AFK

Roblox kicks after 20 idle minutes. When the latest idle line says at least
`anti_afk_after_min` minutes (default 10, kept to 2-15) and input is allowed, it jumps. The
next idle line shows a smaller number if the jump landed. If the game prints no idle
lines, it jumps every `anti_afk_after_min` minutes of no input. With Roblox behind other
windows at 15 minutes idle, it brings Roblox forward for the jump if that setting is on
and you're away, and otherwise notes once that a kick may come (and rejoins after it).

### Storage

Storage full (`n >= m`) for 60 s alerts once. It alerts again only after a reading with
room in it, not after a rejoin (storage belongs to the account, not the server).

### The world vote

A vote happens every 10 minutes. When the timer reads 5 s or less (or was low and is
covered), the whole client is read every 0.5 s for up to 45 s. The vote screen has three
cards in a row: the map name, a green VOTE button under it and an `N Votes` count under
that. A map name counts as an option only when the OCR line is nothing but the name, it's
outside the chat and a VOTE button or vote count sits right under it, so map names in chat
or in a banner are never options. Two different options make a vote screen.

The click goes to the VOTE button of the highest-ranked ticked map (`vote_maps`, best
first) that is an option, or the first option (left to right) if none is. Two seconds
later the chosen card's own vote count is read: if it went up (or the screen is gone), the
vote counted; if not, it clicks once more, at most. A picture of every vote screen is
saved locally.

### Rejoin

With `auto_rejoin` on, a rejoin starts on a disconnect line, no Roblox window for 20 s, or
no Block Mayhem X session for 60 s, and also when a rejoined game is still not loaded a
minute after joining. It waits until you're away, kills `RobloxPlayerBeta.exe` (a launcher
may otherwise ask "close the running instance?" with nobody there to answer), notes the
newest log and opens the next link to try.

The saved link is turned into `roblox://` links (`mayhem/rejoin.py`):

| saved | tried |
|---|---|
| `...?privateServerLinkCode=<code>` or `linkCode=<code>` | `roblox://placeID=75270037454060&linkCode=<code>` |
| `https://www.roblox.com/share?code=<code>&type=Server` | `roblox://navigation/share_links?code=<code>&type=Server` |
| a `roblox://` link | that link as saved, then the form above |
| nothing (or a link that can't work) | a public server: `roblox://experiences/start?placeId=75270037454060` |

Retries take the next link in the list. A try worked when a **new** log shows the Block
Mayhem X join and `Entered play session.` within 120 s, then the `loaded` line within 60 s
more. Failed tries back off 30 / 60 / 120 / 300 s; after 5 in a row the run stops with a
"stuck" alert. At most 6 rejoins an hour.

## Rolling workers

Not part of a run: the player opens Workers in Roblox, hovers the game's Roll button and
presses the roll key (`roll_key`, F8; never F1, F2 or the Medal key). `mayhem/roller.py`
left-clicks at that cursor spot every 0.4 s on a thread of its own, `roll_count` times
(1, 5, 10, 25, 50, 100) or, with `"all"`, until the gem pill stops going down (read every
3 clicks, 3 reads in a row without a drop) or the game says "not enough" anywhere outside
the chat. It also stops when the key is pressed again, when the mouse is moved more than
20 px from the spot, and when a click doesn't go through (Roblox isn't in front). It
refuses to start during a run, and a run starting stops it. Holding the key is one press:
a key-down only counts after a key-up (or a quiet second). The end is one Activity and Log
line ("Rolled workers 25 times.", "Stopped: out of gems after 37 rolls."). The clicks go
through `inputs.click_at`, so they keep its guard and its Roblox-in-front check.

## Updates

`mayhem/update.py` does the checking and installing; the window only asks and shows.

- **Checking.** `GET https://api.github.com/repositories/1421452081/releases/latest`: the
  repo by its permanent number, never by name (a deleted or renamed account's name can be
  registered again; the number can't). The answer must still name `Ariliux/MayhemMacro`
  (its API link, its page and the zip's link), so the number and the name are the same
  repo. Drafts, pre-releases, rate limits and no network are all "no news". Nothing about
  you goes along, only `MayhemMacro/<version>` as the User-Agent.
- **What it installs.** Only the asset `MayhemMacro-v<version>.zip` for that release's own
  version, linked under `https://github.com/Ariliux/MayhemMacro/releases/download/`, with a
  `sha256:` digest from GitHub. The download is hashed on the way and thrown away on a
  mismatch. A release without such a zip is still shown as "out", just not installed.
- **Asking.** A newer version puts a link in the sidebar footer, a dot on About and the
  question "v1.0.1 is available. Do you want to update?" as a card inside the window
  (never a Windows pop-up, so it can't take focus). Never during a run: found during one,
  it asks 1.5 s after the run stops; a question on screen goes away when a run starts and
  comes back after it; a Yes given during a run is kept and acted on when it stops.
- **Installing** (only the built app, `sys.frozen`, in a folder it can write to; from
  source it only says a new version is out):
  1. the zip goes to `%LOCALAPPDATA%\MayhemMacro\update\` and is unpacked to `update\new\`
     (nothing may land outside; a list of every file and its size is kept);
  2. the new `MayhemMacro.exe` is started with
     `--finish-update <install folder> <old pid> <old version>` and the app closes. These
     arguments are a contract between versions and never change;
  3. the new version waits for the old one to end, copies `_internal` and the exe in next
     to the install as `.new`, checks them against the list, then switches over with three
     renames (undone if one fails; nothing changes if the old app is still running), and
     opens the updated app;
  4. that first start says "updated" and removes `update\` and `_internal.old` 20 s later.
- **Never touched:** `settings.json`, `stats.json` and `events\`. Only `MayhemMacro.exe` and
  `_internal` in the install folder are replaced; anything else there stays.

Every request, the swap, the install, the clean-up and every program start call
`safety.guard()` first.

## Settings

`%LOCALAPPDATA%\MayhemMacro\settings.json`, saved atomically (temp file, fsync, replace).
Missing keys get their defaults, a value of the wrong type is replaced by the default, and
a file that can't be read is renamed to `settings.corrupt-<n>.json`. The defaults are in
`mayhem/paths.py` (`DEFAULT_SETTINGS`). `settings_version` is 2: a file without it is from
1.0.0, which saved `"check_updates": false` with no switch for it, so that value is dropped
once and the default (on) applies.

## The code

```
mayhem/__init__.py   APP_NAME, APP_VER, PLACE_ID, MAPS
mayhem/safety.py     guard(): the one switch that blocks real side effects in tests
mayhem/paths.py      the data folder and settings.json
mayhem/robloxlog.py  following the Roblox log
mayhem/window.py     the Roblox window, its picture, your idle time, focus changes
mayhem/ocr.py        Windows OCR with word boxes
mayhem/hud.py        reading the timer, storage, banners, chat and the pinned recipe
mayhem/vote.py       the vote screen's options and which one to pick
mayhem/blocks.py     the blocks and their rarities
mayhem/inputs.py     SendInput: Space, relative mouse moves, clicks
mayhem/rejoin.py     link parsing, closing and opening Roblox
mayhem/discord.py    the webhook sender and the alert messages
mayhem/stats.py      stats.json
mayhem/update.py     the update check, download, unpacking and file swap
mayhem/overlay.py    the session bar
mayhem/roller.py     rolling workers: the click loop, the gem check, the roll key's press gate
mayhem/engine.py     the run loop (one thread)
mayhemmacro.py       the window
run_mayhem.pyw       double-click launcher (hands over to .venv's pythonw)
tools/ocr_probe.py   times Windows OCR on saved screenshots
tests/               pytest
```

Everything with a side effect is passed to the engine in `Deps` (window, OCR, inputs,
rejoin, log tail, clock), so the tests drive it tick by tick with fakes. The engine never
talks to Discord or writes files itself: it emits plain dicts (`state`, `hud`, `rare`,
`collected`, `vote`, `anti_afk`, `rejoin`, `storage_full`, `log`), and the app turns them
into the activity feed, stats and Discord alerts, the one place your alert switches are
applied.

Run the tests with `.venv\Scripts\python.exe -m pytest -q tests`.
