"""The Siphon application: single instance, `siphon URL...` queues links; owns the settings, library, player and
the tray icon, which keeps Siphon running after its window is closed. On a Linux desktop with no tray at all,
closing the window while Siphon plays or downloads keeps it running in the background instead, until it has
nothing left to do; the desktop's media controls (MPRIS) or starting Siphon again bring the window back."""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from . import __version__, paths, settings, tray
from .ui.appearance import Appearance  # importing siphon.ui first pins GTK 4 and libadwaita 1
from .ui.music import Music
from .ui.updates import Updates
from .ui.window import SiphonWindow
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.ggrrk.Siphon"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STYLE = Path(__file__).resolve().parent / "ui" / "style.css"
_SAVE_DELAY_MS = 500
_VOLUME_STEP = 0.05  # per wheel notch on the tray icon
_TRAY_GRACE_S = 10  # a bar that restarts is back sooner; the window comes back if its tray does not
# Hidden in the background (no tray), Siphon quits once nothing plays or downloads for this long; paused, it waits
# longer for the media controls to resume it.
_BACKGROUND_GRACE_S = 5
_PAUSED_GRACE_S = 600


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
        self._engine_progress: Adw.Toast | None = None
        self.tray: tray.Tray | None = None
        self._tray_lost = 0
        self._in_background = False  # the window was closed with no tray to go to, while Siphon had work
        self._idle_quit = 0
        self._idle_grace: int | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("Siphon")
        Gtk.Window.set_default_icon_name(APP_ID)
        if (_DATA_DIR / f"{APP_ID}.svg").exists():  # running from a checkout: use the icon in data/
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(_DATA_DIR))
        self.updates = Updates(self.core, self._on_quit)
        for name, handler in (("quit", self._on_quit), ("about", self._on_about),
                              ("update-engine", self._on_update_engine),
                              ("check-for-updates", lambda *_: self.updates.check_now()),
                              ("restart-to-update", lambda *_: self.updates.restart_to_update())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.updates.connect("changed", self._sync_update_actions)
        self.updates.connect("app-checked", self._on_app_checked)
        self.updates.connect("engine-checked", self._on_engine_checked)
        self._sync_update_actions(self.updates)
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
        self.player.connect("changed", self._check_background)
        library = library_module.Library(self.prefs.folder)
        # lookup: playlists label their entries from the library instead of reading tags again.
        self.music = Music(library, lambda folder: playlists_module.Playlists(folder, lookup=library.get),
                           self.player, covers_module.cover_file, library_module.Song)
        self._mpris = None
        # MPRIS is D-Bus, which Windows lacks: its media keys stay a known gap there.
        if not paths.windows() and os.environ.get("SIPHON_NO_MPRIS") != "1":
            from .mpris import Mpris
            self._mpris = Mpris(self.player, self)
        self.tray = tray.create(self.player, self.get_dbus_connection())
        if self.tray is not None:
            self.tray.connect("command", self._on_tray_command)
            self.tray.connect("scroll", self._on_tray_scroll)
            self.tray.connect("open", self._on_tray_open)
            self.tray.connect("session-end", self._on_session_end)
            self.tray.connect("notify::available", self._on_tray_available)
        self.music.rescan()

    def do_shutdown(self) -> None:
        if self.tray is not None:
            self.tray.quitting()  # Windows: a Siphon started from now on runs by itself
        self.player.stop()
        self.player.shutdown()
        if self._mpris is not None:
            self._mpris.close()
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._write_settings()
        if self._tray_lost:
            GLib.source_remove(self._tray_lost)
        if self._idle_quit:
            GLib.source_remove(self._idle_quit)
        if self.prefs.told_about_background:
            self.withdraw_notification("background")
        # an ending Windows session (logoff, shutdown, or an installer closing Siphon) starts no installer
        self.updates.finish(session_ending=self.tray is not None and self.tray.session_ending)
        if self.tray is not None:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def do_activate(self) -> None:
        if self._window is None:
            self._window = SiphonWindow(self, self.core, self.prefs, self.save_settings, self.music,
                                        hide_on_close=self.hides_on_close)
            self._window.connect("destroy", self._on_window_destroyed)
            self._window.connect("hidden", self._on_window_hidden)
            self._window.connect("close-cancelled", lambda _window: self.updates.cancel_restart())
            self._window.connect("notify::visible", self._on_window_visible)
            self._window.downloads.connect("queue-changed", self._check_background)
            # Once, with a window for the news; never for the development stand-in, which would reach GitHub.
            if os.environ.get("SIPHON_FAKE_CORE") != "1":
                self.updates.start(self.prefs.auto_update, self.prefs.auto_engine)
        self._window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        for arg in self._open(command_line.get_arguments()[1:]):
            command_line.printerr_literal(f"siphon: not a link: {arg}\n")
        return 0

    def _open(self, links: list[str]) -> list[str]:
        """Show the window, hidden or not, and queue links; returns the arguments that were not links."""
        self.activate()
        return self._window.queue_links(links) if links else []

    def _on_window_destroyed(self, _window: SiphonWindow) -> None:
        self._window = None
        self._in_background = False
        self._check_background()

    # -- the tray

    def hides_on_close(self) -> bool:
        """Closing the window leaves Siphon running when that is wanted: in the tray when one shows the icon,
        else in the background while it plays or downloads."""
        if not self.prefs.close_to_tray:
            return False
        if self.tray is not None and self.tray.available:
            return True
        return self.runs_in_background() and self._busy()

    def runs_in_background(self) -> bool:
        """Whether Siphon can keep running with its window closed and no tray: on Linux, where MPRIS's Raise and a
        second start (both over the session bus) bring the window back."""
        return not paths.windows() and self.get_dbus_connection() is not None

    def _busy(self) -> bool:
        return self.player.state == "playing" or (self._window is not None and self._window.downloads.unfinished > 0)

    def _on_window_hidden(self, _window: SiphonWindow) -> None:
        if self.tray is not None and self.tray.available:
            self._tell_about_tray()
        else:
            self._in_background = True
            self._tell_about_background()
            self._check_background()

    def _tell_about_background(self) -> None:
        """The first time the window closes into the background, say so, with a way to quit: once, ever."""
        if self.prefs.told_about_background:
            return
        self.prefs.told_about_background = True
        self.save_settings()
        notification = Gio.Notification.new("Siphon keeps playing in the background")
        notification.set_body("There is no system tray here. Start Siphon again or use the media controls to bring "
                              "its window back; it quits by itself once the music stops and downloads finish.")
        notification.set_icon(Gio.ThemedIcon.new(APP_ID))
        notification.add_button("Quit", "app.quit")
        self.send_notification("background", notification)

    def _on_window_visible(self, window: SiphonWindow, _pspec) -> None:
        if window.get_visible():
            self._in_background = False
            self.withdraw_notification("background")  # its Quit button was for the hidden Siphon
            self._check_background()

    def _check_background(self, *_args) -> None:
        """Hidden in the background with nothing left to do: quit after a grace period, so that nothing lingers
        unseen. The period restarts when it changes (paused, then stopped) and ends when work or a tray comes."""
        window = self._window
        grace = None
        if (self._in_background and window is not None and not window.get_visible() and not window.quitting
                and not (self.tray is not None and self.tray.available) and not self._busy()):
            grace = _PAUSED_GRACE_S if self.player.state == "paused" else _BACKGROUND_GRACE_S
        if grace == self._idle_grace:
            return
        if self._idle_quit:
            GLib.source_remove(self._idle_quit)
            self._idle_quit = 0
        self._idle_grace = grace
        if grace is not None:
            self._idle_quit = GLib.timeout_add_seconds(grace, self._on_idle_quit)

    def _on_idle_quit(self) -> bool:
        self._idle_quit = 0
        self._idle_grace = None
        if self._window is not None and self._in_background and not self._window.get_visible() and not self._busy():
            self._window.quit()
        return GLib.SOURCE_REMOVE

    def _tell_about_tray(self) -> None:
        """The first time the window closes to the tray, say where Siphon went: once, ever."""
        if self.prefs.told_about_tray:
            return
        self.prefs.told_about_tray = True
        self.save_settings()
        heading = "Siphon Is Still Running"
        body = "It keeps playing and downloading in the tray. To quit, choose Quit Siphon in the tray icon's menu."
        if not self.tray.balloon(heading, body):  # Linux: the desktop's notifications
            notification = Gio.Notification.new(heading)
            notification.set_body(body)
            notification.set_icon(Gio.ThemedIcon.new(APP_ID))
            self.send_notification("tray", notification)

    def _on_tray_command(self, source: tray.Tray, command: str) -> None:
        player = self.player
        if command == tray.SHOW:
            token = source.take_token()
            if token and self._window is not None:
                self._window.set_startup_id(token)  # Wayland: the click's activation token lets it take focus
            self.activate()
        elif command == tray.TOGGLE:
            window = self._window
            if window is not None and window.get_visible() and window.is_active() and self.hides_on_close():
                source.take_token()  # the click's, for showing: not this time
                window.close()  # into the tray
            else:
                self._on_tray_command(source, tray.SHOW)
        elif command == tray.PLAY_PAUSE:
            if player.current is not None:
                player.toggle()
        elif command == tray.NEXT:
            player.next()
        elif command == tray.PREVIOUS:
            player.previous()
        elif command == tray.QUIT:
            self.activate_action("quit", None)

    def _on_tray_scroll(self, _tray: tray.Tray, notches: float) -> None:
        self.player.set_volume(min(1.0, max(0.0, self.player.volume + notches * _VOLUME_STEP)))

    def _on_tray_open(self, _tray: tray.Tray, args: list[str]) -> None:
        """Windows: another Siphon was started and handed its command line over."""
        for arg in self._open(args):
            print(f"siphon: not a link: {arg}", file=sys.stderr)

    def _on_session_end(self, _tray: tray.Tray) -> None:
        """Windows is ending the session: stop and save now, without a question."""
        if self._window is not None:
            self._window.downloads.cancel_all()
        self.quit()

    def _on_tray_available(self, source: tray.Tray, _pspec) -> None:
        if self._tray_lost:
            GLib.source_remove(self._tray_lost)
            self._tray_lost = 0
        window = self._window
        if source.available:
            self._in_background = False  # a window hidden in the background is in the tray now
        elif window is not None and not window.get_visible() and not window.quitting and not self._in_background:
            self._tray_lost = GLib.timeout_add_seconds(_TRAY_GRACE_S, self._on_tray_lost)
        self._check_background()

    def _on_tray_lost(self) -> bool:
        """The tray has been gone a while with the window hidden in it: bring the window back, the only way in."""
        self._tray_lost = 0
        if self._window is not None and not self._window.get_visible() and not self.tray.available:
            self.activate()
        return GLib.SOURCE_REMOVE

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
            self._window.quit()  # asks first when downloads are running, even from the tray

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

    # -- updates

    def _sync_update_actions(self, updates: Updates) -> None:
        self.lookup_action("update-engine").set_enabled(not updates.engine_busy)
        self.lookup_action("check-for-updates").set_enabled(not updates.busy and updates.state != "ready")
        self.lookup_action("restart-to-update").set_enabled(updates.state == "ready")

    def _on_update_engine(self, *_args) -> None:
        if self._window is not None:
            self._engine_progress = self._window.toast("Updating the download engine…")
        self.updates.update_engine_now()

    def _on_engine_checked(self, updates: Updates, manual: bool) -> None:
        if self._engine_progress is not None:
            self._engine_progress.dismiss()
            self._engine_progress = None
        # Asked for: every outcome. At start-up: only a new engine, quietly.
        if self._window is not None and (manual or updates.engine_state == "ready"):
            self._window.toast(updates.engine_message)

    def _on_app_checked(self, updates: Updates, _manual: bool) -> None:
        if self._window is None:
            return
        if updates.state == "ready":
            self._window.toast(f"Siphon {updates.latest} Is Ready", "Restart", updates.restart_to_update)
        elif updates.state == "available":
            self._window.toast(f"Siphon {updates.latest} Is Available", "Download",
                               lambda: self._window.open_page(updates.page))


def main(argv: list[str]) -> int:
    return SiphonApp(load_core()).run(argv)
