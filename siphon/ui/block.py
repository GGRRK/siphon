"""Everything one pasted link turns into: a single row, or a titled group of rows."""

import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from gi.repository import Adw, GLib, Gtk

from .downloader import Downloader, Job, unexpected
from .music import Music
from .row import ACTIVE, FINISHED, State, TrackRow
from .songmenu import quoted

# Songs that finish within this long of each other join the playlist in one save and one redraw:
# a playlist pasted again reports its "already downloaded" songs in a quick burst.
_FILL_DELAY_MS = 250


class Host(Protocol):
    core: ModuleType
    downloader: Downloader
    music: Music

    def changed(self) -> None: ...
    def report_error(self, message: str) -> None: ...
    def show_file(self, path: Path) -> None: ...
    def toast(self, message: str, button: str | None = None,
              on_button: Callable[[], None] | None = None) -> Adw.Toast: ...
    def open_playlist(self, playlist: Any) -> None: ...


def short_url(url: str) -> str:
    for prefix in ("https://", "http://", "www."):
        url = url.removeprefix(prefix)
    return url


class LinkBlock(Adw.PreferencesGroup):
    def __init__(self, url: str, fmt: str, outdir: Path, host: Host) -> None:
        super().__init__()
        self.url = url
        self._fmt = fmt
        self._outdir = outdir
        self._host = host
        self._rows: list[TrackRow] = []
        self._jobs: dict[TrackRow, Job] = {}
        self._read_token: object | None = None
        self._group_title: str | None = None  # set once the link turns out to be an album or playlist
        self._saved = False
        self._linked: Any = None  # playlists.LinkedPlaylist, for a playlist link
        self._arrived: dict[int, Path] = {}  # finished songs of a playlist link not in its playlist yet
        self._fill_source = 0
        self._picture: bytes | None = None  # the album's or playlist's picture, squared
        self._fetching = False
        self._cancel_all = Gtk.Button(label="Cancel All", valign=Gtk.Align.CENTER, visible=False)
        self._cancel_all.add_css_class("flat")
        self._cancel_all.connect("clicked", lambda _button: self.cancel_all())
        self._save = Gtk.Button(label="Save as Playlist", valign=Gtk.Align.CENTER, visible=False)
        self._save.add_css_class("flat")
        self._save.connect("clicked", self._on_save)
        self._open = Gtk.Button(label="Open Playlist", valign=Gtk.Align.CENTER, visible=False)
        self._open.add_css_class("flat")
        self._open.connect("clicked", lambda _button: self._open_playlist())

        self._reader = self._add_row(short_url(url))
        self._read()

    # -- state the window asks about

    @property
    def unfinished(self) -> int:
        return sum(row.state in ACTIVE for row in self._rows)

    @property
    def has_finished(self) -> bool:
        return any(row.state in FINISHED for row in self._rows)

    @property
    def is_empty(self) -> bool:
        return not self._rows

    def sync(self) -> None:
        grouped = self._group_title is not None
        self._cancel_all.set_visible(grouped and self.unfinished > 0)
        self._open.set_visible(self._linked is not None and not self._linked.gone)
        # A playlist link has its playlist already; an album waits for its picture so the saved playlist gets it.
        self._save.set_visible(grouped and self._linked is None and not self._saved and not self.unfinished
                               and not self._fetching and bool(self._files()))

    # -- actions

    def cancel_all(self) -> None:
        for row in list(self._rows):
            if row.state in ACTIVE:
                self._cancel(row)

    def clear_finished(self) -> None:
        for row in [r for r in self._rows if r.state in FINISHED]:
            self._remove_row(row)

    def _on_row_action(self, row: TrackRow) -> None:
        if row.state in ACTIVE:
            self._cancel(row)
        elif row.state in (State.DONE, State.ALREADY):
            self._host.show_file(self._jobs[row].path)
        elif row in self._jobs:
            self._host.downloader.submit(self._jobs[row])
        else:
            self._read()

    def _files(self) -> list[Path]:
        """The downloaded files, in the album's or playlist's order."""
        return [self._jobs[row].path for row in self._rows
                if row in self._jobs and row.state in (State.DONE, State.ALREADY)]

    def _on_save(self, _button: Gtk.Button) -> None:
        music = self._host.music
        playlists, files, picture = music.playlists, self._files(), self._picture

        def save() -> Any:
            playlist = playlists.create(self._group_title)
            playlists.add(playlist, files)
            if picture:
                playlists.set_cover(playlist, picture)
            return playlist

        playlist = music.change_playlists(save)
        if playlist is None:
            return
        self._host.toast(f"Saved the playlist {quoted(playlist.name)}", "Open",
                         lambda: self._host.open_playlist(playlist))
        self._saved = True
        self.sync()

    def _open_playlist(self) -> None:
        playlist = self._linked.playlist()
        if playlist is None:
            self._host.toast("That playlist was deleted.")
        else:
            self._host.open_playlist(playlist)
        self.sync()

    def _cancel(self, row: TrackRow) -> None:
        if row in self._jobs:
            self._host.downloader.cancel(self._jobs[row])
        else:  # still reading the link; the result is dropped when it arrives
            self._read_token = None
            row.set_state(State.CANCELLED)

    # -- reading the link

    def _read(self) -> None:
        token = self._read_token = object()
        self._reader.set_state(State.READING)
        threading.Thread(target=self._resolve, args=(token,), name="siphon-resolve", daemon=True).start()

    def _resolve(self, token: object) -> None:  # worker thread
        core = self._host.core
        try:
            result = core.resolve(self.url)
        except core.SiphonError as exc:
            result = str(exc) or "Siphon could not read this link."
        except Exception as exc:
            result = unexpected(exc)
        GLib.idle_add(self._on_resolved, token, result)

    def _on_resolved(self, token: object, result) -> bool:
        if token is not self._read_token:
            return GLib.SOURCE_REMOVE
        self._read_token = None
        if isinstance(result, str) or not result.tracks:
            message = result if isinstance(result, str) else "There is nothing to download at this link."
            self._reader.fail(message)
            self._host.report_error(message)
        elif len(result.tracks) == 1 and result.folder is None:
            self._start(self._reader, result.tracks[0], self._outdir)
        else:
            self._remove_row(self._reader)
            self._show_group(result)
        self._host.changed()
        return GLib.SOURCE_REMOVE

    def _show_group(self, result) -> None:
        count = len(result.tracks)
        buttons = Gtk.Box(spacing=6)
        buttons.append(self._open)
        buttons.append(self._save)
        buttons.append(self._cancel_all)
        self.set_header_suffix(buttons)  # only groups get a header; it would pad single rows
        self._group_title = result.title or short_url(self.url)
        self.set_title(GLib.markup_escape_text(self._group_title))
        summary = f"{result.kind.capitalize()} · {count} track{'s' if count != 1 else ''}"
        self.set_description(GLib.markup_escape_text("\n".join(filter(None, [summary, result.note]))))
        # straight into the music folder like single songs (the user's call, 2026-09-25): the group lives on as a
        # playlist, not as a subfolder - at once for a playlist link (2026-09-26), by "Save as Playlist" for an album
        if result.kind == "playlist":
            self._link(result.link or self.url)
        if result.cover_url:
            self._fetch_picture(result.cover_url)
        for index, track in enumerate(result.tracks):
            on_file = (lambda path, index=index: self._arrive(index, path)) if self._linked is not None else None
            self._start(self._add_row(track.title), track, self._outdir, on_file)

    def _start(self, row: TrackRow, track, outdir: Path, on_file: Callable[[Path], None] | None = None) -> None:
        row.set_title(track.title or short_url(track.url or self.url))
        row.set_subtitle(" · ".join(filter(None, [track.artist, track.source])))
        job = self._jobs[row] = Job(track, outdir, self._fmt, row, on_file)
        self._host.downloader.submit(job)

    # -- the playlist a playlist link makes

    def _link(self, url: str) -> None:
        music = self._host.music
        link = self._host.core.source_link(url)
        linked = music.change_playlists(music.playlists.link, self._group_title, link)
        playlist = linked.playlist() if linked is not None else None
        if playlist is None:  # the Playlists folder can't be written: "Save as Playlist" stays the way
            return
        self._linked = linked
        what = "Created the playlist" if linked.created else "Adding to the playlist"
        self._host.toast(f"{what} {quoted(playlist.name)}", "Open", self._open_playlist)

    def _arrive(self, index: int, path: Path) -> None:
        self._arrived[index] = path
        if not self._fill_source:
            self._fill_source = GLib.timeout_add(_FILL_DELAY_MS, self._fill)

    def _fill(self) -> bool:
        self._fill_source = 0
        songs, self._arrived = self._arrived, {}
        self._host.music.change_playlists(self._linked.add, songs)
        self.sync()  # the playlist may turn out to be deleted
        return GLib.SOURCE_REMOVE

    def _fetch_picture(self, url: str) -> None:
        self._fetching = True
        threading.Thread(target=self._get_picture, args=(url,), name="siphon-picture", daemon=True).start()

    def _get_picture(self, url: str) -> None:  # worker thread
        try:
            picture = self._host.core.cover_image([url])
        except Exception as exc:  # no picture is fine; a stuck "fetching" would hide Save as Playlist
            traceback.print_exception(exc)
            picture = None
        GLib.idle_add(self._on_picture, picture)

    def _on_picture(self, picture: bytes | None) -> bool:
        self._fetching = False
        self._picture = picture
        if picture and self._linked is not None:
            self._host.music.change_playlists(self._linked.set_cover, picture)
        self.sync()
        return GLib.SOURCE_REMOVE

    # -- rows

    def _add_row(self, title: str) -> TrackRow:
        row = TrackRow(title, self._on_row_action, self._host.changed)
        self._rows.append(row)
        self.add(row)
        return row

    def _remove_row(self, row: TrackRow) -> None:
        self._rows.remove(row)
        self._jobs.pop(row, None)
        self.remove(row)
