"""Linux: Siphon's tray icon, a StatusNotifierItem with a com.canonical.dbusmenu menu, published through Gio.

Bars and docks (KDE's, Quickshell, Waybar, GNOME's AppIndicator extension...) show the items that register with
org.kde.StatusNotifierWatcher, the name a bar owns while it runs. The item registers again whenever that name comes
back (a bar restarting) and counts as unavailable while nobody owns it, as on a plain GNOME: closing the window
then quits Siphon instead of hiding it where nothing shows it.
"""

import logging
import os
from pathlib import Path

from gi.repository import Gio, GLib

from . import tray as menus

log = logging.getLogger(__name__)

APP_ID = "io.github.ggrrk.Siphon"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
_SVG = Path(__file__).resolve().parent.parent / "data" / f"{APP_ID}.svg"
_PIXMAP_SIZES = (16, 22, 32, 48)
_NOTCH = 120  # Scroll's delta for one wheel notch: the angle delta Qt reports, which hosts pass on
_INVALID = "org.freedesktop.DBus.Error.InvalidArgs"

_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/>
    </method>
    <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/><arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
    <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
  </interface>
</node>
"""

_MENU_PROPS = {
    "Version": GLib.Variant("u", 3),
    "TextDirection": GLib.Variant("s", "ltr"),
    "Status": GLib.Variant("s", "normal"),
    "IconThemePath": GLib.Variant("as", []),
}


def installed_icon(name: str = APP_ID) -> bool:
    """Whether the desktop's icon theme has Siphon's icon (install.sh puts it in hicolor). A host looks an
    IconName up in the theme only, so a copy running from a clone without install.sh sends pixmaps alone."""
    home = os.environ.get("XDG_DATA_HOME") or ""
    bases = [Path(home) if os.path.isabs(home) else Path.home() / ".local" / "share"]
    bases += [Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
              if os.path.isabs(d)]
    return any(next((base / "icons" / "hicolor").glob(f"*/apps/{name}.*"), None) for base in bases)


def pixmaps(svg: Path = _SVG, sizes: tuple[int, ...] = _PIXMAP_SIZES) -> list[tuple[int, int, bytes]]:
    """The icon as the spec's pixmaps: (width, height, ARGB32 in network byte order) per size, [] on failure."""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        found = []
        for size in sizes:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), size, size)
            found.append((pixbuf.get_width(), pixbuf.get_height(), argb(pixbuf)))
        return found
    except (GLib.Error, ImportError, ValueError) as exc:
        log.warning("the tray icon has no pixmaps: %s", getattr(exc, "message", exc))
        return []


def argb(pixbuf) -> bytes:
    """A GdkPixbuf's RGB(A) rows as ARGB32, big-endian."""
    width, height, stride, channels = (pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride(),
                                       pixbuf.get_n_channels())
    data = pixbuf.read_pixel_bytes().get_data()
    rows = b"".join(data[y * stride:y * stride + width * channels] for y in range(height))
    out = bytearray(width * height * 4)
    out[0::4] = rows[3::channels] if channels == 4 else b"\xff" * (width * height)
    out[1::4], out[2::4], out[3::4] = rows[0::channels], rows[1::channels], rows[2::channels]
    return bytes(out)


def _props(item: menus.Item, names: list[str]) -> dict[str, GLib.Variant]:
    if item.id == 0:  # the root
        props = {"children-display": GLib.Variant("s", "submenu")}
    elif item.separator:
        props = {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", True)}
    else:
        # a single underscore marks a mnemonic: a song's own ones are doubled
        props = {"label": GLib.Variant("s", item.label.replace("_", "__")),
                 "enabled": GLib.Variant("b", item.enabled), "visible": GLib.Variant("b", True)}
    return {name: value for name, value in props.items() if not names or name in names}


class StatusNotifierItem:
    """The tray icon on the session bus for as long as it lives; a bus error leaves it unavailable."""

    session_ending = False  # a Windows matter

    def __init__(self, bus: Gio.DBusConnection, tray: menus.Tray) -> None:
        self._bus = bus
        self._tray = tray
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._state = tray.state
        self._items = menus.menu(self._state)
        self._revision = 1
        self._icon_name = APP_ID if installed_icon() else ""
        self._pixmaps: GLib.Variant | None = None  # rendered on the first request
        self._objects: list[int] = []
        self._owner = self._watch = self._signals = 0
        self._watcher = ""  # the unique name of the watcher while there is one
        self._closed = False
        info = Gio.DBusNodeInfo.new_for_xml(_XML)
        try:
            self._objects.append(bus.register_object(ITEM_PATH, info.lookup_interface(ITEM_IFACE),
                                                     self._on_item_call, self._on_item_property, None))
            self._objects.append(bus.register_object(MENU_PATH, info.lookup_interface(MENU_IFACE),
                                                     self._on_menu_call, self._on_menu_property, None))
        except GLib.Error as exc:
            log.warning("no tray icon: %s", exc.message)
            self.close()
            return
        self._signals = bus.signal_subscribe(None, WATCHER, None, WATCHER_PATH, None, Gio.DBusSignalFlags.NONE,
                                             self._on_watcher_signal)
        self._owner = Gio.bus_own_name_on_connection(bus, self._name, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                                                     self._on_name_acquired, self._on_name_lost)

    @property
    def name(self) -> str:
        return self._name

    def update(self, state: menus.State) -> None:
        if self._closed:
            return
        before, self._state = self._state, state
        old = {item.id: item for item in self._items}
        self._items = menus.menu(state)
        if list(old) != [item.id for item in self._items]:  # the song's label came or went
            self._revision += 1
            self._emit(MENU_PATH, MENU_IFACE, "LayoutUpdated", GLib.Variant("(ui)", (self._revision, 0)))
        else:
            # every property of each changed item: hosts reset the ones an update leaves out
            changed = [(item.id, _props(item, [])) for item in self._items if old[item.id] != item]
            if changed:
                self._emit(MENU_PATH, MENU_IFACE, "ItemsPropertiesUpdated",
                           GLib.Variant("(a(ia{sv})a(ias))", (changed, [])))
        if menus.title(before) != menus.title(state):
            self._emit(ITEM_PATH, ITEM_IFACE, "NewTitle", None)
            self._emit(ITEM_PATH, ITEM_IFACE, "NewToolTip", None)

    def balloon(self, _heading: str, _body: str) -> bool:
        return False  # the app sends the desktop a notification of its own

    def quitting(self) -> None:
        pass  # a Windows matter: GApplication keeps one Siphon per session on Linux

    def close(self) -> None:
        self._closed = True
        if self._signals:
            self._bus.signal_unsubscribe(self._signals)
            self._signals = 0
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = 0
        for registration in self._objects:
            self._bus.unregister_object(registration)
        self._objects.clear()

    def _emit(self, path: str, iface: str, signal: str, args: GLib.Variant | None) -> None:
        try:
            self._bus.emit_signal(None, path, iface, signal, args)
        except GLib.Error as exc:
            log.warning("tray: %s failed: %s", signal, exc.message)

    # -- registration with the watcher

    def _on_name_acquired(self, _bus: Gio.DBusConnection, _name: str) -> None:
        if not self._closed and not self._watch:
            self._watch = Gio.bus_watch_name_on_connection(self._bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                           self._on_watcher_appeared, self._on_watcher_vanished)

    def _on_name_lost(self, _bus: Gio.DBusConnection, name: str) -> None:
        # never while the bus lives: the name has our process id in it
        log.warning("tray: lost the bus name %s", name)
        self._tray.set_available(False)

    def _on_watcher_appeared(self, _bus: Gio.DBusConnection, _name: str, owner: str) -> None:
        self._watcher = owner
        self._bus.call(owner, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self._name,)), None, Gio.DBusCallFlags.NONE, 5000, None,
                       self._on_registered, owner)

    def _on_watcher_vanished(self, _bus: Gio.DBusConnection, _name: str) -> None:
        self._watcher = ""
        if not self._closed:
            self._tray.set_available(False)

    def _on_registered(self, bus: Gio.DBusConnection, result: Gio.AsyncResult, owner: str) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            if not self._closed and owner == self._watcher:
                log.warning("tray: the watcher refused the icon: %s", exc.message)
                self._tray.set_available(False)
            return
        self._check_host(owner)

    def _check_host(self, owner: str) -> None:
        """A watcher with no bar hosting its items shows nothing; the spec has it say so."""
        self._bus.call(owner, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get",
                       GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")), GLib.VariantType("(v)"),
                       Gio.DBusCallFlags.NONE, 5000, None, self._on_host_checked, owner)

    def _on_host_checked(self, bus: Gio.DBusConnection, result: Gio.AsyncResult, owner: str) -> None:
        try:
            hosted = bool(bus.call_finish(result).unpack()[0])
        except GLib.Error:
            hosted = True  # a watcher without the property accepted the icon: trust it
        if not self._closed and owner == self._watcher:
            self._tray.set_available(hosted)

    def _on_watcher_signal(self, _bus, sender: str, _path: str, _iface: str, signal: str, _args) -> None:
        if sender == self._watcher and signal in ("StatusNotifierHostRegistered", "StatusNotifierHostUnregistered"):
            self._check_host(sender)

    # -- the item

    def _on_item_property(self, _bus, _sender, _path, _iface, name: str) -> GLib.Variant | None:
        state = self._state
        match name:
            case "Category":
                return GLib.Variant("s", "ApplicationStatus")
            case "Id":
                return GLib.Variant("s", APP_ID)
            case "Title":
                return GLib.Variant("s", menus.title(state))
            case "Status":
                return GLib.Variant("s", "Active")
            case "WindowId":
                return GLib.Variant("i", 0)
            case "IconName":
                return GLib.Variant("s", self._icon_name)
            case "IconPixmap":
                if self._pixmaps is None:
                    self._pixmaps = GLib.Variant("a(iiay)", pixmaps())
                return self._pixmaps
            case "OverlayIconName" | "AttentionIconName" | "AttentionMovieName":
                return GLib.Variant("s", "")
            case "OverlayIconPixmap" | "AttentionIconPixmap":
                return GLib.Variant("a(iiay)", [])
            case "ToolTip":
                return GLib.Variant("(sa(iiay)ss)", ("", [], "Siphon", state.song))
            case "ItemIsMenu":
                return GLib.Variant("b", False)
            case "Menu":
                return GLib.Variant("o", MENU_PATH)
        return None

    def _on_item_call(self, _bus, _sender, _path, _iface, method: str, args: GLib.Variant,
                      invocation: Gio.DBusMethodInvocation) -> None:
        match method:
            case "Activate" | "ContextMenu":  # ContextMenu: a host without dbusmenu support
                self._tray.run(menus.SHOW)
            case "SecondaryActivate":
                self._tray.run(menus.PLAY_PAUSE)
            case "Scroll":
                delta, orientation = args.unpack()
                if orientation.lower() == "vertical" and delta:
                    self._tray.scroll(delta / _NOTCH)
            case "ProvideXdgActivationToken":
                self._tray.set_token(args.unpack()[0])
        invocation.return_value(None)

    # -- the menu

    def _find(self, item_id: int) -> menus.Item | None:
        if item_id == 0:
            return menus.Item(0, "Siphon")
        return next((item for item in self._items if item.id == item_id), None)

    def _layout(self, item: menus.Item, depth: int, names: list[str]) -> tuple:
        children = self._items if item.id == 0 and depth != 0 else []
        return (item.id, _props(item, names),
                [GLib.Variant("(ia{sv}av)", self._layout(child, depth - 1, names)) for child in children])

    def _on_menu_property(self, _bus, _sender, _path, _iface, name: str) -> GLib.Variant | None:
        return _MENU_PROPS.get(name)

    def _on_menu_call(self, _bus, _sender, _path, _iface, method: str, args: GLib.Variant,
                      invocation: Gio.DBusMethodInvocation) -> None:
        match method:
            case "GetLayout":
                parent, depth, names = args.unpack()
                item = self._find(parent)
                if item is None:
                    invocation.return_dbus_error(_INVALID, f"No menu item {parent}")
                    return
                reply = GLib.Variant("(u(ia{sv}av))", (self._revision, self._layout(item, depth, names)))
            case "GetGroupProperties":
                ids, names = args.unpack()
                found = [self._find(i) for i in ids] if ids else [self._find(0), *self._items]
                reply = GLib.Variant("(a(ia{sv}))", ([(item.id, _props(item, names)) for item in found if item],))
            case "GetProperty":
                item_id, name = args.unpack()
                value = _props(item, [name]).get(name) if (item := self._find(item_id)) else None
                if value is None:
                    invocation.return_dbus_error(_INVALID, f"No property {name} on menu item {item_id}")
                    return
                reply = GLib.Variant("(v)", (value,))
            case "Event":
                item_id, event, _data, _time = args.unpack()
                if self._find(item_id) is None:
                    invocation.return_dbus_error(_INVALID, f"No menu item {item_id}")
                    return
                self._event(item_id, event)
                reply = None
            case "EventGroup":
                events = args.unpack()[0]
                errors = [item_id for item_id, *_rest in events if self._find(item_id) is None]
                if events and len(errors) == len(events):
                    invocation.return_dbus_error(_INVALID, "None of the menu items exist")
                    return
                for item_id, event, _data, _time in events:
                    if item_id not in errors:
                        self._event(item_id, event)
                reply = GLib.Variant("(ai)", (errors,))
            case "AboutToShow":
                reply = GLib.Variant("(b)", (False,))  # the menu is always current: changes are signalled
            case "AboutToShowGroup":
                errors = [item_id for item_id in args.unpack()[0] if self._find(item_id) is None]
                reply = GLib.Variant("(aiai)", ([], errors))
            case _:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
                return
        invocation.return_value(reply)

    def _event(self, item_id: int, event: str) -> None:
        item = self._find(item_id)
        if event == "clicked" and item.command and item.enabled:
            # after the reply: the host's menu closes first, then the window opens or Siphon quits
            GLib.idle_add(self._run, item.command)

    def _run(self, command: str) -> bool:
        if not self._closed:
            self._tray.run(command)
        return GLib.SOURCE_REMOVE
