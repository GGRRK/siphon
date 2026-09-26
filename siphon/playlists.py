"""Playlists as plain M3U8 files in <music folder>/Playlists.

Other players read the same files: songs inside the music folder are written
relative to the playlist folder (so the folder can move as a whole), anything
outside it keeps its absolute path. The files on disk are the only state, so
edits made by another player show up the next time all() is called. Windows
playlists use backslashes; either separator reads on either system.
"""

import os
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import names, paths
from .library import Song, song_from_file, trash_file, write_atomic

EXT = ".m3u8"
NAME_LIMIT = 200  # bytes of the file name, extension included
_TEMP_ROOM = 14  # write_atomic's temporary file is named ".<name>-xxxxxxxx.tmp"


@dataclass
class Playlist:
    name: str
    file: Path
    paths: list[Path] = field(default_factory=list)

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

    def create(self, name: str) -> Playlist:
        with self._lock:
            pl = Playlist(*self._unique(_clean(name) or "Playlist"))
            self._save(pl)
            return pl

    def rename(self, pl: Playlist, name: str) -> None:
        name = _clean(name)
        if not name or name == pl.name:
            return
        with self._lock:
            old = pl.file
            pl.name, pl.file = self._unique(name, keep=old)
            self._save(pl)  # write the new file before dropping the old one: never lose it
            if pl.file != old:
                old.unlink(missing_ok=True)

    def delete(self, pl: Playlist) -> None:
        with self._lock:
            trash_file(pl.file)

    def add(self, pl: Playlist, paths: list[Path]) -> None:
        with self._lock:
            pl.paths.extend(Path(os.path.abspath(p)) for p in paths)
            self._save(pl)

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
