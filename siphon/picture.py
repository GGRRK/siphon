"""A picture the user chose for a playlist, made ready to keep beside it (Playlists.set_cover).

GdkPixbuf decodes it: on Linux every format glycin reads (JPEG, PNG, WebP, GIF, BMP, TIFF, HEIC, AVIF, JPEG XL...),
in the Windows build its own loaders (JPEG, PNG, GIF, BMP, TIFF, ICO, TGA, PNM). What GdkPixbuf can't read goes
through ffmpeg, which Siphon needs anyway: WebP and HEIC on Windows (its build decodes no AVIF: no AV1 decoder).
A picture of more than MAX_PIXELS is refused from its header, before anything decodes it.
The picture is turned upright (EXIF orientation), cropped to a centred square like a downloaded playlist's
picture, shrunk to at most SIZE pixels and saved afresh: a JPEG, or a PNG when parts of it are see-through (a
GIF's or PNG's background). Saving afresh also leaves the photo's metadata, a camera's GPS position say, out of
a folder other players and sync tools read.
"""

import re
import subprocess
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

try:  # glycin, which GdkPixbuf reads through on Linux
    gi.require_version("Gly", "2")
    from gi.repository import Gly  # noqa: E402
except (ImportError, ValueError):  # the Windows build (GdkPixbuf's own loaders), or an older Linux
    Gly = None

from . import paths  # noqa: E402

# The page's cover needs 288 px (144 at twice the scale), 432 on a 3x screen; YouTube's playlist pictures are 720,
# SoundCloud's 500. At 1200 a noisy 24 MP photo saves as 450 KB, a smooth one as 60 KB (measured 2026-09-28).
SIZE = 1200
QUALITY = 90  # JPEG; about what the downloaded pictures get (ffmpeg -q:v 2)
MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 64_000_000  # 8000 x 8000: a 61 MP camera's photo fits; decoded whole it takes 256 MB
FFMPEG_SECONDS = 30

UNREADABLE = "That file is not a picture Siphon can read."


class PictureError(Exception):
    """Why a file can't be a playlist's picture, in words for the user."""


def square(source: Path | bytes) -> bytes:
    """source (an image file, or an image's bytes) as a playlist's picture: JPEG or PNG bytes. Raises PictureError.
    Blocking, 0.25 s for a 24 MP JPEG and 0.7 s for a 12 MP HEIC photo (measured 2026-09-28): the window calls it
    from a worker thread."""
    data = _read(source) if isinstance(source, Path) else source
    _check_header(data)
    try:
        pixbuf = _decode(data)
    except GLib.Error:
        if not isinstance(source, Path):
            raise PictureError(UNREADABLE) from None
        try:
            pixbuf = _decode(_ffmpeg(source))
        except GLib.Error:
            raise PictureError(UNREADABLE) from None
    return _encode(_crop(pixbuf))


def _read(file: Path) -> bytes:
    try:
        size = file.stat().st_size
        if size > MAX_BYTES:
            raise PictureError(f"That picture is too large ({size / 1024 / 1024:.0f} MB; "
                               f"up to {MAX_BYTES // 1024 // 1024} MB).")
        return file.read_bytes()
    except OSError:
        raise PictureError("Could not read that file.") from None


def _check_header(data: bytes) -> None:
    """PictureError when the image's header claims more than MAX_PIXELS. On Linux glycin reads it without decoding
    (10-20 ms): GdkPixbuf's glycin module decodes the whole image whatever size-prepared asks for, so a 190 KB PNG
    of 40000 x 40000 grew Siphon by 6.1 GB, and where that much memory was not to be had it ended Siphon ("memory
    allocation of 4800000000 bytes failed", measured 2026-09-28). Elsewhere _decode refuses it at size-prepared."""
    if Gly is None:
        return
    try:
        image = Gly.Loader.new_for_bytes(GLib.Bytes.new(data)).load()
    except GLib.Error:  # GdkPixbuf, then ffmpeg, say what is wrong with it
        return
    _refuse_over_max(image.get_width(), image.get_height())


def _refuse_over_max(width: int, height: int) -> None:
    if width * height > MAX_PIXELS:
        raise PictureError(f"That picture is too large ({width} × {height}; "
                           f"up to {MAX_PIXELS // 1_000_000} megapixels).")


def _decode(data: bytes) -> GdkPixbuf.Pixbuf:
    """The image in data, upright, its short side at most SIZE. GLib.Error when GdkPixbuf can't read it."""
    loader = GdkPixbuf.PixbufLoader()
    found: list[tuple[int, int]] = []

    def prepared(_loader: GdkPixbuf.PixbufLoader, width: int, height: int) -> None:
        found.append((width, height))
        if width * height > MAX_PIXELS:
            loader.set_size(0, 0)  # GdkPixbuf's own loaders (the Windows build's) then stop before they allocate it
            return
        # Asked for at the size it is kept at: the Windows build's JPEG loader then decodes at a fraction of it.
        # glycin (Linux) decodes whole and shrinks after: a 24 MP photo takes 110 MB for a moment (measured 2026-09-28).
        scale = min(1.0, SIZE / min(width, height))
        loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))

    loader.connect("size-prepared", prepared)
    try:
        try:
            loader.write(data)
        finally:
            loader.close()  # raises too, for data it could make no image of
    except GLib.Error:
        if found:
            _refuse_over_max(*found[0])  # the loader stopped at size 0
        raise
    if found:
        _refuse_over_max(*found[0])
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise GLib.Error("no image")
    return pixbuf.apply_embedded_orientation() or pixbuf


def _ffmpeg(file: Path) -> bytes:
    """file as a PNG, squared and shrunk, by ffmpeg (which turns it upright itself); PictureError when it can't."""
    command = ["ffmpeg", "-v", "error", "-nostdin", "-max_pixels", str(MAX_PIXELS), "-i", str(file),
               "-frames:v", "1", "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale='min(iw,{SIZE})':-1",
               "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        done = subprocess.run(command, capture_output=True, timeout=FFMPEG_SECONDS, creationflags=paths.no_window())
    except (OSError, subprocess.TimeoutExpired):  # no ffmpeg, or a file it chews on for ever
        raise PictureError(UNREADABLE) from None
    if done.returncode != 0 or not done.stdout:
        # "Picture size 9000x9000 exceeds specified max pixel count", or "Picture size 20000x20000 is invalid" for
        # one of 2 GB or more, which ffmpeg refuses before -max_pixels.
        if size := re.search(rb"Picture size (\d+)x(\d+)", done.stderr):
            _refuse_over_max(int(size[1]), int(size[2]))
        raise PictureError(UNREADABLE)
    return done.stdout


def _crop(pixbuf: GdkPixbuf.Pixbuf) -> GdkPixbuf.Pixbuf:
    width, height = pixbuf.get_width(), pixbuf.get_height()
    side = min(width, height)
    square = pixbuf.new_subpixbuf((width - side) // 2, (height - side) // 2, side, side)
    return square.scale_simple(SIZE, SIZE, GdkPixbuf.InterpType.BILINEAR) if side > SIZE else square


def _encode(pixbuf: GdkPixbuf.Pixbuf) -> bytes:
    if not _opaque(pixbuf):
        _ok, data = pixbuf.save_to_bufferv("png", [], [])
        return data
    if pixbuf.get_has_alpha():  # glycin saves no JPEG from RGBA pixels
        side = pixbuf.get_width()
        rgb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, side, side)
        pixbuf.composite(rgb, 0, 0, side, side, 0, 0, 1, 1, GdkPixbuf.InterpType.NEAREST, 255)
        pixbuf = rgb
    _ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], [str(QUALITY)])
    return data


def _opaque(pixbuf: GdkPixbuf.Pixbuf) -> bool:
    """No pixel is even partly see-through (GIFs and many PNGs carry an alpha channel they never use)."""
    if not pixbuf.get_has_alpha():
        return True
    pixels, stride = pixbuf.read_pixel_bytes().get_data(), pixbuf.get_rowstride()
    width = pixbuf.get_width()
    solid = b"\xff" * width
    return all(pixels[row * stride + 3:row * stride + 4 * width:4] == solid for row in range(pixbuf.get_height()))
