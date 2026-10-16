"""Windows' own OCR (Windows.Media.Ocr through pywinrt), with word boxes.

The banner, the HUD timer and the vote screen are all read with this. It is
called directly, not through a text-only wrapper: the vote needs WHERE each
word is, not just the text. Every box comes back in the pixels of the image
that was passed in, so a crop of the client rect gives client-relative boxes.
"""
import asyncio
import logging
import threading
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger(__name__)

# A read takes 3-80 ms. One that hangs must not freeze the run loop with it.
OCR_TIMEOUT = 5.0


@dataclass(frozen=True)
class OcrWord:
    text: str
    x: float          # left, in the INPUT image's pixels
    y: float          # top
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]


class Ocr:
    """One OCR engine, made the first time it is needed.

    Construction never fails, so the app can always start: if Windows has no
    OCR language pack (or pywinrt is missing in a build) `available` is False,
    `error` says why in plain words, and read() just returns nothing.
    """

    def __init__(self, lang: str = "en-US"):
        self.lang = lang
        self._engine = None
        self._tried = False
        self._error = ""
        self._logged = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # One lock for everything: the engine and the event loop are not safe
        # to share between threads, and the UI may call read() while the run
        # loop does.
        self._lock = threading.Lock()

    # -- engine ---------------------------------------------------------------
    def _load(self) -> None:
        """Make the engine once. Imports live here so a missing winrt package
        only turns OCR off instead of stopping the whole app at import."""
        if self._tried:
            return
        self._tried = True
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except Exception as e:
            self._error = f"Text reading isn't installed ({type(e).__name__})."
            return
        engine = None
        try:
            engine = OcrEngine.try_create_from_language(Language(self.lang))
        except Exception:
            engine = None
        if engine is None:
            # No pack for that language: whatever the user's Windows has. Every
            # word on screen is English, but digits and names read fine anyway.
            try:
                engine = OcrEngine.try_create_from_user_profile_languages()
            except Exception:
                engine = None
        if engine is None:
            self._error = ("Windows has no text-reading language installed. Add English in "
                           "Settings > Time & language > Language.")
            return
        self._engine = engine

    @property
    def available(self) -> bool:
        with self._lock:
            self._load()
            return self._engine is not None

    @property
    def error(self) -> str:
        """Why OCR is off, '' when it works (or hasn't been tried yet)."""
        return self._error

    # -- reading --------------------------------------------------------------
    def read(self, img: Image.Image, scale: float = 2.0) -> list[OcrLine]:
        """Recognise `img` and return its lines, boxes in `img`'s own pixels.

        The image is blown up `scale` times first (BICUBIC): the game's chunky
        outlined font reads far more reliably at 2-3x. Never raises; any
        failure returns [] and is logged once.
        """
        with self._lock:
            self._load()
            if self._engine is None:
                self._log_once(self._error)
                return []
            try:
                return self._read(img, scale)
            except Exception as e:
                self._log_once(f"text reading failed: {type(e).__name__}: {e}")
                return []

    def _read(self, img: Image.Image, scale: float) -> list[OcrLine]:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        w, h = img.size
        if w < 1 or h < 1:
            return []
        # The engine refuses anything bigger than this on either side (10000
        # today), so a whole-window read at 2x is fine but 6x would not be.
        limit = float(OcrEngine.max_image_dimension or 10000)
        scale = max(1.0, min(float(scale or 1.0), limit / w, limit / h))
        big = img.convert("RGBA")
        if scale != 1.0:
            big = big.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.BICUBIC)
        bw, bh = big.size
        sx, sy = bw / w, bh / h          # exact factors after rounding

        dw = DataWriter()
        dw.write_bytes(big.tobytes("raw", "BGRA"))
        # The 3-argument form: pywinrt 3.2.1 rejects the 5-argument overload
        # with "Invalid parameter count".
        bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(),
                                                     BitmapPixelFormat.BGRA8, bw, bh)
        # Our own event loop, made once and only ever run under the lock: the
        # threads that call read() have none of their own, and a fresh loop
        # per call would open a new local socket pair every time (Windows'
        # asyncio wake-up pipe), several times a second for hours.
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        result = self._loop.run_until_complete(
            asyncio.wait_for(self._recognize(bmp), OCR_TIMEOUT))

        out: list[OcrLine] = []
        for ln in result.lines:
            words = []
            for wd in ln.words:
                r = wd.bounding_rect
                words.append(OcrWord(wd.text, r.x / sx, r.y / sy, r.width / sx, r.height / sy))
            out.append(OcrLine(ln.text, tuple(words)))
        return out

    async def _recognize(self, bmp):
        return await self._engine.recognize_async(bmp)

    def close(self) -> None:
        """Free the event loop (at app exit). read() makes a new one if needed."""
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
            self._loop = None

    def __del__(self):
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed():
            try:
                loop.close()
            except Exception:
                pass

    def _log_once(self, why: str) -> None:
        if not self._logged and why:
            self._logged = True
            log.warning("OCR: %s", why)


def texts(lines: list[OcrLine]) -> list[str]:
    """Just the text of each line."""
    return [ln.text for ln in lines]
