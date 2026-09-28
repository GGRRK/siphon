"""The XEmbed tray icon (siphon/xembed.py) against a real X server nobody sees and a legacy tray on it
(tests/xtray.py): it docks, draws the Siphon icon over the bar or in the bar's ARGB visual, takes its clicks, runs
its menu by pointer and keyboard, shows its tooltip, and follows the tray as it quits, restarts or never embeds."""

import sys
import time

import pytest
from gi.repository import GLib

from siphon import tray as menus, xembed

pytestmark = pytest.mark.linux

KEYSYMS = {"down": 0xFF54, "up": 0xFF52, "enter": 0xFF0D, "escape": 0xFF1B}


class Owner:
    """What the icon reports to: tray.Tray's side, recorded."""

    def __init__(self, state: menus.State) -> None:
        self.state = state
        self.available = False
        self.log: list[tuple] = []

    def run(self, command: str) -> None:
        self.log.append(("run", command))

    def scroll(self, notches: float) -> None:
        self.log.append(("scroll", notches))

    def set_token(self, token: str) -> None:
        self.log.append(("token", token))

    def set_available(self, available: bool) -> None:
        self.available = available
        self.log.append(("available", available))

    def commands(self) -> list:
        return [entry for entry in self.log if entry[0] in ("run", "scroll")]

    def tokens(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "token"]


PLAYING = menus.State(song="A Song – Someone", playing=True, can_play=True, can_next=True, can_previous=True)


def pump(until, managers=(), timeout: float = 3.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for manager in managers:
            manager.pump()
        if until():
            return True
        context.iteration(False) or time.sleep(0.005)
    return until()


def linger(managers=(), seconds: float = 0.3) -> None:
    pump(lambda: False, managers, seconds)


@pytest.fixture
def x(x_display):
    """An icon on its own connection (disabled until the test enables it), the tray managers the test starts,
    all gone afterwards."""
    from xtray import TrayManager

    made = []

    class Made:
        display = x_display
        owner = Owner(PLAYING)
        icon = xembed.XEmbedIcon(owner, xembed._Connection(xembed._load(), x_display))

        @staticmethod
        def manager(**options):
            made.append(TrayManager(x_display, **options))
            return made[-1]

    yield Made
    Made.icon.close()
    for manager in made:
        try:
            manager.stop()
        except Exception:
            pass


def docked(x, manager) -> bool:
    return pump(lambda: x.icon.docked and x.owner.available and manager.icons, [manager])


def expected_pixels(side: int, background: int | None) -> list[tuple[int, int, int, int]]:
    """The SVG rendered independently: RGB over the background, or premultiplied RGBA without one."""
    from gi.repository import GdkPixbuf

    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(xembed._SVG), side, side)
    data, stride, channels = pixbuf.read_pixel_bytes().get_data(), pixbuf.get_rowstride(), pixbuf.get_n_channels()
    out = []
    back = [(background >> shift) & 0xFF for shift in (16, 8, 0)] if background is not None else None
    for y in range(side):
        for x in range(side):
            r, g, b, a = data[y * stride + x * channels:y * stride + x * channels + 4]
            if back is None:
                out.append((r * a / 255, g * a / 255, b * a / 255, a))
            else:
                out.append(tuple(c * a / 255 + bg * (255 - a) / 255 for c, bg in zip((r, g, b), back)) + (255,))
    return out


def shown_pixels(width: int, height: int, data: bytes) -> list[tuple[int, int, int, int]]:
    return [(data[i + 2], data[i + 1], data[i], data[i + 3]) for i in range(0, width * height * 4, 4)]


def worst(expected, shown, alpha: bool) -> float:
    channels = 4 if alpha else 3
    return max(abs(e[c] - s[c]) for e, s in zip(expected, shown) for c in range(channels))


# ---------------------------------------------------------------- docking


def test_it_docks_into_the_tray_once_enabled(x):
    manager = x.manager()
    linger([manager])
    assert manager.requests == [] and not x.owner.available  # disabled: not in the tray
    x.icon.set_enabled(True)
    assert docked(x, manager)
    assert manager.requests == [x.icon.icon]
    info = manager.icons[0].get_full_property(manager.atom("_XEMBED_INFO"), manager.atom("_XEMBED_INFO"))
    assert list(info.value) == [0, 1]  # version 0, mapped
    assert manager.icons[0].get_wm_class() == ("siphon", "Siphon")


def test_a_tray_that_starts_later_gets_the_icon(x):
    from xtray import TrayManager

    silent = TrayManager(x.display, announce=False)  # the selection nobody (but gamescope, perhaps) keeps
    silent.panel.set_selection_owner(silent.selection, 0)
    silent.d.sync()
    silent.stop()
    x.icon.set_enabled(True)
    linger()
    assert not x.owner.available
    manager = x.manager()  # a bar starting: MANAGER on the root window
    assert docked(x, manager)


def test_a_manager_that_never_embeds_leaves_it_unavailable(x):
    manager = x.manager()
    manager._dock = lambda window: None  # takes the request, shows nothing
    x.icon.set_enabled(True)
    linger([manager], 1.0)
    assert manager.requests == [x.icon.icon] and not x.owner.available


def test_the_tray_quitting_and_starting_again(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    first = x.icon.icon
    manager.stop()  # the save set hands the icon back to the root: it must not stay there as a window
    assert pump(lambda: not x.owner.available and not x.icon.icon)
    again = x.manager()
    assert docked(x, again)
    assert again.requests == [x.icon.icon] and x.icon.icon != first


def test_a_tray_letting_the_icon_go_gets_a_new_one(x):
    """A tray quitting by itself hands its icons back first: the old window goes, a new one asks to dock (and
    settles wherever the next tray is)."""
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    first = x.icon.icon
    manager.release()
    assert pump(lambda: len(manager.requests) == 2 and x.icon.docked, [manager])
    assert ("available", False) in x.owner.log[1:] and x.icon.icon not in (0, first) and x.owner.available
    from Xlib import error

    with pytest.raises((error.BadWindow, error.BadDrawable)):  # destroyed, not left on the root where a window manager would show it
        manager.d.create_resource_object("window", first).get_geometry()


def test_disabled_it_leaves_the_tray_and_comes_back(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    first = x.icon.icon
    x.icon.set_enabled(False)
    assert pump(lambda: manager.gone == [first], [manager])
    assert not x.owner.available and not x.icon.icon
    x.icon.set_enabled(True)
    assert docked(x, manager)


# ---------------------------------------------------------------- drawing


def test_it_draws_the_siphon_icon_over_the_bar(x):
    manager = x.manager(background=0x336699)
    x.icon.set_enabled(True)
    assert docked(x, manager)
    for side in (24, 32):
        if side != 24:
            manager.resize(side, side)
        assert pump(lambda: x.icon.size == (side, side) and x.icon._pixels[0] == side, [manager])
        linger([manager], 0.3)
        width, height, depth, data = manager.image()
        assert (width, height, depth) == (side, side, 24)
        shown = shown_pixels(width, height, data)
        assert worst(expected_pixels(side, 0x336699), shown, alpha=False) <= 3
        assert sum(pixel[:3] != (0x33, 0x66, 0x99) for pixel in shown) > side * side // 3  # not just the bar


def test_it_draws_in_the_trays_argb_visual(x):
    manager = x.manager(argb=True)
    x.icon.set_enabled(True)
    assert docked(x, manager)
    linger([manager], 0.3)
    width, height, depth, data = manager.image()
    assert (width, height, depth) == (24, 24, 32)
    shown = shown_pixels(width, height, data)
    assert worst(expected_pixels(24, None), shown, alpha=True) <= 2
    assert shown[0][3] == 0  # see-through around the round icon


def test_icon_pixels_are_premultiplied_in_machine_order(tmp_path):
    pixels = xembed.icon_pixels(16)
    assert pixels is not None and len(pixels) == 16 * 16 * 4
    blue, green, red, alpha = (pixels[i::4] for i in range(4)) if sys.byteorder == "little" else \
        (pixels[3::4], pixels[2::4], pixels[1::4], pixels[0::4])
    assert all(max(r, g, b) <= a for r, g, b, a in zip(red, green, blue, alpha))
    assert xembed.icon_pixels(16, tmp_path / "missing.svg") is None


# ---------------------------------------------------------------- clicks


def test_clicks_on_the_icon(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    manager.click(1)
    manager.click(2)
    manager.click(4)
    manager.click(5)
    assert pump(lambda: len(x.owner.commands()) == 4, [manager]), x.owner.log
    # the wheel at once; clicks from the main loop, once the X event is done with
    assert sorted(x.owner.commands()) == [("run", menus.PLAY_PAUSE), ("run", menus.TOGGLE), ("scroll", -1.0),
                                          ("scroll", 1.0)]
    # the left click's X time goes with it, for GTK to present the window with (_TIME, as startup ids have it)
    assert len(x.owner.tokens()) == 1 and x.owner.tokens()[0].startswith("_TIME")
    assert int(x.owner.tokens()[0][5:]) > 0
    assert x.owner.log.index(("token", x.owner.tokens()[0])) < x.owner.log.index(("run", menus.TOGGLE))
    left, top, width, height = manager.icon_rect()
    manager.move_pointer(left + 5, top + 5)  # pressed on the icon, let go elsewhere: nothing
    from Xlib import X
    from Xlib.ext import xtest

    xtest.fake_input(manager.d, X.ButtonPress, 1)
    manager.move_pointer(left - 40, top - 40)
    xtest.fake_input(manager.d, X.ButtonRelease, 1)
    manager.d.sync()
    linger([manager])
    assert len(x.owner.commands()) == 4


def open_menu(x, manager) -> None:
    manager.click(3)
    assert pump(lambda: x.icon.menu.is_open and manager.windows_named("POPUP_MENU"), [manager])
    linger([manager], 0.2)


def row_point(x, command: str) -> tuple[int, int]:
    menu = x.icon.menu
    left, top = menu.position
    width, _height = menu.size
    row_top, row_bottom = next((a, b) for a, b, item in menu._rows if item.command == command)
    return left + width // 2, top + (row_top + row_bottom) // 2


def test_the_menu_has_the_dbusmenus_items_and_runs_them(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    open_menu(x, manager)
    menu = x.icon.menu
    assert [(item.label, item.enabled) for item in menu.items] == [
        (item.label, item.enabled) for item in menus.menu(PLAYING)]
    assert [item.label for item in menu.items if item.label] == [
        "Show Siphon", "A Song – Someone", "Pause", "Next", "Previous", "Quit Siphon"]
    left, top, width, height = manager.windows_named("POPUP_MENU")[0]
    icon_left, icon_top, _w, _h = manager.icon_rect()
    assert top + height <= icon_top + 12  # the tray is at the bottom: the menu opens upwards, on screen
    screen_width, _screen_height = x.icon._x.screen_size
    assert 0 <= left and left + width <= screen_width
    manager.move_pointer(*row_point(x, menus.NEXT))
    assert pump(lambda: menu.hover >= 0 and menu._rows[menu.hover][2].command == menus.NEXT, [manager])
    manager.click_at(*row_point(x, menus.NEXT))
    assert pump(lambda: ("run", menus.NEXT) in x.owner.log, [manager])
    assert not menu.is_open and manager.windows_named("POPUP_MENU") == []
    open_menu(x, manager)
    manager.click_at(*row_point(x, menus.QUIT))
    assert pump(lambda: ("run", menus.QUIT) in x.owner.log, [manager])


def test_the_songs_label_and_disabled_items_do_nothing(x):
    x.owner.state = menus.State(song="A Song – Someone", can_play=True)  # the last song: no Next
    x.icon.update(x.owner.state)
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    open_menu(x, manager)
    menu = x.icon.menu
    left, top = menu.position
    width, _height = menu.size
    for label in ("A Song – Someone", "Next"):
        row_top, row_bottom = next((a, b) for a, b, item in menu._rows if item.label == label)
        manager.click_at(left + width // 2, top + (row_top + row_bottom) // 2)
    linger([manager])
    assert x.owner.commands() == [] and menu.is_open


def test_a_click_elsewhere_or_on_the_icon_closes_the_menu(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    open_menu(x, manager)
    manager.click_at(20, 20)  # anywhere else on the screen: the menu's grab sees it
    assert pump(lambda: not x.icon.menu.is_open, [manager])
    open_menu(x, manager)
    manager.click(1)  # the icon again: closes the menu and does nothing else
    assert pump(lambda: not x.icon.menu.is_open, [manager])
    linger([manager])
    assert x.owner.commands() == []


def test_the_menu_by_keyboard(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    open_menu(x, manager)
    manager.key(KEYSYMS["down"])
    manager.key(KEYSYMS["down"])  # past the song's label, which cannot be chosen
    manager.key(KEYSYMS["up"])
    manager.key(KEYSYMS["up"])  # round to the end
    assert pump(lambda: x.icon.menu.hover >= 0 and x.icon.menu._rows[x.icon.menu.hover][2].command == menus.QUIT,
                [manager])
    manager.key(KEYSYMS["escape"])
    assert pump(lambda: not x.icon.menu.is_open, [manager])
    open_menu(x, manager)
    manager.key(KEYSYMS["down"])
    manager.key(KEYSYMS["enter"])
    assert pump(lambda: ("run", menus.SHOW) in x.owner.log, [manager])
    assert [token[:5] for token in x.owner.tokens()] == ["_TIME"]  # Show Siphon: with the key's time


def test_the_open_menu_follows_the_player(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    open_menu(x, manager)
    x.icon.update(menus.State(song="A Song – Someone", playing=False, can_play=True))
    assert [item.label for item in x.icon.menu.items if item.command == menus.PLAY_PAUSE] == ["Play"]
    x.icon.update(menus.State())  # the song gone: the menu is rebuilt, still open
    assert pump(lambda: x.icon.menu.is_open, [manager])
    assert "A Song – Someone" not in [item.label for item in x.icon.menu.items]


def test_the_tooltip_names_the_song(x):
    manager = x.manager()
    x.icon.set_enabled(True)
    assert docked(x, manager)
    left, top, width, height = manager.icon_rect()
    manager.move_pointer(left - 30, top - 30)
    manager.move_pointer(left + width // 2, top + height // 2)
    assert pump(lambda: manager.windows_named("TOOLTIP"), [manager])
    assert x.icon.tooltip.text == "Siphon: A Song – Someone"
    manager.move_pointer(left - 30, top - 30)
    assert pump(lambda: not manager.windows_named("TOOLTIP"), [manager])


# ---------------------------------------------------------------- where it runs


def test_it_runs_on_x11_or_when_asked_to(monkeypatch):
    monkeypatch.delenv("SIPHON_XEMBED", raising=False)
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setattr(xembed, "_gtk_display", lambda: None)
    assert xembed.display_name() is None  # Wayland (or no GTK): not by default
    monkeypatch.setenv("SIPHON_XEMBED", "1")
    assert xembed.display_name() == ":7"
    fake = type("GdkX11Display", (), {"__gtype__": type("T", (), {"name": "GdkX11Display"})(),
                                      "get_name": lambda self: ":3"})
    monkeypatch.setattr(xembed, "_gtk_display", lambda: fake())
    monkeypatch.delenv("SIPHON_XEMBED")
    assert xembed.display_name() == ":3"  # GTK on X11: its own display
    monkeypatch.setenv("SIPHON_XEMBED", "0")
    assert xembed.display_name() is None
    monkeypatch.delenv("SIPHON_XEMBED")
    monkeypatch.setattr(sys, "platform", "win32")
    assert xembed.display_name() is None


def test_losing_the_x_server_leaves_siphon_running(x_display):
    """Its own X server, killed under it: the icon becomes unavailable and nothing else happens."""
    from xtray import TrayManager, x_server

    with x_server() as name:
        if name is None:
            pytest.skip("no second X server")
        owner = Owner(PLAYING)
        icon = xembed.XEmbedIcon(owner, xembed._Connection(xembed._load(), name))
        manager = TrayManager(name)
        icon.set_enabled(True)
        assert pump(lambda: owner.available, [manager])
    assert pump(lambda: icon._x.dead and not owner.available, timeout=5)
    icon.update(menus.State())
    icon.set_enabled(False)
    icon.close()
