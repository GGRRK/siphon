"""Embedded cover art, extracted once into <cache>/covers ($XDG_CACHE_HOME/siphon, %LOCALAPPDATA%\\Siphon\\cache).

The cache name hashes the song's path and mtime, so a re-tagged file gets a
fresh image. Files without a cover leave an empty .none marker, which keeps
repeat lookups to a few stats instead of a tag parse.
"""

import base64
import hashlib
import os
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from . import paths
from .library import write_atomic

FRONT_COVER = 3  # picture type in ID3 APIC and FLAC/Vorbis pictures


def covers_dir() -> Path:
    return paths.cache_dir() / "covers"


def cover_file(path: Path) -> Path | None:
    """The song's embedded cover as a .jpg/.png file, or None when it has none."""
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        return None
    folder = covers_dir()
    stem = hashlib.sha1(f"{path}\0{mtime_ns}".encode()).hexdigest()
    for ext in ("jpg", "png"):
        if (hit := folder / f"{stem}.{ext}").is_file():
            return hit
    none_marker = folder / f"{stem}.none"
    if none_marker.exists():
        return None
    try:
        data = _embedded(path)
    except Exception:  # damaged tags raise all kinds of errors: treat them as no cover
        data = None
    ext = _image_ext(data) if data else None
    try:
        if ext is None:
            write_atomic(none_marker, b"")
            return None
        out = folder / f"{stem}.{ext}"
        write_atomic(out, data)
        return out
    except OSError:
        return None


def _embedded(path: Path) -> bytes | None:
    audio = mutagen.File(path)
    if audio is None:
        return None
    if isinstance(audio, FLAC):
        return _pick([(p.type, p.data) for p in audio.pictures])
    tags = audio.tags
    if tags is None:
        return None
    if isinstance(tags, ID3):
        return _pick([(f.type, f.data) for f in tags.getall("APIC")])
    if isinstance(audio, MP4):
        covers = tags.get("covr") or []
        return bytes(covers[0]) if covers else None
    pictures = [Picture(base64.b64decode(b64)) for b64 in tags.get("metadata_block_picture", [])]
    return _pick([(p.type, p.data) for p in pictures])


def _pick(pictures: list[tuple[int, bytes]]) -> bytes | None:
    """The front cover if there is one, else the first picture."""
    for kind, data in pictures:
        if kind == FRONT_COVER:
            return data
    return pictures[0][1] if pictures else None


def _image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None
