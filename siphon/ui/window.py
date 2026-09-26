"""The main window: Download, Library and Playlists pages, with the now-playing bar below them all."""

import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import settings
from .art import CoverArt
from .download_page import DownloadPage, is_dismissal
from .library_page import LibraryPage
from .music import Music
from .nowplaying import NowPlaying
from .playlists_page import PlaylistsPage
from .settings_dialog import SettingsDialog
from .songmenu import SongActions

_STOP_TIMEOUT = 10.0  # seconds to wait for cancelled downloads before closing anyway
_NARROW = "max-width: 560sp"


def main_menu() -> Gio.Menu:
    """The header bar's three-dot menu."""
    menu = Gio.Menu()
    main = Gio.Menu()
    main.append("Open Music Folder", "win.open-folder")
    main.append("Refresh Library", "win.refresh-library")
    main.append("Update Engine", "app.update-engine")
    menu.append_section(None, main)
    about = Gio.Menu()
    about.append("About Siphon", "app.about")
    about.append("Quit", "app.quit")  # closing the window may only hide it in the tray
    menu.append_section(None, about)
    return menu


class SiphonWindow(Adw.ApplicationWindow):
    __gsignals__ = {
        # Quitting was called off to keep downloads running (a restart to update waits for the next time).
        "close-cancelled": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Closed to the tray: Siphon keeps running.
        "hidden": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: Adw.Application, core: ModuleType, prefs: settings.Settings,
                 save_prefs: Callable[[], None], music: Music,
                 hide_on_close: Callable[[], bool] = lambda: False) -> None:
        """hide_on_close says, at each close, whether closing hides the window in the tray instead of quitting."""
        super().__init__(application=app, title="Siphon", default_width=820, default_height=1080)
        self.set_size_request(360, 480)
        self.music = music
        self._prefs = prefs
        self._save_prefs = save_prefs
        self._hide_on_close = hide_on_close
        self._quitting = False  # a real quit, which closing to the tray is not
        self._closing = False  # the question was answered: close for good
        self._settings: SettingsDialog | None = None
        self._settings_page = "appearance"  # the Settings dialog opens where it was last closed
        art = CoverArt(music.cover_file)

        self._toasts = Adw.ToastOverlay()
        self._stack = Adw.ViewStack(vexpand=True)
        self.downloads = DownloadPage(core, prefs, save_prefs, self.toast, self._toasts.add_toast, self.show_file,
                                      music, self.open_playlist)
        self.library = LibraryPage(music, art, prefs, save_prefs, lambda: self.show_page("download"))
        self.playlists = PlaylistsPage(music, art, self.toast)
        for name, title, icon, page in (
                ("download", "Download", "folder-download-symbolic", self.downloads),
                ("library", "Library", "folder-music-symbolic", self.library),
                ("playlists", "Playlists", "view-list-bullet-symbolic", self.playlists)):
            self._stack.add_titled_with_icon(page, name, title, icon)
        self._stack.set_visible_child_name(prefs.page)

        self._header = self._build_header()
        self._now_playing = NowPlaying(music.player, art)
        switcher_bar = Adw.ViewSwitcherBar(stack=self._stack)
        # Toasts cover the pages only, never the now-playing bar below them.
        self._toasts.set_child(self._stack)
        self._view = Adw.ToolbarView(content=self._toasts)
        self._view.add_top_bar(self._header)
        self._view.add_bottom_bar(self._now_playing)
        self._view.add_bottom_bar(switcher_bar)
        self.set_content(self._view)
        self._now_playing.connect("notify::reveal-child", lambda *_: self._sync_bottom_bar())
        self._sync_bottom_bar()

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(_NARROW))
        narrow.add_setter(switcher_bar, "reveal", True)
        narrow.connect("apply", lambda _b: self._set_narrow(True))
        narrow.connect("unapply", lambda _b: self._set_narrow(False))
        self.add_breakpoint(narrow)

        SongActions(self, music, self.toast, self.show_file)
        self._install_actions()
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("notify::is-active", self._on_active_changed)
        self._stack.connect("notify::visible-child-name", self._on_page_changed)
        self.downloads.connect("queue-changed", lambda _page: self._sync_header())
        self.playlists.nav.connect("notify::visible-page", lambda *_: self._sync_header())
        music.connect("error", lambda _music, message: self.toast(message))
        self._play_error: Adw.Toast | None = None
        music.player.connect("error", self._on_player_error)
        self._sync_header()

    # -- construction

    def _build_header(self) -> Adw.HeaderBar:
        self._switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=self._switcher)
        self._back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self._back.connect("clicked", lambda _b: self.playlists.nav.pop())
        header.pack_start(self._back)
        # A text button: icon themes draw edit-clear-all too much like a close button.
        self._clear_button = Gtk.Button(label="Clear", action_name="win.clear-finished",
                                        tooltip_text="Clear Finished Downloads")
        header.pack_start(self._clear_button)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu(), primary=True,
                                       tooltip_text="Main Menu"))
        cog = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings", action_name="win.settings")
        header.pack_end(cog)  # pack_end runs right to left: the cog sits left of the main menu
        return header

    def _install_actions(self) -> None:
        for name, handler in (("open-folder", self._on_open_folder),
                              ("clear-finished", lambda *_: self.downloads.clear_finished()),
                              ("focus-entry", self._on_focus_entry),
                              ("search", self._on_search),
                              ("settings", self._on_settings),
                              ("refresh-library", lambda *_: self.music.rescan())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    # -- pages

    def show_page(self, name: str) -> None:
        self._stack.set_visible_child_name(name)

    def open_playlist(self, playlist: Any) -> None:
        self.show_page("playlists")
        self.playlists.open(playlist)

    def _on_page_changed(self, *_args) -> None:
        self._prefs.page = self._stack.get_visible_child_name()
        self._save_prefs()
        self._sync_header()

    def _sync_header(self) -> None:
        page = self._stack.get_visible_child_name()
        finished = self.downloads.has_finished
        self.lookup_action("clear-finished").set_enabled(finished)
        self._clear_button.set_visible(finished and page == "download")
        at_detail = self.playlists.nav.get_visible_page().get_tag() != "playlists"
        self._back.set_visible(page == "playlists" and at_detail)

    def _sync_bottom_bar(self) -> None:
        """Raised only while the now-playing bar shows: a hidden one would still draw its edge."""
        shown = self._now_playing.get_reveal_child()
        self._view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED if shown else Adw.ToolbarStyle.FLAT)

    def _set_narrow(self, narrow: bool) -> None:
        self._header.set_title_widget(None if narrow else self._switcher)
        self._now_playing.set_narrow(narrow)

    def _on_focus_entry(self, *_args) -> None:
        self.show_page("download")
        self.downloads.focus_entry()

    def _on_search(self, *_args) -> None:
        self.show_page("library")
        self.library.focus_search()

    def _on_settings(self, *_args) -> None:
        if self._settings is not None:  # Ctrl+, again while it is open, maybe under one of its questions
            return
        self._settings = SettingsDialog(self.get_application(), self, self._settings_page)
        self._settings.connect("closed", self._on_settings_closed)
        self._settings.present(self)

    def _on_settings_closed(self, dialog: SettingsDialog) -> None:
        self._settings_page = dialog.get_visible_page_name()
        self._settings = None

    # -- downloads

    def queue_links(self, links: list[str]) -> list[str]:
        """Queue every supported link; returns the ones that were not links."""
        self.show_page("download")
        return self.downloads.queue_links(links)

    # -- messages and files

    def toast(self, message: str, button: str | None = None, on_button: Callable[[], None] | None = None) -> Adw.Toast:
        toast = Adw.Toast(title=message, use_markup=False)
        if button is not None and on_button is not None:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda _toast: on_button())
        self._toasts.add_toast(toast)
        return toast

    def _on_player_error(self, _player: Any, message: str) -> None:
        """A run of unplayable files updates one toast instead of queueing one per file."""
        if self._play_error is not None:
            self._play_error.set_title(message)
            return
        self._play_error = self.toast(message)
        self._play_error.connect("dismissed", lambda _toast: setattr(self, "_play_error", None))

    def show_file(self, path: Path) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.open_containing_folder(self, None, self._on_launched, launcher.open_containing_folder_finish)

    def _on_open_folder(self, *_args) -> None:
        try:
            self._prefs.folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.toast(f"Could not create the folder: {exc.strerror}.")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self._prefs.folder)))
        launcher.launch(self, None, self._on_launched, launcher.launch_finish)

    def open_page(self, uri: str) -> None:
        launcher = Gtk.UriLauncher.new(uri)
        launcher.launch(self, None, self._on_page_launched)

    def _on_page_launched(self, launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not is_dismissal(error):
                self.toast("Could not open the page.")

    def _on_launched(self, _launcher: Gtk.FileLauncher, result: Gio.AsyncResult,
                     finish: Callable[[Gio.AsyncResult], bool]) -> None:
        try:
            finish(result)
        except GLib.Error as error:
            if not is_dismissal(error):
                self.toast("Could not open the folder.")

    # -- input

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int,
                state: Gdk.ModifierType) -> bool:
        """Space plays or pauses and Ctrl+Left/Right skip, unless the user is typing or in a dialog or menu."""
        player = self.music.player
        focus = self.get_focus()
        if (player.current is None or self.get_visible_dialog() is not None or isinstance(focus, Gtk.Editable)
                or (focus is not None and focus.get_ancestor(Gtk.Popover) is not None)):
            return False
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if keyval == Gdk.KEY_space and not mods:
            player.toggle()
        elif keyval == Gdk.KEY_Right and mods == Gdk.ModifierType.CONTROL_MASK:
            player.next()
        elif keyval == Gdk.KEY_Left and mods == Gdk.ModifierType.CONTROL_MASK:
            player.previous()
        else:
            return False
        return True

    def _on_active_changed(self, *_args) -> None:
        if self.is_active() and self._stack.get_visible_child_name() == "download":
            self.downloads.offer_clipboard(self.get_clipboard())

    def _on_drop(self, _target: Gtk.DropTarget, value, _x: float, _y: float) -> bool:
        if isinstance(value, Gdk.FileList):
            links = [file.get_uri() for file in value.get_files()]
        else:
            links = [line.strip() for line in value.splitlines() if line.strip() and not line.startswith("#")]
        self.queue_links(links)
        return True

    # -- closing

    @property
    def quitting(self) -> bool:
        return self._quitting

    def quit(self) -> None:
        """Quit Siphon, not just close the window: asks first while downloads run, in the window shown again."""
        self._quitting = True
        if self.downloads.unfinished and not self.get_visible():
            self.present()
        self.close()

    def do_close_request(self) -> bool:
        """Closing hides the window when Siphon keeps running in the tray. Quitting asks first while downloads
        run; music just stops."""
        if self._closing:
            return False
        if not self._quitting and self._hide_on_close():
            self.set_visible(False)
            self.emit("hidden")
            return True
        count = self.downloads.unfinished
        if not count:
            return False
        dialog = Adw.AlertDialog(heading=f"Stop {count} download{'s' if count != 1 else ''}?",
                                 body="Quitting Siphon cancels what is still downloading. "
                                      "Finished files are kept.")
        dialog.add_response("keep", "Keep Downloading")
        dialog.add_response("stop", "Stop and Quit")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        dialog.connect("response", self._on_close_response)
        dialog.present(self)
        return True

    def _on_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "stop":
            self._quitting = False
            self.emit("close-cancelled")
            return
        self.downloads.cancel_all()
        self.music.player.stop()
        self.set_visible(False)
        deadline = time.monotonic() + _STOP_TIMEOUT

        def close_when_idle() -> bool:
            if self.downloads.downloader.running and time.monotonic() < deadline:
                return GLib.SOURCE_CONTINUE
            self._closing = True
            self.close()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(100, close_when_idle)
