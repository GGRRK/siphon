"""The Download page: link entry, format and folder, and the download queue."""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import settings
from .block import LinkBlock
from .downloader import Downloader
from .fmt import pretty_path
from .format_row import FormatRow
from .music import Music

_CLIPBOARD_MAX = 2048  # longer clipboard text is not a link worth offering


def is_dismissal(error: GLib.Error) -> bool:
    return error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED) or \
        error.matches(Gtk.DialogError.quark(), Gtk.DialogError.CANCELLED)


class DownloadPage(Gtk.Box):
    """Also the host every LinkBlock reports to (see block.Host)."""

    __gsignals__ = {"queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, core: ModuleType, prefs: settings.Settings, save_prefs: Callable[[], None],
                 toast: Callable[..., Adw.Toast], add_toast: Callable[[Adw.Toast], None],
                 show_file: Callable[[Path], None], music: Music, open_playlist: Callable[[Any], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.core = core
        self._prefs = prefs
        self._save_prefs = save_prefs
        self.toast = toast
        self._add_toast = add_toast
        self.show_file = show_file
        self.music = music
        self.open_playlist = open_playlist
        self.downloader = Downloader(core, self.report_error, music.add_file)
        self._blocks: list[LinkBlock] = []
        self._seen: set[str] = set()  # links queued or offered from the clipboard this session
        self._error_toast: Adw.Toast | None = None
        self._error_count = 0
        self.append(self._build_controls())
        self.append(self._build_queue())

    # -- construction

    def _build_controls(self) -> Gtk.Widget:
        self._entry = Gtk.Entry(hexpand=True, input_purpose=Gtk.InputPurpose.URL,
                                placeholder_text="Paste a link – YouTube, Spotify, SoundCloud…",
                                primary_icon_name="insert-link-symbolic")
        self._entry.update_property([Gtk.AccessibleProperty.LABEL], ["Link"])
        self._entry.connect("activate", self._on_submit)
        self._entry.connect("changed", self._on_entry_changed)
        self._download = Gtk.Button(label="Download", sensitive=False)
        self._download.add_css_class("suggested-action")
        self._download.connect("clicked", self._on_submit)
        link_bar = Gtk.Box(spacing=6)
        link_bar.append(self._entry)
        link_bar.append(self._download)

        self._format_row = FormatRow(self.core, self._prefs.format)
        self._format_row.connect("notify::selected", self._on_format_changed)
        self._folder_row = Adw.ActionRow(title="Save To", use_markup=False, subtitle_lines=2)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.add_css_class("flat")
        change.connect("clicked", self._on_choose_folder)
        self._folder_row.add_suffix(change)
        self._folder_row.set_activatable_widget(change)
        options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        options.append(self._format_row)
        options.append(self._folder_row)
        self._show_settings()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=6, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(link_bar)
        box.append(options)
        return Adw.Clamp(maximum_size=640, child=box)

    def _build_queue(self) -> Gtk.Widget:
        empty = Adw.StatusPage(icon_name="io.github.ggrrk.Siphon", title="Paste a link", vexpand=True,
                               description="YouTube, YouTube Music, SoundCloud, Bandcamp, Spotify and most "
                                           "other sites – you get just the audio, tagged and ready to play.",
                               css_classes=["compact"])
        self._queue = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                              margin_top=6, margin_bottom=18, margin_start=12, margin_end=12)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                      child=Adw.Clamp(maximum_size=640, child=self._queue))
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._stack.add_named(empty, "empty")
        self._stack.add_named(scroller, "queue")
        return self._stack

    def focus_entry(self) -> None:
        self._entry.grab_focus()

    def set_narrow(self, narrow: bool) -> None:
        self._format_row.props.narrow = narrow

    # -- queue

    def queue_links(self, links: list[str]) -> list[str]:
        """Queue every supported link; returns the ones that were not links."""
        links = [link.strip() for link in links if link.strip()]
        rejected = [link for link in links if not self.core.is_supported(link)]
        for link in links:
            if link not in rejected:
                self.queue_link(link)
        if rejected:
            self.toast(f"“{rejected[0][:60]}” is not a link Siphon can read.")
        return rejected

    def queue_link(self, url: str) -> None:
        if any(block.url == url and block.unfinished for block in self._blocks):
            self.toast("That link is already in the queue.")
            return
        self._seen.add(url)
        block = LinkBlock(url, self._prefs.format, self._prefs.folder, self)
        self._blocks.insert(0, block)
        self._queue.prepend(block)
        self.changed()

    def changed(self) -> None:
        """Called whenever a row changes state; keeps the chrome around the queue in step."""
        for block in self._blocks:
            block.sync()
        self._stack.set_visible_child_name("queue" if self._blocks else "empty")
        self.emit("queue-changed")

    @property
    def unfinished(self) -> int:
        return sum(block.unfinished for block in self._blocks)

    @property
    def has_finished(self) -> bool:
        return any(block.has_finished for block in self._blocks)

    def cancel_all(self) -> None:
        for block in self._blocks:
            block.cancel_all()

    def clear_finished(self) -> None:
        for block in list(self._blocks):
            block.clear_finished()
            if block.is_empty:
                self._blocks.remove(block)
                self._queue.remove(block)
        self.changed()

    # -- messages

    def report_error(self, message: str) -> None:
        """One toast per burst of failures, so a failing playlist does not queue fifty toasts."""
        if self._error_toast is None:
            self._error_count = 1
            # High priority: shown at once instead of waiting behind other toasts.
            self._error_toast = Adw.Toast(title=message, use_markup=False, priority=Adw.ToastPriority.HIGH)
            self._error_toast.connect("dismissed", self._on_error_toast_dismissed)
            self._add_toast(self._error_toast)
        else:
            self._error_count += 1
            self._error_toast.set_title(f"{self._error_count} downloads failed")

    def _on_error_toast_dismissed(self, _toast: Adw.Toast) -> None:
        self._error_toast = None

    # -- settings

    def _show_settings(self) -> None:
        self._folder_row.set_subtitle(pretty_path(self._prefs.folder))
        self._folder_row.set_tooltip_text(str(self._prefs.folder))

    def _on_format_changed(self, row: FormatRow, _pspec) -> None:
        self._prefs.format = row.format
        self._save_prefs()

    def _on_choose_folder(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose Where to Save", modal=True)
        if self._prefs.folder.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(self._prefs.folder)))
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if not is_dismissal(error):
                self.toast("Could not use that folder.")
            return
        if folder.get_path() is None:
            self.toast("Choose a folder on this computer.")
            return
        self._prefs.folder = Path(folder.get_path())
        self._show_settings()
        self._save_prefs()
        self.music.set_root(self._prefs.folder)

    # -- link entry, clipboard, drops

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        entry.remove_css_class("error")
        self._download.set_sensitive(bool(entry.get_text().strip()))

    def _on_submit(self, _widget: Gtk.Widget) -> None:
        links = self._entry.get_text().split()
        if not links:
            return
        if not all(self.core.is_supported(link) for link in links):
            self._entry.add_css_class("error")
            self.toast("That does not look like a link. Paste a web address or a Spotify link.")
            return
        self._entry.set_text("")
        self.queue_links(links)

    def offer_clipboard(self, clipboard: Gdk.Clipboard) -> None:
        if not self._entry.get_text():
            clipboard.read_text_async(None, self._on_clipboard_text)

    def _on_clipboard_text(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            text = (clipboard.read_text_finish(result) or "").strip()
        except GLib.Error:
            return
        if (not text or len(text) > _CLIPBOARD_MAX or text in self._seen or self._entry.get_text()
                or not self.core.is_supported(text)):
            return
        self._seen.add(text)
        self._entry.set_text(text)
        self._entry.grab_focus()
        self._entry.select_region(0, -1)
