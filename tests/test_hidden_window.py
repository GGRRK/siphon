"""A hidden window (closed to the tray, say) rests: the now-playing bar skips the position's updates and
catches up once shown, and the cover cache is let go. Closed to the tray and brought back by it, the whole Siphon
does so too, and quits cleanly from the tray or with Ctrl+Q.

GTK needs a display, so each check runs in a child process: on Linux against a private gtk4-broadwayd, with no
session bus or, for the tray, a private one (nothing reaches the desktop); on Windows in the session's own desktop."""

import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_BAR = """
import json, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from pathlib import Path
from siphon.ui.art import CoverArt
from siphon.ui.nowplaying import NowPlaying
from siphon.library import Song
from gi.repository import GLib, Gtk
from fake_player import Player

def run_until(condition):
    end = time.monotonic() + 10
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

player = Player()
bar = NowPlaying(player, CoverArt(lambda path: None))
window = Gtk.Window(child=bar)
player.queue = [Song(Path("a.mp3"), "A Song", "Someone", "", 300.0, None, 0.0, 1)]
player.current, player.index, player.duration, player.state = player.queue[0], 0, 300.0, "playing"
player.emit("changed")
window.present()
seen = {"shown": run_until(bar.get_mapped)}
player.position = 10.0
player.emit("position", 10.0)
seen["playing"] = (bar._elapsed.get_label(), bar._seek.get_value())
labels = []
bar._elapsed.connect("notify::label", lambda label, _pspec: labels.append(label.get_label()))
window.set_visible(False)
seen["hidden"] = run_until(lambda: not bar.get_mapped())
for second in (11.0, 12.0, 42.0):  # a seek from the media keys, too
    player.position = second
    player.emit("position", second)
seen["while hidden"] = (bar._elapsed.get_label(), bar._seek.get_value(), labels[:])
window.present()
seen["shown again"] = run_until(bar.get_mapped)
seen["caught up"] = (bar._elapsed.get_label(), bar._seek.get_value())
print(json.dumps(seen))
"""


@pytest.fixture
def display(tmp_path_factory):
    """Environment for a GTK child process that can open no window on the desktop and reach no session bus."""
    if sys.platform == "win32":
        yield dict(os.environ)
        return
    broadwayd = shutil.which("gtk4-broadwayd")
    if broadwayd is None:
        pytest.skip("needs gtk4-broadwayd")
    runtime = tmp_path_factory.mktemp("rt")  # short: the display's socket path must fit in 108 bytes
    runtime.chmod(0o700)
    number = random.randint(40, 99)
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
    env |= {"XDG_RUNTIME_DIR": str(runtime), "GDK_BACKEND": "broadway", "BROADWAY_DISPLAY": f":{number}",
            "GTK_A11Y": "none"}
    server = subprocess.Popen([broadwayd, f":{number}", "--address", "127.0.0.1", "--port", str(8080 + number)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not list(runtime.glob("broadway*.socket")) and time.monotonic() < deadline:
            time.sleep(0.05)
        yield env
    finally:
        server.terminate()
        server.wait(10)


def test_a_hidden_bar_skips_position_updates_and_catches_up_when_shown(display):
    done = subprocess.run([sys.executable, "-c", _BAR, str(ROOT)], env=display, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    assert seen["shown"] and seen["hidden"] and seen["shown again"]
    assert seen["playing"] == ["0:10", 10.0]
    assert seen["while hidden"] == ["0:10", 10.0, []]  # no label or bar change at all
    assert seen["caught up"] == ["0:42", 42.0]


_COVERS = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from siphon.ui.art import Cover, CoverArt
from gi.repository import GdkPixbuf, GLib, Gtk

def run_until(condition):
    end = time.monotonic() + 10
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

files = []
for n in range(3):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
    pixbuf.fill(0x204080ff + n)
    files.append(Path(sys.argv[2]) / f"{n}.png")
    pixbuf.savev(str(files[-1]), "png", [], [])
art = CoverArt(lambda path: path)
cover = Cover(art, 40)
window = Gtk.Window(child=cover)
art.rest_with(window)
window.present()
seen = {"shown": run_until(cover.get_mapped)}
cover.show(None, files[0])
for file in files[1:]:
    art.load(file, 40, lambda texture: None, mtime=1)
seen["cached"] = run_until(lambda: len(art._cache) == 3) and cover._image.get_paintable() is not None
window.set_visible(False)
seen["hidden"] = run_until(lambda: not cover.get_mapped())
seen["cached while hidden"] = len(art._cache)
window.present()
seen["shown again"] = run_until(cover.get_mapped)
seen["still showing"] = cover._image.get_paintable() is not None and not cover.has_css_class("placeholder")
print(json.dumps(seen))
"""


def test_a_hidden_window_lets_the_cover_cache_go_but_keeps_the_covers_on_screen(display, tmp_path):
    done = subprocess.run([sys.executable, "-c", _COVERS, str(ROOT), str(tmp_path)], env=display, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    assert seen == {"shown": True, "cached": True, "hidden": True, "cached while hidden": 0, "shown again": True,
                    "still showing": True}



# The whole Siphon (the real engine, library and libmpv player, silent) closed to the tray and brought back by it.
# The tray's other side is a bar's (test_tray's fake StatusNotifierWatcher and dbusmenu client, on the private bus),
# an X session's legacy tray (tests/xtray.py on an invisible X server: GTK beside it on Broadway, or on that X server
# itself), or Windows's notification area (test_wintray's fake user32 and shell32, running the real window procedure).
_TRAY = """
import json, os, sys, threading, time
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from pathlib import Path
from siphon import app as siphon_app, tray, wintray
from siphon.library import Song
from gi.repository import GdkPixbuf, GLib

song_file, picture, platform, quit_by = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
seen = {}

def run_until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

def linger(seconds):
    run_until(lambda: False, seconds)

class Bar:
    \"\"\"A Linux bar with a system tray.\"\"\"
    def __init__(self, app):
        from test_tray import Client, Watcher
        address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        self.watcher = Watcher(address)
        self.client = Client(address, app.tray._backend.name)
    def click(self):
        self.client.item("Activate", "ii", 0, 0)
    def choose(self, command):
        self.client.click(tray.IDS[command])

class XTray:
    \"\"\"An X session's legacy tray, with nothing on the bus to show StatusNotifierItems.\"\"\"
    def __init__(self, app):
        from xtray import TrayManager
        self.app = app
        self.manager = TrayManager(os.environ["DISPLAY"])
        GLib.timeout_add(10, lambda: self.manager.pump() or True)
    def click(self):
        self.manager.click(1)
    def choose(self, command):
        menu = self.app.tray._backend.xembed.menu
        self.manager.click(3)
        run_until(lambda: menu.is_open)
        linger(0.1)
        left, top = menu.position
        top_row, bottom_row = next((a, b) for a, b, item in menu._rows if item.command == command)
        self.manager.click_at(left + menu.size[0] // 2, top + (top_row + bottom_row) // 2)

class NotificationArea:
    \"\"\"Windows's.\"\"\"
    def __init__(self, app):
        self.api = fake_windows
    def click(self):
        self.api.PostMessageW(HWND_TRAY, wintray.WM_APP + 1, 0, (1 << 16) | wintray.NIN_SELECT)
    def choose(self, command):
        self.api.chosen = tray.IDS[command]
        self.api.PostMessageW(HWND_TRAY, wintray.WM_APP + 1, (300 << 16) | 20, (1 << 16) | wintray.WM_CONTEXTMENU)

if platform == "windows":
    from test_wintray import FakeApi, HWND_TRAY
    fake_windows = FakeApi()
    tray.create = lambda player, bus: tray.Tray(player, lambda owner: wintray.NotifyIcon(owner, api=fake_windows))

class App(siphon_app.SiphonApp):
    def do_activate(self):
        first = self._window is None
        siphon_app.SiphonApp.do_activate(self)
        if first:
            GLib.timeout_add(100, self.drive)

    def drive(self):
        try:
            self.steps()
        except Exception:
            import traceback
            traceback.print_exc()
            self.quit()
        return GLib.SOURCE_REMOVE

    def steps(self):
        win, player = self._window, self.player
        bar = win._now_playing
        art = bar._cover._art
        self.other_side = {"windows": NotificationArea, "linux": Bar}.get(platform, XTray)(self)
        told = self.tray.quitting

        def quitting():  # first thing as Siphon quits: a Siphon started now must not hand this one its links
            seen["tray told first"] = self.player._mpv is not None
            told()
        self.tray.quitting = quitting
        seen["tray"] = run_until(lambda: self.tray.available) and self.hides_on_close()
        moves = []
        bar._seek.connect("value-changed", lambda scale: moves.append(scale.get_value()))
        player.play_songs([Song(song_file, "A Song", "Someone", "", 0.0, None, 0.0, 1)])
        seen["playing"] = run_until(lambda: player.state == "playing" and len(moves) >= 3)
        GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64).savev(str(picture), "png", [], [])

        def rests_and_comes_back(show, name):
            decoded = []
            art.load(picture, 40, decoded.append, mtime=len(seen))  # a cover decoded while shown
            cached = run_until(lambda: decoded) and len(art._cache) >= 1
            win.close()  # the window's close button
            hidden = run_until(lambda: not win.get_visible() and not bar.get_mapped())
            before, at = len(moves), player.position
            linger(1.5)
            rested = len(moves) == before and player.position - at > 1.0 and self._window is win
            show()
            shown = run_until(lambda: win.get_visible() and bar.get_mapped())
            caught_up = abs(bar._seek.get_value() - player.position) < 0.3
            before = len(moves)
            linger(1.0)
            seen[name] = {"cached": cached, "hidden": hidden, "cache while hidden": len(art._cache),
                          "rested": rested, "shown": shown, "caught up": caught_up, "live": len(moves) - before >= 2}

        rests_and_comes_back(self.other_side.click, "icon clicked")
        rests_and_comes_back(lambda: self.other_side.choose(tray.SHOW), "Show Siphon")
        if platform in ("xembed", "x11"):  # the X tray's left click hides a window in front, too
            win.present()
            active = run_until(win.is_active, 3)
            self.other_side.click()
            hidden = run_until(lambda: not win.get_visible(), 3)
            self.other_side.click()
            seen["toggle"] = {"active": active, "hidden": hidden, "shown": run_until(win.get_visible, 3)}
        seen["no yt-dlp"] = "yt_dlp" not in sys.modules  # nothing so far needed a link resolved
        seen["ctrl+q"] = self.get_accels_for_action("app.quit")
        if quit_by == "tray":
            win.close()
            run_until(lambda: not win.get_visible())
            self.other_side.choose(tray.QUIT)  # with the window hidden in the tray
        else:
            self.activate_action("quit", None)  # what Ctrl+Q does, with the window shown

app = App(siphon_app.load_core())
started = time.monotonic()
seen["exit"] = app.run([])
seen["quit in time"] = time.monotonic() - started < 60
seen["mpv stopped"] = app.player._mpv is None and app.player.state == "stopped"
seen["threads left"] = sorted(t.name for t in threading.enumerate()
                              if t is not threading.main_thread() and not t.daemon)
seen["windows left"] = len(app.get_windows())
seen["tray icon left"] = app.tray.available or (platform == "windows" and fake_windows.icons != [])
print(json.dumps(seen))
"""


@pytest.mark.linux
@pytest.mark.parametrize("platform, quit_by", [("linux", "tray"), ("linux", "ctrl+q"), ("windows", "tray"),
                                               ("xembed", "tray"), ("x11", "ctrl+q")])
def test_a_window_closed_to_the_tray_rests_and_comes_back_live(display, bus, tmp_path, platform, quit_by, request):
    """Linux's tray on the private bus, an X tray (GTK on Broadway beside it, or on its X server), or Windows's
    simulated; quitting from the tray or with Ctrl+Q."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to make a song")
    music = tmp_path / "music"
    music.mkdir()
    song = music / "a.opus"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-c:a", "libopus", str(song)], check=True)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "siphon"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(json.dumps({"folder": str(music), "auto_update": False,
                                                      "auto_engine": False, "told_about_tray": True}))
    env = display | {"DBUS_SESSION_BUS_ADDRESS": bus, "SIPHON_AO": "null"}
    for name in ("SIPHON_FAKE_CORE", "SIPHON_FAKE_PLAYER", "SIPHON_NO_MPRIS", "SIPHON_NO_TRAY"):
        env.pop(name, None)
    if platform == "windows":
        env["SIPHON_NO_MPRIS"] = "1"  # Windows has none
    if platform in ("xembed", "x11"):
        pytest.importorskip("Xlib")
        env["DISPLAY"] = request.getfixturevalue("x_display")
        if platform == "xembed":
            env["SIPHON_XEMBED"] = "1"  # GTK on Broadway (Wayland alike): the X tray only when asked for
        else:
            env["GDK_BACKEND"] = "x11"  # GTK on the X server: its tray found by itself
            env.pop("BROADWAY_DISPLAY")
    done = subprocess.run([sys.executable, "-c", _TRAY, str(ROOT), str(song), str(tmp_path / "cover.png"), platform,
                           quit_by], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    back = {"cached": True, "hidden": True, "cache while hidden": 0, "rested": True, "shown": True,
            "caught up": True, "live": True}
    expected = {"tray": True, "tray told first": True, "playing": True, "icon clicked": back, "Show Siphon": back,
                "no yt-dlp": True, "ctrl+q": ["<Control>q"], "exit": 0, "quit in time": True, "mpv stopped": True,
                "threads left": [], "windows left": 0, "tray icon left": False}
    if platform == "x11":  # in front, the window hides; clicked again, it shows
        expected["toggle"] = {"active": True, "hidden": True, "shown": True}
    elif platform == "xembed":  # Broadway gives no window the focus: not in front, so the click only shows it
        expected["toggle"] = {"active": False, "hidden": False, "shown": True}
    assert seen == expected, done.stderr
