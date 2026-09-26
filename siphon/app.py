"""The Siphon application: single instance, `siphon URL...` queues links; owns the settings, library and player."""

import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType

from . import __version__, paths, settings
from .ui.appearance import Appearance  # importing siphon.ui first pins GTK 4 and libadwaita 1
from .ui.downloader import unexpected
from .ui.music import Music
from .ui.window import SiphonWindow
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.ggrrk.Siphon"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STYLE = Path(__file__).resolve().parent / "ui" / "style.css"
_SAVE_DELAY_MS = 500


def _fake(name: str) -> ModuleType:
    """A development stand-in from tests/ (see the SIPHON_FAKE_* switches)."""
    spec = importlib.util.spec_from_file_location(f"siphon_{name}", _DATA_DIR.parent / "tests" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module


def load_core() -> ModuleType:
    """The real engine, or the offline stand-in when SIPHON_FAKE_CORE=1 (development only)."""
    if os.environ.get("SIPHON_FAKE_CORE") != "1":
        from . import core
        return core
    return _fake("fake_core")


def load_music() -> tuple[ModuleType, ModuleType, ModuleType]:
    """The library, playlists and covers modules; SIPHON_FAKE_CORE=1 swaps all three for tests/fake_library.py."""
    if os.environ.get("SIPHON_FAKE_CORE") == "1":
        fake = _fake("fake_library")
        return fake, fake, fake
    from . import covers, library, playlists
    return library, playlists, covers


def load_player() -> type:
    """The libmpv player, or a silent timer-driven one when SIPHON_FAKE_PLAYER=1."""
    if os.environ.get("SIPHON_FAKE_PLAYER") == "1":
        return _fake("fake_player").Player
    from .player import Player
    return Player


class SiphonApp(Adw.Application):
    def __init__(self, core: ModuleType) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.core = core
        self._window: SiphonWindow | None = None
        self._save_source = 0

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("Siphon")
        Gtk.Window.set_default_icon_name(APP_ID)
        if (_DATA_DIR / f"{APP_ID}.svg").exists():  # running from a checkout: use the icon in data/
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(_DATA_DIR))
        for name, handler in (("quit", self._on_quit), ("about", self._on_about),
                              ("update-engine", self._on_update_engine)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("win.focus-entry", ["<Control>l"])
        self.set_accels_for_action("win.search", ["<Control>f"])
        self.set_accels_for_action("win.refresh-library", ["F5"])
        self.set_accels_for_action("win.settings", ["<Control>comma"])
        css = Gtk.CssProvider()
        css.load_from_path(str(_STYLE))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.prefs = settings.load(settings.Settings(self.core.FORMATS[0], self.core.default_outdir()),
                                   self.core.FORMATS)
        # Before any window exists, so a forced style never flashes the system's first.
        self.appearance = Appearance(Gdk.Display.get_default())
        self.appearance.set_style(self.prefs.style)
        self.appearance.set_accent(self.prefs.accent)
        for name in ("style", "accent"):
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"),
                                                   GLib.Variant.new_string(getattr(self.prefs, name)))
            action.connect("change-state", self._on_appearance_changed)
            self.add_action(action)
        library_module, playlists_module, covers_module = load_music()
        self.player = load_player()()
        self.player.set_volume(self.prefs.volume)
        self.player.set_shuffle(self.prefs.shuffle)
        self.player.set_repeat(self.prefs.repeat)
        self.player.set_equalizer(self.prefs.equalizer.gains())
        self.player.connect("changed", self._on_player_changed)
        library = library_module.Library(self.prefs.folder)
        # lookup: playlists label their entries from the library instead of reading tags again.
        self.music = Music(library, lambda folder: playlists_module.Playlists(folder, lookup=library.get),
                           self.player, covers_module.cover_file, library_module.Song)
        self._mpris = None
        # MPRIS is D-Bus, which Windows lacks: its media keys stay a known gap there.
        if not paths.windows() and os.environ.get("SIPHON_NO_MPRIS") != "1":
            from .mpris import Mpris
            self._mpris = Mpris(self.player, self)
        self.music.rescan()

    def do_shutdown(self) -> None:
        self.player.stop()
        self.player.shutdown()
        if self._mpris is not None:
            self._mpris.close()
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._write_settings()
        Adw.Application.do_shutdown(self)

    def do_activate(self) -> None:
        if self._window is None:
            self._window = SiphonWindow(self, self.core, self.prefs, self.save_settings, self.music)
            self._window.connect("destroy", self._on_window_destroyed)
        self._window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        self.activate()
        links = command_line.get_arguments()[1:]
        for arg in self._window.queue_links(links) if links else []:
            command_line.printerr_literal(f"siphon: not a link: {arg}\n")
        return 0

    def _on_window_destroyed(self, _window: SiphonWindow) -> None:
        self._window = None

    # -- settings

    def save_settings(self) -> None:
        """Soon rather than now: dragging the volume changes the settings many times a second."""
        if not self._save_source:
            self._save_source = GLib.timeout_add(_SAVE_DELAY_MS, self._write_settings)

    def apply_equalizer(self) -> None:
        """After a change to self.prefs.equalizer: play it from now on and remember it."""
        self.player.set_equalizer(self.prefs.equalizer.gains())
        self.save_settings()

    def _write_settings(self) -> bool:
        self._save_source = 0
        try:
            settings.save(self.prefs)
        except OSError as exc:
            if self._window is not None:
                self._window.toast(f"Could not save your settings: {exc.strerror}.")
        return GLib.SOURCE_REMOVE

    def _on_appearance_changed(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        name, choice = action.get_name(), value.get_string()
        allowed, apply = ((settings.STYLES, self.appearance.set_style) if name == "style"
                          else (settings.ACCENTS, self.appearance.set_accent))
        if choice not in allowed:
            return
        action.set_state(value)
        setattr(self.prefs, name, choice)
        apply(choice)
        self.save_settings()

    def _on_player_changed(self, player) -> None:
        state = (player.volume, player.shuffle, player.repeat)
        if state != (self.prefs.volume, self.prefs.shuffle, self.prefs.repeat):
            self.prefs.volume, self.prefs.shuffle, self.prefs.repeat = state
            self.save_settings()

    def _on_quit(self, *_args) -> None:
        if self._window is None:
            self.quit()
        else:
            self._window.close()  # asks first when downloads are running

    def _on_about(self, *_args) -> None:
        try:
            engine = f"yt-dlp {self.core.engine_version()}"
        except Exception:
            engine = "yt-dlp (not found)"
        dialog = Adw.AboutDialog(
            application_name="Siphon", application_icon=APP_ID, version=__version__, developer_name="GGRRK",
            comments=f"Paste a link, get just the audio as a tagged file.\n\nDownload engine: {engine}",
            debug_info=f"Siphon {__version__}\n{engine}\nPython {sys.version.split()[0]}\n"
                       f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}\n"
                       f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        )
        dialog.present(self._window)

    def _on_update_engine(self, action: Gio.SimpleAction, _param) -> None:
        action.set_enabled(False)
        progress = self._window.toast("Updating the download engine…")

        def work() -> None:
            try:
                before = self.core.engine_version()
                after = self.core.update_engine()
                message = (f"The engine is already up to date (yt-dlp {after})." if after == before
                           else f"Updated to yt-dlp {after}. Restart Siphon to use it.")
            except self.core.SiphonError as exc:
                message = str(exc) or "The engine update failed."
            except Exception as exc:
                message = unexpected(exc)
            GLib.idle_add(done, message)

        def done(message: str) -> bool:
            action.set_enabled(True)
            progress.dismiss()
            if self._window is not None:
                self._window.toast(message)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="siphon-update", daemon=True).start()


def main(argv: list[str]) -> int:
    return SiphonApp(load_core()).run(argv)
