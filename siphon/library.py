"""The music folder as a list of songs.

Tags are read once with mutagen and kept in a versioned JSON cache
(library.json in Siphon's cache folder, see paths.py), so a rescan of an unchanged folder is
only a directory walk plus one stat per file, and the library shows up at once
after a restart, before the first rescan finishes.

scan() is meant to run in a worker thread while the UI keeps reading songs():
all shared state sits behind one lock and readers always get copies.
"""

import json
import ntpath
import os
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
from gi.repository import Gio, GLib
from mutagen.id3 import ID3

from . import paths

if TYPE_CHECKING:
    from .playlists import Playlists

AUDIO_EXTS = frozenset({"mp3", "m4a", "opus", "ogg", "flac", "wav", "aac", "webm"})
PLAYLISTS_DIR = "Playlists"
CACHE_VERSION = 1

# Raw ID3 frames for formats whose tags mutagen has no "easy" wrapper for (WAV, AIFF).
_FILE_ATTRIBUTE_HIDDEN = 0x2  # Windows' hidden flag, in stat_result.st_file_attributes
_ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2",
               "album": "TALB", "tracknumber": "TRCK"}


@dataclass(frozen=True)
class Song:
    path: Path
    title: str
    artist: str
    album: str
    duration: float  # seconds, 0 when unknown
    track_no: int | None
    added: float  # file mtime
    size: int

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown artist"

    @property
    def key(self) -> str:
        return str(self.path)

    @cached_property
    def _haystack(self) -> str:
        return fold(f"{self.title}\n{self.artist}\n{self.album}")


def fold(text: str) -> str:
    """Case- and accent-insensitive form: 'Beyoncé' and 'BEYONCE' fold alike."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def cache_path() -> Path:
    return paths.cache_dir() / "library.json"


def is_audio(name: str) -> bool:
    return os.path.splitext(name)[1][1:].lower() in AUDIO_EXTS


def song_from_file(path: Path) -> Song | None:
    """Stat and read the tags of one file; None when it is missing."""
    try:
        st = path.stat()
    except OSError:
        return None
    return _read_song(path, st.st_mtime_ns, st.st_size)


def trash_file(path: Path) -> None:
    """Move a file to the desktop trash. Raises OSError (with GLib's reason as its text)
    when that is impossible, e.g. on a filesystem without a trash, rather than deleting
    for good."""
    try:
        Gio.File.new_for_path(str(path)).trash(None)
    except GLib.Error as e:
        # Windows' Recycle Bin reports a missing file as a plain failure, without NOT_FOUND.
        if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND) or not os.path.lexists(path):
            return
        raise OSError(e.message) from None


def write_atomic(path: Path, data: str | bytes) -> None:
    """Replace a file in one step, so a crash never leaves half of it behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Library:
    def __init__(self, root: Path, cache_file: Path | None = None) -> None:
        self.root = _absolute(root)
        self._cache_file = cache_file or cache_path()
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()  # scans run one at a time
        self._save_lock = threading.Lock()
        # Tag cache: every file whose tags we know, keyed by _ident(path), with its mtime_ns.
        self._cache: dict[str, tuple[int, Song]] = self._load_cache()
        # The library proper: the cached songs under root until the first scan says otherwise.
        self._index: dict[str, Song] = {k: s for k, (_, s) in self._cache.items()
                                        if s.path.is_relative_to(self.root)}
        self._sorted: list[Song] | None = None
        self._gen = 0  # bumped by set_root, so a scan of the old root is thrown away
        self._tick = 0
        self._changes: dict[str, int] = {}  # key -> tick of the last add/forget

    # ------------------------------------------------------------ reading

    def songs(self) -> list[Song]:
        """Newest first. A copy: safe to keep while a scan runs."""
        with self._lock:
            if self._sorted is None:
                self._sorted = sorted(self._index.values(), key=_newest_first)
            return list(self._sorted)

    def get(self, path: Path) -> Song | None:
        with self._lock:
            return self._index.get(_ident(str(_absolute(path))))

    def search(self, text: str, songs: list[Song] | None = None) -> list[Song]:
        """Songs whose title, artist or album contain every word of text."""
        pool = self.songs() if songs is None else songs
        words = fold(text).split()
        return [s for s in pool if all(w in s._haystack for w in words)]

    # ------------------------------------------------------------ changing

    def set_root(self, root: Path) -> None:
        with self._lock:
            self.root = _absolute(root)
            self._index = {}
            self._sorted = None
            self._gen += 1

    def scan(self) -> list[Song]:
        """Walk root and return its songs, newest first. Blocking: call it off the UI thread."""
        with self._scan_lock:
            with self._lock:
                root, gen, start = self.root, self._gen, self._tick
                known = dict(self._cache)
            found: dict[str, tuple[int, Song]] = {}
            dirty = False
            for path, st in _walk(root):
                key = _ident(path)
                hit = known.get(key)
                if hit and hit[0] == st.st_mtime_ns and hit[1].size == st.st_size:
                    found[key] = hit
                else:
                    found[key] = (st.st_mtime_ns, _read_song(Path(path), st.st_mtime_ns, st.st_size))
                    dirty = True
            dirty = dirty or found.keys() != known.keys()
            with self._lock:
                current = gen == self._gen
                if current:
                    # add()/forget() calls made while we walked know better than the walk.
                    for key, tick in self._changes.items():
                        if tick <= start:
                            continue
                        if key in self._index:
                            found[key] = self._cache[key]
                        else:
                            found.pop(key, None)
                    self._changes.clear()
                    self._cache = found
                    self._index = {k: s for k, (_, s) in found.items()}
                    self._sorted = None
            if current and dirty:
                self._save()
            return self.songs()

    def add(self, path: Path) -> Song | None:
        """Take in one new file (a finished download) without a full rescan."""
        path = _absolute(path)
        if not (is_audio(path.name) and path.is_relative_to(self.root)):
            return None
        parts = path.relative_to(self.root).parts
        if _ident(parts[0]) == _ident(PLAYLISTS_DIR) or any(part.startswith(".") for part in parts):
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        song, key = _read_song(path, st.st_mtime_ns, st.st_size), _ident(str(path))
        with self._lock:
            self._cache[key] = (st.st_mtime_ns, song)
            self._index[key] = song
            self._touch(key)
        self._save()
        return song

    def forget(self, path: Path) -> None:
        key = _ident(str(_absolute(path)))
        with self._lock:
            known = (self._cache.pop(key, None), self._index.pop(key, None)) != (None, None)
            self._touch(key)
        if known:
            self._save()

    def trash(self, song: Song, playlists: "Playlists | None" = None) -> None:
        trash_file(song.path)
        self.forget(song.path)
        if playlists is not None:
            playlists.drop_path(song.path)

    # ------------------------------------------------------------ internals

    def _touch(self, key: str) -> None:
        """Record a change under the lock, for a scan running concurrently."""
        self._tick += 1
        self._changes[key] = self._tick
        self._sorted = None

    def _load_cache(self) -> dict[str, tuple[int, Song]]:
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {}
        entries = data.get("songs")
        out: dict[str, tuple[int, Song]] = {}
        for path, row in entries.items() if isinstance(entries, dict) else ():
            try:
                mtime_ns, size, title, artist, album, duration, track_no = row
                song = Song(Path(path), str(title), str(artist), str(album), float(duration),
                            int(track_no) if track_no is not None else None,
                            mtime_ns / 1e9, int(size))
                out[_ident(path)] = (int(mtime_ns), song)
            except (TypeError, ValueError):
                continue
        return out

    def _save(self) -> None:
        with self._save_lock:
            with self._lock:
                rows = {s.key: [m, s.size, s.title, s.artist, s.album, s.duration, s.track_no]
                        for m, s in self._cache.values()}
            text = json.dumps({"version": CACHE_VERSION, "songs": rows},
                              ensure_ascii=False, separators=(",", ":"))
            try:
                write_atomic(self._cache_file, text)
            except OSError:
                pass  # only a cache: the next scan rebuilds it


# ---------------------------------------------------------------- walking and tags


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(path)))


def _ident(path: str) -> str:
    """The identity of a path: Windows paths ignore case and take either slash."""
    return ntpath.normcase(path) if paths.windows() else path


def _newest_first(song: Song) -> tuple[float, str]:
    return (-song.added, song.title.casefold())


def _inside(real: str, real_root: str) -> bool:
    real, real_root = _ident(real), _ident(real_root)
    sep = ntpath.sep if paths.windows() else "/"  # ntpath.sep: '/' in an MSYS2 shell, as normcase makes it
    return real == real_root or real.startswith(real_root.rstrip(sep) + sep)


def _hidden(entry: os.DirEntry) -> bool:
    """Windows' hidden flag (dot-names are hidden everywhere); free there: scandir already read it."""
    return bool((getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) or 0) & _FILE_ATTRIBUTE_HIDDEN)


def _walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """(path, stat) of every audio file under root, skipping hidden entries (download
    work dirs, and on Windows anything flagged hidden), the Playlists folder and symlinks
    that lead outside root."""
    top_dir, real_root, windows = str(root), os.path.realpath(root), paths.windows()
    seen = {real_root}  # real paths of walked dirs: no loops, no dir walked twice
    stack = [(top_dir, real_root)]
    while stack:
        top, real_top = stack.pop()
        try:
            entries = os.scandir(top)
        except OSError:
            continue
        with entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") or (top == top_dir and _ident(name) == _ident(PLAYLISTS_DIR)):
                    continue
                try:
                    if windows and _hidden(entry):
                        continue
                    link = entry.is_symlink()
                    real = os.path.realpath(entry.path) if link else os.path.join(real_top, name)
                    if link and not _inside(real, real_root):
                        continue
                    if entry.is_dir():
                        if real not in seen:
                            seen.add(real)
                            stack.append((entry.path, real))
                    elif is_audio(name) and entry.is_file():
                        yield entry.path, entry.stat()
                except OSError:
                    continue  # vanished or unreadable mid-walk


def _read_song(path: Path, mtime_ns: int, size: int) -> Song:
    tags: dict[str, str] = {}
    duration = 0.0
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # mutagen raises many kinds of errors on damaged files; never fail a scan
        audio = None
    if audio is not None:
        duration = float(getattr(audio.info, "length", 0) or 0)
        if audio.tags is not None:
            tags = _tag_values(audio.tags)
    title, artist = tags.get("title", ""), tags.get("artist") or tags.get("albumartist", "")
    left, sep, right = (part.strip() for part in path.stem.partition(" - "))
    named = bool(sep and left and right)  # Siphon names its files "Artist - Title"
    if not title:
        title = right if named else (path.stem.strip() or path.name)
        artist = artist or (left if named else "")
    elif not artist and named and right == title:
        artist = left
    return Song(path, title, artist, tags.get("album", ""), duration,
                _track_no(tags.get("tracknumber", "")), mtime_ns / 1e9, size)


def _tag_values(tags) -> dict[str, str]:
    out = {}
    for field, frame in _ID3_FRAMES.items():
        if isinstance(tags, ID3):
            values = tags[frame].text if frame in tags else []
        else:
            values = tags.get(field) or []
        text = next((str(v).replace("\x00", " ").strip() for v in values if str(v).strip()), "")
        if text:
            out[field] = text
    return out


def _track_no(text: str) -> int | None:
    head = text.split("/", 1)[0].strip()
    return int(head) if head.isdigit() and int(head) > 0 else None
