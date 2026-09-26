"""What the music pages share: a snapshot of the library, the playlists and the player.

The library scans in a worker thread; the pages only ever read `songs`,
the snapshot the last scan produced, and hear about changes through the
"library-changed" and "playlists-changed" signals.
"""

import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import GLib, GObject

from .fmt import split_stem

PLAYLISTS_DIR = "Playlists"


class Music(GObject.Object):
    __gsignals__ = {
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, library: Any, playlists_for: Callable[[Path], Any], player: Any,
                 cover_file: Callable[[Path], Path | None], song_type: type) -> None:
        super().__init__()
        self.library = library
        self.player = player
        self.cover_file = cover_file
        self._playlists_for = playlists_for
        self._song_type = song_type
        self.playlists = playlists_for(library.root / PLAYLISTS_DIR)
        self.songs: list = []
        self._by_path: dict[Path, Any] = {}
        self._set_songs(library.songs())  # what the last run knew, shown before the first scan ends
        self.scanning = False
        self._scan_again = False
        self._next_root: Path | None = None  # a folder change waiting for the running scan
        self._arrived: list[Path] = []  # downloads that finished during a scan

    @property
    def root(self) -> Path:
        return self._next_root or self.library.root

    # -- library

    def rescan(self) -> None:
        if self.scanning:
            self._scan_again = True
            return
        self.scanning = True
        self.emit("library-changed")
        threading.Thread(target=self._scan, name="siphon-scan", daemon=True).start()

    def _scan(self) -> None:  # worker thread
        try:
            result: list | str = self.library.scan()
        except Exception as exc:
            traceback.print_exception(exc)
            result = f"Could not read your music folder ({exc})."
        GLib.idle_add(self._scanned, result)

    def _scanned(self, result: list | str) -> bool:
        self.scanning = False
        if self._next_root is not None:  # the result is for the old folder
            self.library.set_root(self._next_root)
            self._next_root = None
            self._set_songs([])
            self._scan_again = True
        elif isinstance(result, str):
            self.emit("error", result)
        else:
            self._set_songs(result)
        arrived, self._arrived = self._arrived, []
        for path in arrived:
            self._add(path)
        if self._scan_again:
            self._scan_again = False
            self.rescan()
        else:
            self.emit("library-changed")
        return GLib.SOURCE_REMOVE

    def set_root(self, root: Path) -> None:
        if root == self.root:
            return
        self.playlists = self._playlists_for(root / PLAYLISTS_DIR)
        self.emit("playlists-changed")
        if self.scanning:
            self._next_root = root
            self._scan_again = True
            return
        self.library.set_root(root)
        self._set_songs([])
        self.rescan()

    def add_file(self, path: Path) -> None:
        """A download finished: show it at once instead of waiting for a rescan."""
        if self.scanning:
            self._arrived.append(path)
        elif self._add(path):
            self.emit("library-changed")

    def _add(self, path: Path) -> bool:
        if not path.is_relative_to(self.library.root):
            return False
        try:
            song = self.library.add(path)
        except Exception as exc:
            traceback.print_exception(exc)
            return False
        if song is None:
            return False
        self.songs = [song] + [s for s in self.songs if s.path != path]
        self._by_path[path] = song
        return True

    def trash(self, song: Any) -> bool:
        try:
            self.library.trash(song)
        except (OSError, GLib.Error) as exc:
            self.emit("error", f"Could not move “{song.title}” to the Trash ({getattr(exc, 'message', exc)}).")
            return False
        self._set_songs([s for s in self.songs if s.path != song.path])
        self.emit("library-changed")
        self.change_playlists(self.playlists.drop_path, song.path)
        return True

    def _set_songs(self, songs: list) -> None:
        self.songs = songs
        self._by_path = {song.path: song for song in songs}

    def song_at(self, path: Path) -> Any:
        """The library's song for a file, or one made up from its name (a playlist can point outside the library)."""
        song = self._by_path.get(path)
        if song is None:
            artist, title = split_stem(path.stem)
            song = self._song_type(path=path, title=title, artist=artist, album="", duration=0.0,
                                   track_no=None, added=0.0, size=0)
        return song

    # -- playlists

    def change_playlists(self, change: Callable[..., Any], *args: Any) -> Any:
        """Run one playlist change; failures to save become an "error" instead of an exception."""
        try:
            result = change(*args)
        except OSError as exc:
            self.emit("error", f"Could not save the playlist ({exc.strerror or exc}).")
            result = None
        self.emit("playlists-changed")
        return result

    def entries(self, playlist: Any) -> list[tuple[Any, bool]]:
        """(song, missing) for every entry, in order; missing files stay listed."""
        return [(self.song_at(path), not path.is_file()) for path in playlist.paths]

    def find_playlist(self, file: Path) -> Any:
        return next((pl for pl in self.playlists.all() if pl.file == file), None)
