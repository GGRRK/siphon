"""Stand-ins for siphon.library, siphon.playlists and siphon.covers, for running the window offline.

Selected with SIPHON_FAKE_CORE=1. The library lists the (usually empty)
audio files under its root and makes up albums and durations from their
names; playlists live in memory; covers are generated gradient PNGs.
`python tests/fake_library.py populate DIR N` fills DIR with N such files.

Environment: SIPHON_FAKE_SCAN_SECONDS (default 0.6) slows the scan down,
SIPHON_FAKE_PLAYLISTS=0 starts without the sample playlists.
"""

import hashlib
import os
import random
import struct
import sys
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac", ".webm"}
_ALBUMS = ["Night Drive", "Coastal Lines", "Paper Moons", "Signals", "Afterglow", "Café Nocturne", "", "Low Tide"]


@dataclass(frozen=True)
class Song:
    path: Path
    title: str
    artist: str
    album: str
    duration: float
    track_no: int | None
    added: float
    size: int

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown artist"

    @property
    def key(self) -> str:
        return str(self.path)


def _digest(text: str) -> int:
    return int.from_bytes(hashlib.sha1(text.encode()).digest()[:8], "big")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _song(path: Path) -> Song | None:
    try:
        st = path.stat()
    except OSError:
        return None
    artist, sep, title = path.stem.partition(" - ")
    if not sep:
        artist, title = "", path.stem
    seed = _digest(path.name)
    return Song(path=path, title=title, artist=artist, album=_ALBUMS[seed % len(_ALBUMS)],
                duration=float(95 + seed % 330), track_no=None, added=st.st_mtime, size=st.st_size)


class Library:
    def __init__(self, root: Path, cache_file: Path | None = None) -> None:
        self.root = root
        self._songs: dict[Path, Song] = {}

    def set_root(self, root: Path) -> None:
        self.root = root
        self._songs.clear()

    def scan(self) -> list[Song]:
        time.sleep(float(os.environ.get("SIPHON_FAKE_SCAN_SECONDS", "0.6")))
        found: dict[Path, Song] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "Playlists"]
            for name in filenames:
                path = Path(dirpath) / name
                if not name.startswith(".") and path.suffix.lower() in AUDIO_EXTS and (song := _song(path)):
                    found[path] = song
        self._songs = found
        return self.songs()

    def songs(self) -> list[Song]:
        return sorted(self._songs.values(), key=lambda s: s.added, reverse=True)

    def get(self, path: Path) -> Song | None:
        return self._songs.get(path)

    def add(self, path: Path) -> Song | None:
        song = _song(path)
        if song:
            self._songs[path] = song
        return song

    def forget(self, path: Path) -> None:
        self._songs.pop(path, None)

    def trash(self, song: Song) -> None:
        song.path.unlink(missing_ok=True)  # the fake never touches a real trash can
        self.forget(song.path)

    def search(self, text: str, songs: list[Song] | None = None) -> list[Song]:
        words = _fold(text).split()
        pool = self.songs() if songs is None else songs
        return [s for s in pool if all(w in _fold(f"{s.title} {s.artist} {s.album}") for w in words)]


@dataclass
class Playlist:
    name: str
    file: Path
    paths: list[Path]
    cover: Path | None = None
    source: str = ""


class Playlists:
    def __init__(self, folder: Path, root: Path | None = None, lookup=None) -> None:
        self.folder = folder
        self._lists: list[Playlist] = []
        if os.environ.get("SIPHON_FAKE_PLAYLISTS", "1") != "0":
            self._seed()

    def _seed(self) -> None:
        files = sorted((p for p in self.folder.parent.rglob("*") if p.suffix in AUDIO_EXTS),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return
        self.add(self.create("Evening Chill"), files[3:17])
        self.add(self.create("Workout"), files[20:52])
        self.add(self.create("old favourites"), files[60:66] + [self.folder.parent / "Gone - Deleted Song.mp3"])

    def all(self) -> list[Playlist]:
        return sorted(self._lists, key=lambda pl: pl.name.casefold())

    def _unique(self, name: str, skip: Playlist | None = None) -> str:
        taken = {pl.name.casefold() for pl in self._lists if pl is not skip}
        candidate, n = name, 2
        while candidate.casefold() in taken:
            candidate, n = f"{name} {n}", n + 1
        return candidate

    def create(self, name: str, source: str = "") -> Playlist:
        name = self._unique(name.strip() or "Playlist")
        pl = Playlist(name, self.folder / f"{name}.m3u8", [], source=source)
        self._lists.append(pl)
        return pl

    def link(self, name: str, source: str):
        from siphon.playlists import LinkedPlaylist  # the real ordering, over these in-memory playlists

        found = next((pl for pl in self._lists if pl.source == source), None)
        return LinkedPlaylist(self, found or self.create(name, source), created=found is None)

    def rename(self, pl: Playlist, name: str) -> None:
        pl.name = self._unique(name.strip() or pl.name, skip=pl)
        pl.file = self.folder / f"{pl.name}.m3u8"

    def delete(self, pl: Playlist) -> None:
        self._lists.remove(pl)

    def add(self, pl: Playlist, paths: list[Path]) -> None:
        pl.paths.extend(paths)

    def insert(self, pl: Playlist, index: int, paths: list[Path]) -> None:
        pl.paths[index:index] = paths

    def set_cover(self, pl: Playlist, data: bytes) -> bool:
        """Pictures go to the cache folder the generated covers use: nothing is written beside the music."""
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "siphon-fake-covers"
        base.mkdir(parents=True, exist_ok=True)
        pl.cover = base / f"playlist-{_digest(pl.source or pl.name)}.png"
        pl.cover.write_bytes(data)
        return True

    def remove(self, pl: Playlist, index: int) -> None:
        del pl.paths[index]

    def move(self, pl: Playlist, src: int, dst: int) -> None:
        pl.paths.insert(dst, pl.paths.pop(src))

    def drop_path(self, path: Path) -> None:
        for pl in self._lists:
            pl.paths = [p for p in pl.paths if p != path]


def _png(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    rows = bytearray()
    for y in range(height):
        t = y / (height - 1)
        pixel = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom))
        rows += b"\x00" + pixel * width

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b"")


def cover_file(path: Path) -> Path | None:
    """Three songs in four get one of eight generated covers; a short sleep stands in for tag reading."""
    time.sleep(0.01)
    seed = _digest(path.name)
    if seed % 4 == 0:
        return None
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "siphon-fake-covers"
    target = base / f"{seed % 8}.png"
    if not target.exists():
        rng = random.Random(seed % 8)
        base.mkdir(parents=True, exist_ok=True)
        colours = [tuple(rng.randrange(30, 230) for _ in range(3)) for _ in range(2)]
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(_png(300, 300, *colours))
        tmp.replace(target)
    return target


_ARTISTS = ["Kaito Mori", "Lumen Drive", "Ines Calder", "Björk Sørensen", "The Extended Mix Collective",
            "Nadia Okafor", "Paper Kites", "Élan Vital", "DJ Q", "Marlow & The Tides"]
_WORDS = ["Neon", "Harbour", "Salt", "Static", "Moon", "Tide", "Glass", "River", "Echo", "Velvet", "Winter",
          "Garden", "Signal", "Ghost", "Summer", "Lights", "Fever", "Dream", "Paper", "Ocean", "Café", "Ember"]


def populate(root: Path, count: int) -> None:
    rng = random.Random(7)
    now = time.time()
    for i in range(count):
        title = " ".join(rng.sample(_WORDS, rng.choice((1, 2, 2, 3))))
        if i % 37 == 5:
            title += " (A Very Long Extended Version That Keeps Going Past Any Reasonable Row Width)"
        artist = rng.choice(_ARTISTS)
        folder = root / artist if i % 5 == 0 else root
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{artist} - {title}{rng.choice(sorted(AUDIO_EXTS))}"
        path.touch()
        stamp = now - i * 3600 * 7
        os.utime(path, (stamp, stamp))


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "populate":
        sys.exit("usage: fake_library.py populate DIR COUNT")
    populate(Path(sys.argv[2]), int(sys.argv[3]))
