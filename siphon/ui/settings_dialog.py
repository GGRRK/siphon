"""The Settings dialog behind the header bar's cog (Ctrl+,): Appearance (with the window's Close to Tray),
Equalizer and Updates.

Each control saves through the app's own settings save; the Updates page follows app.updates, and Close to Tray
whether a tray shows Siphon's icon, while it is open.
"""

from gi.repository import Adw, Gtk

from . import appearance
from .equalizer import EqualizerPage
from .updates import Updates

# what sits at the end of the Siphon row in each state; "check" (the Check Now button) in any other
_STATUS_ACTIONS = {"checking": "busy", "downloading": "progress", "ready": "restart", "available": "download"}


def status_line(updates: Updates) -> str:
    """The Siphon row's subtitle: what the last check found, or is doing."""
    return updates.message if updates.state != "idle" else "Not checked since Siphon started."


def status_action(state: str) -> str:
    """The Siphon row's end: a spinner, the download's progress, or the button for the state."""
    return _STATUS_ACTIONS.get(state, "check")


def tray_line(available: bool) -> str:
    """The Close to Tray row's subtitle."""
    if available:
        return "Closing the window keeps Siphon playing and downloading in the tray"
    return "No system tray was found, so closing the window quits Siphon"


def engine_line(updates: Updates) -> str:
    """The engine row's subtitle; an engine fetched earlier still starts next time when a later check failed."""
    if updates.engine_state == "idle":
        return "Not checked since Siphon started."
    if updates.engine_pending and updates.engine_state == "error":
        return f"{updates.engine_message} yt-dlp {updates.engine_pending} will be used from the next start."
    return updates.engine_message


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, app: Adw.Application, window: Gtk.Window, page: str) -> None:
        super().__init__(title="Settings")
        self._tray = TrayRow(app)
        self.add(_appearance_page(self._tray.row))
        self.add(EqualizerPage(app))
        self._updates = UpdatesPage(app, window)
        self.add(self._updates)
        self.set_visible_page_name(page)
        self.connect("closed", self._on_closed)

    def _on_closed(self, _dialog) -> None:
        self._updates.detach()
        self._tray.detach()


def _card(child: Gtk.Widget) -> Gtk.Box:
    # the .appearance class gives the swatches their look (style.css)
    box = Gtk.Box(css_classes=["card", "appearance"])
    child.set_hexpand(True)
    box.append(child)
    return box


def _appearance_page(tray: Gtk.Widget) -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="Appearance", icon_name="preferences-desktop-appearance-symbolic",
                               name="appearance")
    style = Adw.PreferencesGroup(title="Style")
    style.add(_card(appearance.style_choices()))
    page.add(style)
    if appearance.accents_supported():
        accent = Adw.PreferencesGroup(title="Accent Colour")
        accent.add(_card(appearance.accent_choices()))
        page.add(accent)
    window = Adw.PreferencesGroup(title="Window")
    window.add(tray)
    page.add(window)
    return page


class TrayRow:
    """Close to Tray (row); insensitive, saying why, while no tray shows Siphon's icon (closing quits then)."""

    def __init__(self, app: Adw.Application) -> None:
        self.row = Adw.SwitchRow(title="Close to Tray", active=app.prefs.close_to_tray)  # a final class
        self._app = app
        self._tray = app.tray
        self._available = self._tray.connect("notify::available", self._sync) if self._tray is not None else 0
        self.row.connect("notify::active", self._on_active)
        self._sync()

    def detach(self) -> None:
        """The dialog closed: stop following the tray."""
        if self._available:
            self._tray.disconnect(self._available)
            self._available = 0

    def _sync(self, *_args) -> None:
        available = self._tray is not None and self._tray.available
        self.row.set_sensitive(available)
        self.row.set_subtitle(tray_line(available))

    def _on_active(self, row: Adw.SwitchRow, _pspec) -> None:
        self._app.prefs.close_to_tray = row.get_active()
        self._app.save_settings()


class UpdatesPage(Adw.PreferencesPage):
    def __init__(self, app: Adw.Application, window: Gtk.Window) -> None:
        super().__init__(title="Updates", icon_name="software-update-available-symbolic", name="updates")
        self._app = app
        self._updates: Updates = app.updates

        self._auto_update = Adw.SwitchRow(title="Update Siphon Automatically",
                                          subtitle="Checks for a new release every time Siphon starts",
                                          active=app.prefs.auto_update)
        self._auto_update.connect("notify::active", self._on_auto_update)
        self._status = Adw.ActionRow(title=f"Siphon {self._updates.version}")
        self._status_action = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, valign=Gtk.Align.CENTER)
        self._status_action.add_named(Gtk.Button(label="Check Now", action_name="app.check-for-updates"), "check")
        self._status_action.add_named(Adw.Spinner(tooltip_text="Checking for Updates"), "busy")
        self._progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self._status_action.add_named(self._progress, "progress")
        self._status_action.add_named(Gtk.Button(label="Restart to Update", action_name="app.restart-to-update",
                                                 css_classes=["suggested-action"]), "restart")
        download = Gtk.Button(label="Download", tooltip_text="Open the Release Page")
        download.connect("clicked", lambda _button: window.open_page(self._updates.page))
        self._status_action.add_named(download, "download")
        self._status.add_suffix(self._status_action)
        siphon = Adw.PreferencesGroup(title="Siphon")
        siphon.add(self._auto_update)
        siphon.add(self._status)
        self.add(siphon)

        self._auto_engine = Adw.SwitchRow(title="Update Engine Automatically",
                                          subtitle="Fetches the newest yt-dlp every time Siphon starts",
                                          active=app.prefs.auto_engine)
        self._auto_engine.connect("notify::active", self._on_auto_engine)
        self._engine = Adw.ActionRow()
        self._engine_action = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, valign=Gtk.Align.CENTER)
        # the three-dot menu's Update Engine, toasts included
        self._engine_action.add_named(Gtk.Button(label="Update Engine Now", action_name="app.update-engine"),
                                      "update")
        self._engine_action.add_named(Adw.Spinner(tooltip_text="Updating the Engine"), "busy")
        self._engine.add_suffix(self._engine_action)
        engine = Adw.PreferencesGroup(title="Download Engine")
        engine.add(self._auto_engine)
        engine.add(self._engine)
        self.add(engine)

        self._changed = self._updates.connect("changed", self._sync)
        self._sync(self._updates)

    def detach(self) -> None:
        """The dialog closed: stop following the updates."""
        self._updates.disconnect(self._changed)

    def _sync(self, updates: Updates) -> None:
        self._status.set_subtitle(status_line(updates))
        self._status_action.set_visible_child_name(status_action(updates.state))
        self._progress.set_fraction(updates.fraction)
        self._engine.set_title(f"yt-dlp {updates.engine_version}")
        self._engine.set_subtitle(engine_line(updates))
        self._engine_action.set_visible_child_name("busy" if updates.engine_busy else "update")

    def _on_auto_update(self, row: Adw.SwitchRow, _pspec) -> None:
        self._app.prefs.auto_update = row.get_active()
        self._app.save_settings()
        # read at start-up only: switched on before any check this run, check now rather than at the next start
        if row.get_active() and not self._updates.checked:
            self._updates.check_now()

    def _on_auto_engine(self, row: Adw.SwitchRow, _pspec) -> None:
        self._app.prefs.auto_engine = row.get_active()
        self._app.save_settings()
        if row.get_active() and not self._updates.engine_checked:
            self._updates.update_engine_now()
