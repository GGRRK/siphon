"""Playlists as plain M3U8 files in <music folder>/Playlists.

Other players read the same files: songs inside the music folder are written
relative to the playlist folder (so the folder can move as a whole), anything
outside it keeps its absolute path. The files on disk are the only state, so
edits made by another player show up the next time all() is called. Windows
playlists use backslashes; either separator reads on either system.

Two more '#' lines, which other players skip: #EXTIMG: names the playlist's
picture, an image Siphon keeps beside the file under the same name (Road
Trip.jpg beside Road Trip.m3u8; renamed and trashed with it), and
#SIPHON-SOURCE: the link of the playlist it was made from (core.source_link),
so pasting that link again adds to it instead of making another.
"""

import os
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import names, paths
from .covers import image_ext
from .library import Song, song_from_file, trash_file, write_atomic

EXT = ".m3u8"
IMAGE = "#EXTIMG:"
SOURCE = "#SIPHON-SOURCE:"
NAME_LIMIT = 200  # bytes of the file name, extension included
_TEMP_ROOM = 14  # write_atomic's temporary file is named ".<name>-xxxxxxxx.tmp"


@dataclass
class Playlist:
    name: str
    file: Path
    paths: list[Path] = field(default_factory=list)
    cover: Path | None = None  # its picture (#EXTIMG), which may be missing
    source: str = ""  # the link it was made from (#SIPHON-SOURCE)

    def missing(self) -> set[Path]:
        """Entries whose file is gone (renamed, moved or deleted): listed but not playable."""
        return {p for p in self.paths if not p.is_file()}


class Playlists:
    def __init__(self, folder: Path, root: Path | None = None,
                 lookup: Callable[[Path], Song | None] | None = None) -> None:
        """folder: usually <root>/Playlists. root: the music folder (default: folder's
        parent). lookup: Library.get, to label entries without reading their tags."""
        self.folder = Path(os.path.abspath(folder))
        self.root = Path(os.path.abspath(root)) if root else self.folder.parent
        self._lookup = lookup
        self._labels: dict[Path, tuple[int, str]] = {}  # #EXTINF data seen in files, reused on save
        self._lock = threading.RLock()

    def all(self) -> list[Playlist]:
        """Every playlist, sorted by name; fresh objects on each call."""
        with self._lock:
            try:
                files = [f for f in self.folder.iterdir()
                         if f.suffix.lower() == EXT and not f.name.startswith(".") and f.is_file()]
            except OSError:
                return []
            lists = [pl for pl in map(self._read, files) if pl is not None]
        return sorted(lists, key=lambda pl: (pl.name.casefold(), pl.name))

    def create(self, name: str, source: str = "") -> Playlist:
        with self._lock:
            pl = Playlist(*self._unique(_clean(name) or "Playlist"), source=_clean(source))
            self._save(pl)
            return pl

    def link(self, name: str, source: str) -> "LinkedPlaylist":
        """The playlist made from the link source before, whatever it is called now, else a new one called name."""
        with self._lock:
            found = next((pl for pl in self.all() if pl.source == source), None) if source else None
            return LinkedPlaylist(self, found or self.create(name, source), created=found is None)

    def rename(self, pl: Playlist, name: str) -> None:
        name = _clean(name)
        if not name or name == pl.name:
            return
        with self._lock:
            old, old_cover = pl.file, self._own_cover(pl)
            pl.name, pl.file = self._unique(name, keep=old)
            if pl.file == old:
                self._save(pl)
                return
            if old_cover is not None and (target := self._cover_target(pl, old_cover.suffix, None)):
                try:
                    write_atomic(target, old_cover.read_bytes())
                    pl.cover = target
                except OSError:
                    pass  # the picture stays under the old name, still named in the file: never fail a rename for it
            self._save(pl)  # write the new files before dropping the old ones: never lose them
            old.unlink(missing_ok=True)
            if old_cover is not None and pl.cover != old_cover:
                old_cover.unlink(missing_ok=True)

    def delete(self, pl: Playlist) -> None:
        with self._lock:
            cover = self._own_cover(pl)
            trash_file(pl.file)
            if cover is not None:
                trash_file(cover)

    def add(self, pl: Playlist, paths: list[Path]) -> None:
        self.insert(pl, len(pl.paths), paths)

    def insert(self, pl: Playlist, index: int, paths: list[Path]) -> None:
        """Put paths in before entry index."""
        with self._lock:
            pl.paths[index:index] = [Path(os.path.abspath(p)) for p in paths]
            self._save(pl)

    def set_cover(self, pl: Playlist, data: bytes) -> bool:
        """Keep data, a JPEG or PNG, as pl's picture beside its file. False when it is neither, or can't be
        written: a playlist without its picture still works."""
        ext = image_ext(data)
        if ext is None:
            return False
        with self._lock:
            old = self._own_cover(pl)
            target = self._cover_target(pl, f".{ext}", old)
            if target is None:
                return False
            try:
                write_atomic(target, data)
            except OSError:
                return False
            pl.cover = target
            self._save(pl)
            if old is not None and old != target:
                old.unlink(missing_ok=True)
            return True

    def remove(self, pl: Playlist, index: int) -> None:
        with self._lock:
            del pl.paths[index]
            self._save(pl)

    def move(self, pl: Playlist, src: int, dst: int) -> None:
        """Move entry src so that it ends up at index dst."""
        with self._lock:
            pl.paths.insert(dst, pl.paths.pop(src))
            self._save(pl)

    def drop_path(self, path: Path) -> None:
        """Take a song out of every playlist (it was deleted)."""
        path = Path(os.path.abspath(path))
        with self._lock:
            for pl in self.all():
                if path in pl.paths:
                    pl.paths = [p for p in pl.paths if p != path]
                    self._save(pl)

    # ------------------------------------------------------------ files

    def _unique(self, name: str, keep: Path | None = None) -> tuple[str, Path]:
        """name, or "name 2", "name 3"... so that neither the name nor its file clashes."""
        others = [pl for pl in self.all() if pl.file != keep]
        taken = {pl.name.casefold() for pl in others} | {pl.file.name.casefold() for pl in others}
        n = 1
        while True:
            candidate = name if n == 1 else f"{name} {n}"
            file = self.folder / (self._fit(_file_stem(candidate)) + EXT)
            clash = file != keep and (file.exists() or file.name.casefold() in taken)
            if candidate.casefold() not in taken and not clash:
                return candidate, file
            n += 1

    def _own_cover(self, pl: Playlist) -> Path | None:
        """pl's picture when it is the one Siphon keeps for it: beside the file and named after it. Any other
        picture the file names (a user's, another player's) stays where it is: never moved, never trashed."""
        cover = pl.cover
        if cover is None or cover.parent != self.folder or not cover.is_file():
            return None
        pattern = rf"{re.escape(pl.file.stem)}(?: \(\d+\))?\.(?:jpg|png)"
        return cover if re.fullmatch(pattern, cover.name, re.IGNORECASE if paths.windows() else 0) else None

    def _cover_target(self, pl: Playlist, suffix: str, own: Path | None) -> Path | None:
        """Where pl's picture goes: "Name.jpg" beside "Name.m3u8", or "Name (2).jpg"... when a file Siphon did
        not write for pl has that name. None when the name would not fit (Windows' MAX_PATH)."""
        n = 1
        while True:
            stem = pl.file.stem if n == 1 else f"{pl.file.stem} ({n})"
            if self._fit(stem) != stem:
                return None
            target = self.folder / f"{stem}{suffix.lower()}"
            if target == own or not os.path.lexists(target):
                return target
            n += 1

    def _fit(self, stem: str) -> str:
        """On Windows, stem shortened so the playlist's path (and its temporary twin) stays under MAX_PATH."""
        if not paths.windows():
            return stem
        return names.shorten(stem, names.room(self.folder) - len(EXT) - _TEMP_ROOM) or "Playlist"

    def _read(self, file: Path) -> Playlist | None:
        try:
            text = file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None
        pl = Playlist(file.stem, file)
        info: tuple[int, str] | None = None
        for line in (raw.strip() for raw in text.splitlines()):
            if line.startswith("#PLAYLIST:"):
                pl.name = _clean(line[len("#PLAYLIST:"):]) or pl.name
            elif line.startswith(IMAGE):
                image = line[len(IMAGE):].strip()
                pl.cover = self._resolve(image) if image else None
            elif line.startswith(SOURCE):
                pl.source = line[len(SOURCE):].strip()
            elif line.startswith("#EXTINF:"):
                info = _parse_extinf(line)
            elif line and not line.startswith("#"):
                path = self._resolve(line)
                if path is not None:
                    pl.paths.append(path)
                    if info is not None:
                        self._labels[path] = info
                info = None
        return pl

    def _resolve(self, entry: str) -> Path | None:
        if entry.startswith("file://"):
            entry = file_uri_path(entry)
        elif re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://", entry):
            return None  # a stream URL: not a song in the library
        path = Path(os.path.normpath(self.folder / Path(entry).expanduser()))
        if "\\" in entry and not paths.windows() and not path.exists():
            # written on Windows (a shared music folder): a backslash is a legal character in a Linux name
            path = Path(os.path.normpath(self.folder / entry.replace("\\", "/")))
        return path

    def _save(self, pl: Playlist) -> None:
        lines = ["#EXTM3U", f"#PLAYLIST:{pl.name}"]
        if pl.cover is not None:
            lines.append(IMAGE + self._entry(pl.cover))
        if pl.source:
            lines.append(SOURCE + pl.source)
        for path in pl.paths:
            seconds, label = self._label(path)
            lines += [f"#EXTINF:{seconds},{label}", self._entry(path)]
        write_atomic(pl.file, "\n".join(lines) + "\n")

    def _entry(self, path: Path) -> str:
        if path.is_relative_to(self.root):
            entry = os.path.relpath(path, self.folder)
            return entry.replace("/", "\\") if paths.windows() else entry  # the separator Windows players expect
        return str(path)

    def _label(self, path: Path) -> tuple[int, str]:
        song = self._lookup(path) if self._lookup else None
        if song is None and path in self._labels:
            return self._labels[path]
        song = song or song_from_file(path)
        if song is None:
            info = (-1, path.stem)
        else:
            name = f"{song.artist} - {song.title}" if song.artist else song.title
            info = (round(song.duration) if song.duration else -1, _clean(name))
        self._labels[path] = info
        return info


class LinkedPlaylist:
    """The playlist a playlist link made, filled as the link's songs finish downloading.

    Songs keep the source playlist's order whichever finishes first: each goes after the nearest earlier song
    of the source that is in the playlist, else before the nearest later one. So a song retried later still
    finds its place, and what the user did meanwhile (moved, removed or added songs, renamed it) stays. A song
    already in it, from an earlier paste of the link, is not added again. Once it is deleted, it stays deleted.
    """

    def __init__(self, playlists: Playlists, playlist: Playlist, created: bool) -> None:
        self.created = created
        self._playlists = playlists
        self._file = playlist.file
        self._source = playlist.source
        self._placed: dict[int, Path] = {}  # source position -> song, for every song accounted for
        self._gone = False

    @property
    def gone(self) -> bool:
        """The playlist was deleted (as last seen)."""
        return self._gone

    def playlist(self) -> Playlist | None:
        """The playlist as its file is now - found by its source after a rename; None once it was deleted."""
        if self._gone:
            return None
        lists = self._playlists.all()
        # By file first: another player that saves it may drop the #SIPHON-SOURCE line.
        found = next((pl for pl in lists if pl.file == self._file), None) \
            or next((pl for pl in lists if self._source and pl.source == self._source), None)
        if found is None:
            self._gone = True
            return None
        self._file = found.file
        return found

    def add(self, songs: dict[int, Path]) -> bool:
        """Put finished songs (position in the source -> file) in their places; False once the playlist is gone."""
        pl = self.playlist()
        if pl is None:
            return False
        for index, path in sorted(songs.items()):
            path = Path(os.path.abspath(path))
            if index in self._placed:
                continue
            # The source may list a song twice: only copies beyond the ones already accounted for are new.
            known = sum(p == path for p in self._placed.values())
            if pl.paths.count(path) <= known:
                self._playlists.insert(pl, _slot(pl.paths, self._placed, index), [path])
            self._placed[index] = path
        return True

    def set_cover(self, data: bytes) -> bool:
        pl = self.playlist()
        return pl is not None and self._playlists.set_cover(pl, data)


def _slot(paths: list[Path], placed: dict[int, Path], index: int) -> int:
    """Where the source's song index goes in paths: after the nearest earlier placed song still there, else
    before the nearest later one, else at the end. Copies of one song pair up with its placements in order."""
    spots: dict[Path, list[int]] = {}
    for position, path in enumerate(paths):
        spots.setdefault(path, []).append(position)
    where = {}
    for i in sorted(placed):
        if spots.get(placed[i]):
            where[i] = spots[placed[i]].pop(0)
    earlier = [i for i in where if i < index]
    if earlier:
        return where[max(earlier)] + 1
    later = [i for i in where if i > index]
    return where[min(later)] if later else len(paths)


def _clean(text: str) -> str:
    """One line of text: a newline in a name would break the file format."""
    return " ".join(text.split())


def file_uri_path(uri: str) -> str:
    """The local path of a file:// URI: file:///C:/Music/a%20b.mp3 -> C:/Music/a b.mp3 on Windows."""
    parts = urllib.parse.urlsplit(uri)
    path = urllib.parse.unquote(parts.path)
    if not paths.windows():
        return path
    if re.fullmatch(r"[A-Za-z]:", parts.netloc):  # file://C:/Music/a.mp3, a common malformed form
        return parts.netloc + path
    if re.match(r"/[A-Za-z]:", path):
        return path[1:]
    if parts.netloc not in ("", "localhost"):
        return f"//{parts.netloc}{path}"  # a network share, \\server\share\...
    return path


def _file_stem(name: str) -> str:
    if paths.windows():
        name = names.windows_chars(name)
    text = re.sub(r"[\x00-\x1f\x7f/]", "-", name).lstrip(". ")
    if paths.windows():
        text = names.unreserved(text)
    text = text.encode()[:NAME_LIMIT - len(EXT)].decode("utf-8", "ignore").rstrip(" .")
    return text or "Playlist"


def _parse_extinf(line: str) -> tuple[int, str]:
    head, _, label = line[len("#EXTINF:"):].partition(",")
    seconds = head.split()[0] if head.split() else ""
    try:
        return int(float(seconds)), label.strip()
    except ValueError:
        return -1, label.strip()
