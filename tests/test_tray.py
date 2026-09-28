"""The tray icon: its menu, and the Linux StatusNotifierItem end to end on a private D-Bus.

The bus is our own dbus-daemon (conftest.py). A fake org.kde.StatusNotifierWatcher and a dbusmenu client live on
connections of their own in this process, like a bar would, so nothing here touches the desktop's session bus.
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib, GObject

from siphon import linuxtray, sni, tray
from siphon.bus import export

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning:gi.events")  # MainContext.iteration()

WATCHER_XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
    <method name="RegisterStatusNotifierHost"><arg name="service" type="s" direction="in"/></method>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierHostRegistered"/>
  </interface>
</node>
"""


class Player(GObject.Object):
    """Just what the tray reads of the player."""
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.state, self.current, self.next_ok, self.volume = "stopped", None, False, 0.5

    def has_next(self) -> bool:
        return self.next_ok

    def load(self, title: str, artist: str = "", state: str = "playing", next_ok: bool = True) -> None:
        self.current = SimpleNamespace(title=title, artist=artist)
        self.state, self.next_ok = state, next_ok
        self.emit("changed")


def song_state(**fields) -> tray.State:
    return tray.State(**({"song": "Song – Artist", "can_play": True, "can_previous": True} | fields))


# ---------------------------------------------------------------- the menu


def test_the_menu_without_a_song():
    items = tray.menu(tray.State())
    assert [(i.id, i.label, i.enabled, i.command) for i in items] == [
        (1, "Show Siphon", True, "show"), (3, "", True, ""), (4, "Play", False, "play-pause"),
        (5, "Next", False, "next"), (6, "Previous", False, "previous"), (7, "", True, ""),
        (8, "Quit Siphon", True, "quit")]
    assert [i.separator for i in items] == [False, True, False, False, False, True, False]


def test_the_menu_names_the_song_and_follows_the_player():
    items = tray.menu(song_state(playing=True, can_next=True))
    assert [(i.id, i.label, i.enabled) for i in items[:5]] == [
        (1, "Show Siphon", True), (2, "Song – Artist", False), (3, "", True), (4, "Pause", True), (5, "Next", True)]
    assert tray.menu(song_state(playing=False))[3].label == "Play"
    long = tray.menu(song_state(song="x" * 200))[1].label
    assert len(long) == 60 and long.endswith("…")


def test_state_and_title_from_the_player():
    player = Player()
    assert tray.state_of(player) == tray.State() and tray.title(tray.State()) == "Siphon"
    player.load("Title", "Artist", "paused", next_ok=False)
    state = tray.state_of(player)
    assert state == tray.State("Title – Artist", playing=False, can_play=True, can_next=False, can_previous=True)
    assert tray.title(state) == "Siphon: Title – Artist"
    player.load("No Artist")
    assert tray.state_of(player).song == "No Artist"


def test_the_ids_are_the_commands():
    assert {tray.COMMANDS[number] for number in tray.IDS.values()} == {"show", "play-pause", "next", "previous",
                                                                       "quit"}
    assert tray.IDS[tray.QUIT] == 8  # packaging/windows/smoke.ps1 posts it as WM_COMMAND


class FakeBackend:
    session_ending = False

    def __init__(self, owner: tray.Tray) -> None:
        self.owner, self.updates, self.closed = owner, [], 0

    def update(self, state):
        self.updates.append(state)

    def balloon(self, heading, body):
        return True

    def close(self):
        self.closed += 1


def test_the_tray_updates_its_backend_only_on_real_changes():
    player = Player()
    backends = []
    icon = tray.Tray(player, lambda owner: backends.append(FakeBackend(owner)) or backends[-1])
    backend = backends[0]
    player.emit("changed")  # a volume change: nothing the tray shows
    assert backend.updates == []
    player.load("Title", "Artist")
    player.emit("changed")
    assert backend.updates == [tray.state_of(player)]
    seen = []
    icon.connect("command", lambda _t, command: seen.append(command))
    icon.connect("scroll", lambda _t, notches: seen.append(notches))
    icon.connect("open", lambda _t, args: seen.append(args))
    icon.connect("session-end", lambda _t: seen.append("end"))
    icon.run("quit"), icon.run("bogus"), icon.scroll(-1.0), icon.open(["a"]), icon.end_session()
    icon.set_available(True)
    icon.set_token("tok")
    assert seen == ["quit", -1.0, ["a"], "end"] and icon.available
    assert (icon.take_token(), icon.take_token()) == ("tok", "")
    icon.close()
    icon.close()
    icon.run("quit")
    player.load("Other")
    assert backend.closed == 1 and seen == ["quit", -1.0, ["a"], "end"] and len(backend.updates) == 1
    assert not icon.available


def test_no_tray_when_switched_off_or_without_a_bus(monkeypatch):
    monkeypatch.setenv("SIPHON_NO_TRAY", "1")
    assert tray.create(Player(), object()) is None
    monkeypatch.delenv("SIPHON_NO_TRAY")
    monkeypatch.delenv("SIPHON_XEMBED", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    assert tray.create(Player(), None) is None  # nor X to look on: GTK is not on X11 here


def test_without_a_bus_the_x_tray_alone_and_no_x_server_is_no_harm(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("SIPHON_XEMBED", "1")
    monkeypatch.setenv("DISPLAY", ":97")  # no such server
    icon = tray.create(Player(), None)
    assert icon is not None and icon._backend.name == "" and icon._backend.xembed is None
    assert not icon.available
    icon.close()


def test_the_toggle_command_passes():
    icon = tray.Tray(Player(), FakeBackend)
    seen = []
    icon.connect("command", lambda _t, command: seen.append(command))
    icon.run(tray.TOGGLE)
    assert seen == [tray.TOGGLE]


# ---------------------------------------------------------------- icons


def test_pixmaps_are_argb_big_endian_at_four_sizes():
    found = sni.pixmaps()
    assert [(w, h) for w, h, _ in found] == [(16, 16), (22, 22), (32, 32), (48, 48)]
    for w, h, data in found:
        assert len(data) == w * h * 4
    w, h, data = found[-1]
    middle = (h // 2 * w + w // 2) * 4
    assert data[middle] == 255  # opaque in the middle of the icon: alpha comes first
    assert data[0] == 0  # and transparent in the corner of its rounded square


def fake_pixbuf(width: int, height: int, stride: int, channels: int, data: list[int]) -> SimpleNamespace:
    return SimpleNamespace(get_width=lambda: width, get_height=lambda: height, get_rowstride=lambda: stride,
                           get_n_channels=lambda: channels, read_pixel_bytes=lambda: GLib.Bytes.new(bytes(data)))


def test_argb_reorders_rgba_and_fills_alpha():
    assert sni.argb(fake_pixbuf(2, 1, 8, 4, [1, 2, 3, 4, 5, 6, 7, 8])) == bytes([4, 1, 2, 3, 8, 5, 6, 7])
    rgb = fake_pixbuf(1, 2, 4, 3, [1, 2, 3, 0, 4, 5, 6, 0])  # rows padded to their stride
    assert sni.argb(rgb) == bytes([255, 1, 2, 3, 255, 4, 5, 6])


def test_the_icon_name_is_used_only_when_the_theme_has_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    assert not sni.installed_icon()
    apps = tmp_path / "system" / "icons" / "hicolor" / "scalable" / "apps"
    apps.mkdir(parents=True)
    (apps / f"{sni.APP_ID}.svg").write_text("<svg/>")
    assert sni.installed_icon()


# ---------------------------------------------------------------- the item on a private bus


def pump(until, timeout: float = 3.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "timed out"
        context.iteration(False) or time.sleep(0.005)

def linger(seconds: float = 0.2) -> None:
    """Run the main loop a while: for checks that something did not happen."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)



def connect(address: str) -> Gio.DBusConnection:
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    return Gio.DBusConnection.new_for_address_sync(address, flags, None, None)


class Watcher:
    """org.kde.StatusNotifierWatcher as a bar owns it, on a connection of its own."""

    def __init__(self, address: str, hosted: bool = True) -> None:
        self.bus = connect(address)
        self.hosted, self.items, self.asked = hosted, [], []
        iface = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        self._object = export(self.bus, sni.WATCHER_PATH, iface, self._call, self._get, None)
        self._owner = Gio.bus_own_name_on_connection(self.bus, sni.WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)

    def _call(self, _bus, sender, _path, _iface, method, args, invocation):
        if method == "RegisterStatusNotifierItem":
            self.items.append((sender, args.unpack()[0]))
        invocation.return_value(None)

    def _get(self, _bus, _sender, _path, _iface, name):
        self.asked.append(name)
        return GLib.Variant("b", self.hosted) if name == "IsStatusNotifierHostRegistered" else GLib.Variant("i", 0)

    def host_arrives(self) -> None:
        self.hosted = True
        self.bus.emit_signal(None, sni.WATCHER_PATH, sni.WATCHER, "StatusNotifierHostRegistered", None)

    def stop(self) -> None:
        Gio.bus_unown_name(self._owner)
        self.bus.unregister_object(self._object)
        self.bus.close_sync(None)


class Client:
    """A bar's side: reads the item and drives its menu, and hears its signals."""

    def __init__(self, address: str, service: str) -> None:
        self.bus = connect(address)
        self.service = service
        self.signals = []
        self._subscription = self.bus.signal_subscribe(None, None, None, None, None, Gio.DBusSignalFlags.NONE,
                                                       self._heard)

    def _heard(self, _bus, _sender, path, iface, name, args):
        if iface in (sni.ITEM_IFACE, sni.MENU_IFACE):
            self.signals.append((path, name, args.unpack() if args is not None else ()))

    def call(self, path: str, iface: str, method: str, args: GLib.Variant | None = None,
             service: str | None = None):
        """Asynchronously, pumping this thread's main loop meanwhile: the item answers on it."""
        done = []
        self.bus.call(service or self.service, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 3000, None,
                      lambda bus, result: done.append(result))
        pump(lambda: done)
        return self.bus.call_finish(done[0]).unpack()

    def item(self, method: str, signature: str = "", *args):
        return self.call(sni.ITEM_PATH, sni.ITEM_IFACE, method, GLib.Variant(f"({signature})", args) if signature
                         else None)

    def menu(self, method: str, signature: str, *args):
        return self.call(sni.MENU_PATH, sni.MENU_IFACE, method, GLib.Variant(f"({signature})", args))

    def props(self, iface: str = sni.ITEM_IFACE, path: str = sni.ITEM_PATH) -> dict:
        return self.call(path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (iface,)))[0]

    def layout(self, parent: int = 0, depth: int = -1, names: list[str] | None = None):
        return self.menu("GetLayout", "iias", parent, depth, names or [])

    def click(self, item_id: int) -> None:
        self.menu("Event", "isvu", item_id, "clicked", GLib.Variant("i", 0), 0)

    def close(self) -> None:
        self.bus.signal_unsubscribe(self._subscription)
        self.bus.close_sync(None)


@pytest.fixture
def item(bus):
    """A Tray with a StatusNotifierItem on its own connection; its commands, scrolls and availability recorded."""
    connection = connect(bus)
    player = Player()
    heard = []
    icon = tray.Tray(player, lambda owner: sni.StatusNotifierItem(connection, owner))
    icon.connect("command", lambda _t, command: heard.append(command))
    icon.connect("scroll", lambda _t, notches: heard.append(notches))
    made = SimpleNamespace(tray=icon, player=player, heard=heard, name=icon._backend.name, bus=bus, closers=[])
    yield made
    for close in made.closers:
        close()
    icon.close()
    connection.close_sync(None)


def start_watcher(item, hosted: bool = True) -> Watcher:
    watcher = Watcher(item.bus, hosted)
    item.closers.append(lambda: watcher.bus.is_closed() or watcher.stop())
    return watcher


def client_of(item) -> Client:
    client = Client(item.bus, item.name)
    item.closers.append(client.close)
    return client


def test_it_registers_with_the_watcher_and_again_when_the_bar_restarts(item):
    assert not item.tray.available  # no bar yet: closing the window quits
    first = start_watcher(item)
    pump(lambda: item.tray.available)
    assert [name for _sender, name in first.items] == [item.name]
    assert item.name.startswith("org.kde.StatusNotifierItem-") and item.name.endswith("-1")
    first.stop()  # the bar quits or restarts
    pump(lambda: not item.tray.available)
    second = start_watcher(item)
    pump(lambda: item.tray.available)
    assert [name for _sender, name in second.items] == [item.name]


def test_a_watcher_without_a_bar_hosting_it_shows_nothing(item):
    watcher = start_watcher(item, hosted=False)
    pump(lambda: "IsStatusNotifierHostRegistered" in watcher.asked)
    linger()  # the answer arrives
    assert not item.tray.available
    watcher.host_arrives()
    pump(lambda: item.tray.available)


def test_the_items_properties(item):
    client = client_of(item)
    props = client.props()
    assert {k: props[k] for k in ("Category", "Id", "Title", "Status", "ItemIsMenu", "Menu", "WindowId")} == {
        "Category": "ApplicationStatus", "Id": "io.github.ggrrk.Siphon", "Title": "Siphon", "Status": "Active",
        "ItemIsMenu": False, "Menu": "/MenuBar", "WindowId": 0}
    assert props["ToolTip"] == ("", [], "Siphon", "")
    assert props["IconName"] in ("", "io.github.ggrrk.Siphon")
    assert [(w, h, len(data)) for w, h, data in props["IconPixmap"]] == [
        (16, 16, 1024), (22, 22, 1936), (32, 32, 4096), (48, 48, 9216)]
    assert client.props(sni.MENU_IFACE, sni.MENU_PATH) == {"Version": 3, "TextDirection": "ltr", "Status": "normal",
                                                           "IconThemePath": []}
    item.player.load("Title", "Artist")
    pump(lambda: len(client.signals) >= 3)
    assert client.props()["Title"] == "Siphon: Title – Artist"
    assert client.props()["ToolTip"] == ("", [], "Siphon", "Title – Artist")
    assert ("/StatusNotifierItem", "NewTitle", ()) in client.signals
    assert ("/StatusNotifierItem", "NewToolTip", ()) in client.signals


def test_clicks_on_the_icon(item):
    client = client_of(item)
    client.item("Activate", "ii", 0, 0)
    client.item("SecondaryActivate", "ii", 0, 0)
    client.item("ContextMenu", "ii", 5, 5)  # a host without dbusmenu: the window at least
    client.item("Scroll", "is", 240, "vertical")
    client.item("Scroll", "is", -120, "Vertical")
    client.item("Scroll", "is", 120, "horizontal")
    client.item("ProvideXdgActivationToken", "s", "wayland-token")
    pump(lambda: len(item.heard) >= 5)
    assert item.heard == ["show", "play-pause", "show", 2.0, -1.0]
    assert item.tray.take_token() == "wayland-token"


def labels(layout) -> list[tuple]:
    _revision, (root, props, children) = layout
    return [(c[0], c[1].get("label"), c[1].get("enabled"), c[1].get("type")) for c in children]


def test_the_menus_layout(item):
    client = client_of(item)
    revision, (root, props, children) = client.layout()
    assert (root, props) == (0, {"children-display": "submenu"})
    assert labels(client.layout()) == [
        (1, "Show Siphon", True, None), (3, None, None, "separator"), (4, "Play", False, None),
        (5, "Next", False, None), (6, "Previous", False, None), (7, None, None, "separator"),
        (8, "Quit Siphon", True, None)]
    assert all(child[2] == [] and child[1].get("visible") is True for child in children)
    assert client.layout(depth=0)[1][2] == []  # no children asked for
    assert client.layout(names=["label"])[1][2][0][1] == {"label": "Show Siphon"}
    assert client.layout(parent=8)[1] == (8, {"label": "Quit Siphon", "enabled": True, "visible": True}, [])
    with pytest.raises(GLib.Error, match="InvalidArgs"):
        client.layout(parent=42)
    assert client.menu("AboutToShow", "i", 0) == (False,)
    assert client.menu("GetProperty", "is", 4, "label") == ("Play",)
    with pytest.raises(GLib.Error, match="InvalidArgs"):
        client.menu("GetProperty", "is", 4, "icon-name")
    group = dict(client.menu("GetGroupProperties", "aias", [1, 8], ["label"])[0])
    assert group == {1: {"label": "Show Siphon"}, 8: {"label": "Quit Siphon"}}
    assert len(client.menu("GetGroupProperties", "aias", [], [])[0]) == 8  # the root and every item
    assert client.menu("AboutToShowGroup", "ai", [0, 1, 99]) == ([], [99])
    item.player.load("A_B & C", "D")  # an underscore is a mnemonic in dbusmenu: doubled
    pump(lambda: len(labels(client.layout())) == 8)
    assert labels(client.layout())[1] == (2, "A__B & C – D", False, None)


def test_menu_items_run_their_commands(item):
    client = client_of(item)
    item.player.load("Song", "Artist", next_ok=True)
    pump(lambda: len(labels(client.layout())) == 8)
    for item_id in (1, 2, 3, 4, 5, 6, 7, 8):
        client.click(item_id)
    pump(lambda: len(item.heard) >= 5)
    linger()
    assert item.heard == ["show", "play-pause", "next", "previous", "quit"]  # the label and separators do nothing
    client.menu("Event", "isvu", 1, "hovered", GLib.Variant("i", 0), 0)
    with pytest.raises(GLib.Error, match="InvalidArgs"):
        client.click(99)
    assert client.menu("EventGroup", "a(isvu)", [(8, "clicked", GLib.Variant("i", 0), 0),
                                                  (99, "clicked", GLib.Variant("i", 0), 0)]) == ([99],)
    with pytest.raises(GLib.Error, match="InvalidArgs"):
        client.menu("EventGroup", "a(isvu)", [(99, "clicked", GLib.Variant("i", 0), 0)])
    pump(lambda: len(item.heard) == 6)
    assert item.heard[-1] == "quit"


def test_disabled_items_do_nothing(item):
    client = client_of(item)
    for item_id in (4, 5, 6):  # nothing loaded: Play, Next and Previous are off
        client.click(item_id)
    client.click(8)
    pump(lambda: item.heard)
    assert item.heard == ["quit"]


def test_changes_are_signalled(item):
    client = client_of(item)
    item.player.load("Song", "Artist", "paused", next_ok=False)  # the song's label appears: a new layout
    pump(lambda: any(name == "LayoutUpdated" for _p, name, _a in client.signals))
    assert ("/MenuBar", "LayoutUpdated", (2, 0)) in client.signals
    assert client.layout()[0] == 2
    client.signals.clear()
    item.player.state = "playing"  # the same items, a label and a sensitivity change
    item.player.next_ok = True
    item.player.emit("changed")
    pump(lambda: client.signals)
    assert client.signals == [("/MenuBar", "ItemsPropertiesUpdated", (
        [(4, {"label": "Pause", "enabled": True, "visible": True}),
         (5, {"label": "Next", "enabled": True, "visible": True})], []))]
    client.signals.clear()
    item.player.emit("changed")  # the volume, say: nothing the tray shows
    linger()
    assert client.signals == []


def test_closing_releases_the_name(item):
    client = client_of(item)

    def owned() -> bool:
        return client.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                           GLib.Variant("(s)", (item.name,)), service="org.freedesktop.DBus")[0]

    assert owned()
    item.tray.close()
    pump(lambda: not owned())


def test_no_pixmaps_from_a_missing_file(tmp_path, caplog):
    assert sni.pixmaps(Path(tmp_path / "none.svg")) == []
    assert "no pixmaps" in caplog.text


# ---------------------------------------------------------------- Linux: StatusNotifierItem, else XEmbed


class FakeXIcon:
    """siphon.xembed.XEmbedIcon's side: a tray that embeds it at once when enabled (docks) or never."""

    def __init__(self, owner, docks: bool = True) -> None:
        self.owner, self.docks, self.enabled, self.updates, self.closed = owner, docks, False, [], 0

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.owner.set_available(enabled and self.docks)

    def update(self, state) -> None:
        self.updates.append(state)

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def linux(bus):
    """A Tray over LinuxTray on its own connection, with a fake XEmbed icon when one is asked for."""
    connection = connect(bus)
    made = SimpleNamespace(icons=[], heard=[], closers=[], bus=bus, player=Player(), docks=True, x_there=True)

    def start_x(owner):
        if not made.x_there:
            return None
        made.icons.append(FakeXIcon(owner, made.docks))
        return made.icons[-1]

    made.start = lambda: setattr(made, "tray", tray.Tray(made.player, lambda owner: linuxtray.LinuxTray(
        owner, connection, start_x)))
    yield made
    for close in made.closers:
        close()
    if hasattr(made, "tray"):
        made.tray.close()
    connection.close_sync(None)


def test_without_a_bar_the_x_tray_takes_over_and_gives_way_to_one(linux):
    linux.start()
    pump(lambda: linux.icons and linux.icons[0].enabled and linux.tray.available)
    x = linux.icons[0]
    watcher = start_watcher(linux)  # a bar with StatusNotifierItems starts
    pump(lambda: watcher.items and not x.enabled)
    assert linux.tray.available  # never unavailable in between: the item was hosted before the X icon left
    watcher.stop()  # and quits
    pump(lambda: x.enabled)
    assert linux.tray.available and len(linux.icons) == 1  # the same X icon again


def test_a_watcher_without_a_bar_leaves_the_x_tray_in_charge(linux):
    start_watcher(linux, hosted=False)
    linux.start()
    pump(lambda: linux.icons and linux.icons[0].enabled and linux.tray.available)
    linger()
    assert linux.icons[0].enabled


def test_a_bar_from_the_start_needs_no_x(linux):
    watcher = start_watcher(linux)
    linux.start()
    pump(lambda: linux.tray.available and watcher.items)
    linger()
    assert linux.icons == []


def test_neither_kind_leaves_it_unavailable(linux):
    linux.x_there = False
    linux.start()
    linger(0.5)
    assert not linux.tray.available
    linux.x_there, linux.docks = True, False
    other = tray.Tray(Player(), lambda owner: linuxtray.LinuxTray(owner, None, lambda o: FakeXIcon(o, docks=False)))
    assert not other.available and other._backend.xembed.enabled
    other.close()


def test_the_x_icons_clicks_and_updates_go_through(linux):
    linux.start()
    pump(lambda: linux.icons and linux.tray.available)
    x = linux.icons[0]
    linux.tray.connect("command", lambda _t, command: linux.heard.append(command))
    linux.tray.connect("scroll", lambda _t, notches: linux.heard.append(notches))
    x.owner.run(tray.TOGGLE)
    x.owner.run(tray.QUIT)
    x.owner.scroll(-1.0)
    assert linux.heard == [tray.TOGGLE, tray.QUIT, -1.0] and x.owner.state == tray.State()
    linux.player.load("Title", "Artist")
    assert x.updates == [tray.state_of(linux.player)]
    linux.tray.close()
    assert x.closed == 1 and not linux.tray.available
