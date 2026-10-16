<h1 align="center">MayhemMacro</h1>

<p align="center">An AFK helper for the Roblox game <b>Block Mayhem X</b>.</p>

## Why I made this

In Block Mayhem X your workers collect blocks for you, so the game basically plays itself.
The annoying part is everything around it: Roblox kicks you after 20 minutes of being idle,
you get disconnected at 3 AM, the world vote picks a map you don't want, and the one
1-in-a-million block spawns while you're asleep and you never find out.

So I made a macro that keeps you in the game while you're away and tells you when
something rare shows up.

## What it does

- **Stops the AFK kick.** Just before Roblox's 20-minute idle kick it jumps once. It reads
  Roblox's own "you've been idle" note, so it only jumps when it actually has to.
- **Rejoins your private server.** After a kick, a disconnect, a crash or Roblox closing, it
  waits until you're away from your PC, then reopens Roblox straight into your server.
  No link saved? It joins a public server instead.
- **Rare block alerts.** It reads the spawn banner (`Resonance has spawned! (1 / 100,000)`)
  and Roblox's log, and tells you when a block at or above your rarity spawns. Inverted and
  Chromatic blocks can always alert, however common they are. Blocks you collect (from the
  chat and the worker pickup banners) show up in the activity feed.
- **Votes for your maps.** Tick the maps you like and put them in order. When the world
  vote comes up it clicks the green **VOTE** button of your #1 map if it's there, then #2,
  and so on. If none of your maps are in the vote, it votes for the first map.
- **Gets the game ready when a run starts.** It opens your pinned recipe (the arrow on the
  right), puts the cursor in the middle of the screen and jumps once. Each of those can be
  switched off.
- **Storage full alert** when your storage stays full for a minute.
- **A session bar** on top of Roblox during a run: run time, the rarest block so far, rare
  count, your votes, rejoins and anti-AFK jumps.
- **Never gets in your way.** It only presses keys or clicks while Roblox is the window in
  front, and never while you're using your mouse or keyboard. Anything bigger (closing and
  reopening Roblox, pulling it to the front) waits until you've been away for a minute,
  and not while a full-screen video or game is in front.

## What you need

- Windows 10 or 11
- A **1920×1080** screen with Windows display scaling at **100%**. That's the only setup
  it's tested on. Other sizes may work, but reading the game can miss things (the app tells
  you if your screen is different).
- Windows' English text recognition. It comes with an English Windows. If the app says it
  can't read the screen, add English in Settings → Time & language → Language & region.

## Installing it

1. Go to **[Releases](../../releases/latest)** and download `MayhemMacro-v1.0.0.zip`.
   Only get it from here, not from random re-uploads.
2. Right-click the zip → **Extract All**. Don't run it from inside the zip.
3. Open the folder and run **`MayhemMacro.exe`**.

Windows will probably warn you the first time. That's normal, and the section
[If Windows or your antivirus warns you](#if-windows-or-your-antivirus-warns-you)
below explains what to do.

Your settings, stats and log are saved in `%LOCALAPPDATA%\MayhemMacro`. If you ever want it
gone, delete the MayhemMacro folder and that one, and that's everything.

## Setting it up

1. **Server page:** copy your private server link in Roblox (the server's **Copy link**, or
   the link from the private server's page), then **Paste** → **Save**. The page tells you
   right away if the link can't work.
2. **World vote page:** tick the maps you want and put them in order with the arrows.
3. **Alerts page:** pick how rare a block has to be to alert you.
4. **Discord page** (optional): see [Discord alerts](#discord-alerts).

## Using it

- Open Block Mayhem X, then press **Start** in the app or **F1** in Roblox. **F2** stops it.
- **Don't run it at the same time as another macro that uses F1/F2.** Those keys work
  everywhere, so one press would start or stop both.
- If Roblox is in another game when you press Start, the run stops right away instead of
  touching that game. Join Block Mayhem X first.
- The pages are down the left side:
  - **Home** shows what it's doing right now, the world, the vote timer, your storage and
    the run's counters.
  - **Activity** lists what happened in plain English, newest first.
  - **Alerts**, **World vote**, **Server** and **Discord** are the setup above.
  - **Stats** keeps your totals from every run, **Log** has every detail.
  - **Settings** has the start-of-run steps, the session bar and keeping the PC awake.
  - **About** has the version and every keyboard shortcut.

## Discord alerts

MayhemMacro can post to a Discord channel of yours, so you can check on a long run from
your phone. It's **off** until you set it up, and it only ever posts to the one link you
give it.

1. In Discord, open the channel's settings → **Integrations** → **Webhooks** →
   **New Webhook** → **Copy Webhook URL**.
2. In MayhemMacro, open the **Discord** page, press **Paste**, then **Save**.
3. Turn on **Send alerts to Discord** and press **Send test** to check it arrives.

You pick which alerts it sends:
- a rare block spawns
- a run starts or stops
- rejoins
- your storage is full
- it stopped because of an error
- it's stuck: it couldn't rejoin after 5 tries
- every full hour of run time (off at first)

If you add your Discord user ID it can **@ you**, but only for rare blocks, errors and being
stuck. Alerts only go out during a run you started.

**Pictures:** with **Attach a picture of Roblox** on (Alerts page, **off** at first), a rare
block alert comes with a picture of the Roblox window, so you can see what spawned. Only
the Roblox window is in it, never the rest of your screen, and it only goes to your own
webhook. With it off, no picture is ever sent.

Anyone who has your webhook link can post in that channel, so keep it to yourself. The app
shows it masked once it's saved. The same goes for your private server link: anyone with it
can join your server.

## If Windows or your antivirus warns you

MayhemMacro isn't code-signed, because that costs money every year. Windows is careful with
any new app that hasn't been signed. It doesn't mean something's wrong with it.

**"Windows protected your PC"**: click **More info**, check it says `MayhemMacro.exe`,
then click **Run anyway**. That only allows this one file.

**Your browser says the file "isn't commonly downloaded"**: choose **Keep**. In Edge
it's **…** → **Keep** → **Show more** → **Keep anyway**.

**Your antivirus quarantines it**: don't restore it. Open an [issue](../../issues) with
the exact detection name and I'll get it reported as a false positive. It listens for F1/F2
and presses keys in Roblox, and some antivirus programs flag anything that does that.

**Please never** turn off Defender or real-time protection, or add exclusions for whole
folders like Downloads, just to get a program running. MayhemMacro doesn't need that, and
nothing you download ever should.

## Good to know

- **Use it at your own risk.** Macros can go against Roblox's or the game's rules, and your
  account is your responsibility.
- **I'm not affiliated with Roblox or Block Mayhem X, and I'm not partnered or collaborating
  with any other macro developers.** MayhemMacro is a solo project, made by me.
- **What it actually does on your PC:**
  - It looks at the Roblox window (screen reading with Windows' own text recognition) and
    reads Roblox's log files in `%LOCALAPPDATA%\Roblox\logs`.
  - It only presses keys and clicks inside Roblox, while Roblox is in front.
  - It listens for F1 and F2 to start and stop. It doesn't record anything else you type.
  - The only program it ever closes or opens is Roblox (`RobloxPlayerBeta.exe`), and only
    to rejoin during a run you started.
  - The only place it ever contacts is your own Discord webhook, and only if you turn
    alerts on. There are no update checks and nothing goes to GitHub or anywhere else.
  - The pictures it saves (vote screens, rare blocks) stay in your data folder.

## Found a bug?

Open an [issue](../../issues) and tell me what happened. Attaching your `log.txt` from
`%LOCALAPPDATA%\MayhemMacro` helps a ton, and so do the pictures in the `events` folder
there, since they show exactly what the macro saw.

## Running it from source

If you'd rather run it from the code, you need Python 3.14 (64-bit) on Windows:

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then double-click `run_mayhem.pyw`. The tests run with
`.venv\Scripts\python.exe -m pytest -q tests`. They never press a key, move the mouse, touch
Roblox or send anything. A few read real Roblox logs and screenshots in `tests/fixtures`,
which aren't in the repo, so those skip. `DESIGN.md` explains how it works inside.

## License

[MIT](LICENSE). Do what you want with it, just keep the license file.
