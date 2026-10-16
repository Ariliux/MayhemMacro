"""The session bar: a slim stats strip at the top of the screen during a run.

It sits over Roblox's title bar, top centre, and shows run time, the rarest
block this session and the counters. It is click-through, never takes focus
and is not in the taskbar or Alt+Tab, so Roblox stays the window in front.
The app shows it only while a run is on AND Roblox is the window in front.

Kept clear of what the macro reads: the bar is at most BAR_H px tall, above
the top HUD band (8% of the Roblox picture) and the vote scan ignores the
top 17% anyway.
"""
import ctypes
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

from . import safety

BAR_H = 40
TOP = 4                        # floats this far below the top of the screen
RADIUS = 20                    # fully round ends
PAD_X = 20
GAP = 18                       # space each side of a divider
KEY = "#010203"                # see-through colour for the rounded corners
BG = "#0d131b"
EDGE = "#2a3646"
DIVIDER = "#1f2935"
TEXT = "#e8eef6"
MUTED = "#7389a0"
ACCENT = "#f5a524"
GREEN = "#22c55e"
ALPHA = 0.94

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
SW_HIDE, SW_SHOWNA = 0, 8
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_GetWindowLong = _u32.GetWindowLongPtrW
_GetWindowLong.restype = ctypes.c_ssize_t
_GetWindowLong.argtypes = (wintypes.HWND, ctypes.c_int)
_SetWindowLong = _u32.SetWindowLongPtrW
_SetWindowLong.restype = ctypes.c_ssize_t
_SetWindowLong.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
_ShowWindow = _u32.ShowWindow
_ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_SetWindowPos = _u32.SetWindowPos
_SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_uint)


def _pill_image(w: int, h: int):
    """The bar's background: a rounded pill drawn 4x and scaled down, so the
    edges are smooth. Pixels mostly outside the pill become the see-through
    colour; the rest are blended onto the background colour."""
    from PIL import Image, ImageDraw, ImageTk
    ss = 4
    key = tuple(int(KEY[i:i + 2], 16) for i in (1, 3, 5))
    big = Image.new("RGB", (w * ss, h * ss), key)
    mask = Image.new("L", (w * ss, h * ss), 0)
    box = [0, 0, w * ss - 1, h * ss - 1]
    ImageDraw.Draw(big).rounded_rectangle(box, radius=RADIUS * ss, fill=BG, outline=EDGE, width=ss)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=RADIUS * ss, fill=255)
    img = big.resize((w, h), Image.LANCZOS)
    cover = mask.resize((w, h), Image.LANCZOS)
    keyimg = Image.new("RGB", (w, h), key)
    img = Image.composite(img, keyimg, cover.point(lambda v: 255 if v >= 110 else 0))
    return ImageTk.PhotoImage(img)


def _round_rect(c: tk.Canvas, x0, y0, x1, y1, r, **kw):
    """Rounded rectangle (bottom corners round, top edge flush with the screen)."""
    pts = [x0, y0, x1, y0, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r]
    return c.create_polygon(pts, smooth=True, splinesteps=24, **kw)


class SessionBar:
    """One Toplevel made at app start (while the app itself has focus) and
    then only shown/hidden with Win32 calls that never activate it."""

    def __init__(self, root: tk.Misc, family: str = "Segoe UI"):
        safety.guard("the session bar window")
        self.root = root
        self.visible = False
        self._sig = None
        self.f_val = tkfont.Font(root=root, family=family, size=11, weight="bold")
        self.f_lbl = tkfont.Font(root=root, family=family, size=8, weight="bold")
        self.f_big = tkfont.Font(root=root, family=family, size=12, weight="bold")
        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.overrideredirect(True)
        w.configure(bg=KEY)
        w.attributes("-topmost", True)
        w.attributes("-transparentcolor", KEY)
        w.attributes("-alpha", 0.0)
        self.c = tk.Canvas(w, bg=KEY, highlightthickness=0, bd=0, height=BAR_H, width=600)
        self.c.pack()
        w.geometry(f"600x{BAR_H}+0+0")
        w.deiconify()
        w.update_idletasks()
        self.hwnd = int(w.wm_frame(), 16)
        ex = _GetWindowLong(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
              | WS_EX_NOACTIVATE | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
        _SetWindowLong(self.hwnd, GWL_EXSTYLE, ex)
        _ShowWindow(self.hwnd, SW_HIDE)
        w.attributes("-alpha", ALPHA)

    # -- show / hide (never activates) -------------------------------------------
    def show(self, screen_w: int) -> None:
        if not self.visible:
            self.visible = True
            _ShowWindow(self.hwnd, SW_SHOWNA)
        self._place(screen_w)

    def hide(self) -> None:
        if self.visible:
            self.visible = False
            _ShowWindow(self.hwnd, SW_HIDE)

    def _place(self, screen_w: int) -> None:
        width = int(self.c.cget("width"))
        x = max(0, (screen_w - width) // 2)
        _SetWindowPos(self.hwnd, HWND_TOPMOST, x, TOP, width, BAR_H,
                      SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass

    # -- drawing --------------------------------------------------------------------
    def update(self, d: dict, screen_w: int) -> None:
        """d: time, rarest (name, odds, colour) or None, rares, rares_label,
        votes (label, value), rejoins, afk, world, state."""
        sig = tuple(sorted((k, str(v)) for k, v in d.items()))
        if sig == self._sig:
            return
        self._sig = sig
        c = self.c
        c.delete("all")
        # segments: (label, value, value colour); the first is the brand/time
        segs = []
        if d.get("rarest"):
            name, odds, colour = d["rarest"]
            segs.append(("RAREST", f"{name}  {odds}" if odds else name, colour or ACCENT))
        else:
            segs.append(("RAREST", "none yet", MUTED))
        vote_lbl, vote_val = d.get("votes") or ("VOTES", "0")
        segs += [(d.get("rares_label") or "RARES", str(d.get("rares", 0)), TEXT),
                 (vote_lbl, vote_val, TEXT),
                 ("REJOINS", str(d.get("rejoins", 0)), TEXT),
                 ("AFK SAVES", str(d.get("afk", 0)), TEXT)]
        if d.get("world"):
            segs.append(("WORLD", d["world"], TEXT))

        # measure, then size the window to fit
        dot_w, cube_w = 10, 18
        head = d.get("time", "0m")
        widths = [max(self.f_lbl.measure(a), self.f_val.measure(b)) for a, b, _ in segs]
        total = (PAD_X + cube_w + 8 + self.f_big.measure(head) + 8 + dot_w
                 + sum(widths) + GAP * 2 * len(segs) + PAD_X)
        total = min(int(total), max(400, screen_w - 640))
        c.configure(width=total)
        self._bg = _pill_image(total, BAR_H)
        c.create_image(0, 0, image=self._bg, anchor="nw")

        x, mid = PAD_X, BAR_H // 2 - 1
        self._cube(x, mid, 8)
        x += cube_w + 8
        c.create_text(x, mid, text=head, anchor="w", fill=TEXT, font=self.f_big)
        x += self.f_big.measure(head) + 8
        running = d.get("state") == "running"
        c.create_oval(x, mid - 4, x + 8, mid + 4, fill=GREEN if running else ACCENT, outline="")
        x += dot_w
        for (lbl, val, colour), w in zip(segs, widths):
            c.create_line(x + GAP, 14, x + GAP, BAR_H - 14, fill=DIVIDER)
            x += GAP * 2
            c.create_text(x, mid - 8, text=lbl, anchor="w", fill=MUTED, font=self.f_lbl)
            c.create_text(x, mid + 8, text=val, anchor="w", fill=colour, font=self.f_val)
            x += w
        self._place(screen_w)

    def _cube(self, x: int, cy: int, s: int) -> None:
        """The app's mark: a tiny isometric cube."""
        c, h = self.c, s * 0.55
        top = [x + s, cy - s, x + 2 * s, cy - s + h, x + s, cy - s + 2 * h, x, cy - s + h]
        left = [x, cy - s + h, x + s, cy - s + 2 * h, x + s, cy + s, x, cy + s - h]
        right = [x + s, cy - s + 2 * h, x + 2 * s, cy - s + h, x + 2 * s, cy + s - h, x + s, cy + s]
        c.create_polygon(top, fill="#ffd089", outline="")
        c.create_polygon(left, fill=ACCENT, outline="")
        c.create_polygon(right, fill="#b86e0a", outline="")
