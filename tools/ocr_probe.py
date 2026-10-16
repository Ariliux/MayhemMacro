"""Time Windows OCR on saved 1920x1080 Block Mayhem X screenshots.

Reads the spawn banner, the vote timer and the storage pill of each picture
and prints what came back and how long each read took. A developer tool, not
part of the app or the tests:

    .venv\\Scripts\\python.exe tools\\ocr_probe.py picture1.jpg picture2.jpg
"""
import asyncio
import sys
import time

from PIL import Image

# Screen boxes (x0, y0, x1, y1) on a 1920x1080 screenshot, and the upscale.
CROPS = (("banner", (460, 850, 1460, 910), 2),
         ("timer", (1040, 118, 1180, 158), 3),
         ("storage", (780, 118, 900, 158), 3))


def _engine():
    from winrt.windows.media.ocr import OcrEngine
    return OcrEngine.try_create_from_user_profile_languages()


def _bitmap(img: Image.Image):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    img = img.convert("RGBA")
    w, h = img.size
    dw = DataWriter()
    dw.write_bytes(img.tobytes("raw", "BGRA"))
    return SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)


async def _read(engine, img: Image.Image) -> list[str]:
    res = await engine.recognize_async(_bitmap(img))
    return [ln.text for ln in res.lines]


async def main(files: list[str]) -> None:
    engine = _engine()
    if engine is None:
        print("Windows has no OCR language installed.")
        return
    print("OCR language:", engine.recognizer_language.language_tag)
    for f in files:
        with Image.open(f) as im:
            for name, box, scale in CROPS:
                crop = im.crop(box)
                crop = crop.resize((crop.width * scale, crop.height * scale))
                t = time.perf_counter()
                lines = await _read(engine, crop)
                ms = (time.perf_counter() - t) * 1000
                print(f"{f[-14:]:>14} {name:7s} {ms:5.0f} ms {lines}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
