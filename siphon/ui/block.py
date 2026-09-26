"""Everything one pasted link turns into: a single row, or a titled group of rows."""

import threading
from pathlib import Path
from types import ModuleType
from typing import Protocol

from gi.repository import Adw, GLib, Gtk

from .downloader import Downloader, Job, unexpected
from .row import ACTIVE, FINISHED, State, TrackRow


class Host(Protocol):
    core: ModuleType
    downloader: Downloader

    def changed(self) -> None: ...
    def report_error(self, message: str) -> None: ...
    def show_file(self, path: Path) -> None: ...
    def save_playlist(self, name: str, paths: list[Path]) -> bool: ...


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
        self._cancel_all = Gtk.Button(label="Cancel All", valign=Gtk.Align.CENTER, visible=False)
        self._cancel_all.add_css_class("flat")
        self._cancel_all.connect("clicked", lambda _button: self.cancel_all())
        self._save = Gtk.Button(label="Save as Playlist", valign=Gtk.Align.CENTER, visible=False)
        self._save.add_css_class("flat")
        self._save.connect("clicked", self._on_save)

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
        self._save.set_visible(grouped and not self._saved and not self.unfinished and bool(self._files()))

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
        if self._host.save_playlist(self._group_title, self._files()):
            self._saved = True
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
        buttons.append(self._save)
        buttons.append(self._cancel_all)
        self.set_header_suffix(buttons)  # only groups get a header; it would pad single rows
        self._group_title = result.title or short_url(self.url)
        self.set_title(GLib.markup_escape_text(self._group_title))
        summary = f"{result.kind.capitalize()} · {count} track{'s' if count != 1 else ''}"
        self.set_description(GLib.markup_escape_text("\n".join(filter(None, [summary, result.note]))))
        # straight into the music folder like single songs (the user's call, 2026-09-25): the group lives on as a
        # playlist through "Save as Playlist", not as a subfolder
        for track in result.tracks:
            self._start(self._add_row(track.title), track, self._outdir)

    def _start(self, row: TrackRow, track, outdir: Path) -> None:
        row.set_title(track.title or short_url(track.url or self.url))
        row.set_subtitle(" · ".join(filter(None, [track.artist, track.source])))
        job = self._jobs[row] = Job(track, outdir, self._fmt, row)
        self._host.downloader.submit(job)

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
