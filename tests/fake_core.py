"""Stand-in for siphon.core with the same API, for running the window offline.

Any link resolves to one track, or five for links containing "playlist".
Downloads fail for links containing "fail"; links containing "unreadable"
fail while being read. Nothing touches the network; "downloads" are empty
files written into the requested folder, and a playlist's picture is a
generated gradient.
"""

import re
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class SiphonError(Exception):
    pass


class Cancelled(SiphonError):
    pass


FORMATS = ("opus", "m4a", "mp3", "flac", "best")
FORMAT_LABELS = {"mp3": "MP3", "m4a": "M4A (AAC)", "opus": "Opus", "flac": "FLAC", "best": "Original"}
FORMAT_NOTES = {
    "opus": "As YouTube sends it, converted from others; not for Apple Music",
    "m4a": "Small files that play almost anywhere, Apple Music included",
    "mp3": "Plays on anything, even old car stereos; files often twice as big",
    "flac": "Sounds like Original, no better, in files often 10 times bigger",
    "best": "Exactly what the site sends; the file type depends on the site",
}
QUALITY_BLOCKS = 4
FORMAT_QUALITY = {"opus": (3, "Excellent"), "m4a": (3, "Excellent"), "mp3": (3, "Excellent"), "flac": (4, "Best"),
                  "best": (4, "Best")}


@dataclass
class Track:
    url: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float | None = None
    cover_url: str = ""
    source: str = ""
    query: str = ""
    index: int | None = None
    track_no: int | None = None
    error: str = ""


@dataclass
class Resolved:
    title: str
    kind: str
    tracks: list[Track] = field(default_factory=list)
    folder: str | None = None
    note: str = ""
    cover_url: str = ""
    link: str = ""


@dataclass
class Progress:
    stage: str
    fraction: float | None
    detail: str = ""


_PLAYLIST = [
    ("Neon Harbour", "Kaito Mori"),
    ("A Very Long Song Title That Keeps Going Well Past The Edge Of Any Reasonable Row Width", "The Extended Mix Collective, Featuring Many Guests"),
    ("Salt & Static", "Lumen Drive"),
    ("Paper Moons", "Ines Calder"),
    ("Low Tide", "Kaito Mori"),
]


def is_supported(text: str) -> bool:
    return bool(re.fullmatch(r"(https?://\S+|spotify:(track|album|playlist):\w+)", text.strip()))


def _source(url: str) -> str:
    for needle, name in (("spotify", "Spotify"), ("soundcloud", "SoundCloud"), ("bandcamp", "Bandcamp"), ("youtu", "YouTube")):
        if needle in url:
            return name
    return "Web"


def resolve(url: str) -> Resolved:
    time.sleep(1.2)
    if "unreadable" in url:
        raise SiphonError("Siphon could not read this link.")
    source = _source(url)
    if "playlist" in url:
        tracks = [
            Track(url=f"{url}#{i}", title=title, artist=artist, source=source, index=i, track_no=i, duration=200.0)
            for i, (title, artist) in enumerate(_PLAYLIST, start=1)
        ]
        return Resolved(title="Late Night Drive & Chill", kind="playlist", tracks=tracks,
                        folder="Late Night Drive & Chill", note="Spotify shows the first 100 tracks of this playlist",
                        cover_url=f"fake://cover/{source_link(url)}")
    slug = re.split(r"[/=?#]", url.rstrip("/"))[-1]
    title = "Me at the zoo" if slug == "jNQXAC9IVRw" else slug.replace("-", " ").replace("_", " ").title()
    track = Track(url=url, title=title, artist="jawed", source=source, duration=19.0)
    return Resolved(title=track.title, kind="track", tracks=[track], folder=None)


def source_link(url: str) -> str:
    return url.strip().split("?")[0].rstrip("/")


def cover_image(urls: list[str]) -> bytes | None:
    """A 300 px PNG gradient whose colours follow the url; None for urls containing "fail"."""
    time.sleep(0.5)
    url = next(filter(None, urls), "")
    if not url or "fail" in url:
        return None
    seed = zlib.crc32(url.encode())
    top, bottom = (seed & 0xFF, seed >> 8 & 0xFF, seed >> 16 & 0xFF), (40, 30, seed >> 24 & 0xFF)
    rows = b"".join(b"\x00" + bytes(round(a + (b - a) * y / 299) for a, b in zip(top, bottom)) * 300
                    for y in range(300))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 300, 300, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled("Download cancelled.")


def download(track: Track, outdir: Path, fmt: str, progress: Callable[[Progress], None] | None = None,
             cancel: threading.Event | None = None) -> Path:
    report = progress or (lambda p: None)
    if track.error:
        raise SiphonError(track.error)
    ext = "opus" if fmt == "best" else fmt
    target = Path(outdir) / f"{track.artist} - {track.title}.{ext}".replace("/", "_")
    if target.exists():
        report(Progress("done", 1.0, "already downloaded"))
        return target
    report(Progress("matching", None))
    _wait(1.0, cancel)
    if "fail" in track.url:
        raise SiphonError("No matching audio was found for this track.")
    for step in range(41):
        report(Progress("downloading", step / 40, f"{step * 2.5:.0f}%"))
        _wait(0.1, cancel)
    report(Progress("converting", None))
    _wait(0.8, cancel)
    report(Progress("tagging", None))
    _wait(0.2, cancel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    report(Progress("done", 1.0))
    return target


def default_outdir() -> Path:
    # Never the real ~/Music: the fake is for development runs only.
    return Path(tempfile.gettempdir()) / "siphon-fake-out"


def engine_version() -> str:
    return "2026.08.19 (fake)"


def update_engine() -> str:
    time.sleep(1.5)
    return "2026.09.25 (fake)"
