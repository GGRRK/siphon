"""A hidden window (closed to the background, say) rests: the now-playing bar skips the position's updates and
catches up once shown, and the cover cache is let go.

GTK needs a display, so each check runs in a child process: on Linux against a private gtk4-broadwayd, with no
session bus (nothing reaches the desktop), on Windows in the session's own desktop."""

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
