"""Linux with no tray at all (plain GNOME, say): closing the window while Siphon plays or downloads hides it and keeps
going, says so once in a notification with a Quit button, and quits by itself once nothing is left to do; the
desktop's media controls (MPRIS Raise) and starting Siphon again bring the window back. Idle, or with Close to Tray
off, closing quits as before.

The whole Siphon (the real engine or the offline one, the libmpv player, silent) runs in a child process against a
private gtk4-broadwayd and a private session bus with no tray on it; a fake notification server there records what
Siphon sends and presses its buttons."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_hidden_window import display  # noqa: F401 (the fixture)

ROOT = Path(__file__).resolve().parent.parent
GRACE_S, PAUSED_GRACE_S = 1, 3  # the app's, shortened

_BACKGROUND = """
import json, os, subprocess, sys, threading, time, traceback
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from pathlib import Path
from siphon import app as siphon_app, tray
from siphon.library import Song
from gi.repository import Gio, GLib

song_file, mode = Path(sys.argv[2]), sys.argv[3]
siphon_app._BACKGROUND_GRACE_S, siphon_app._PAUSED_GRACE_S = int(sys.argv[4]), int(sys.argv[5])
siphon_app._TRAY_GRACE_S = 1
seen = {}

def run_until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

def linger(seconds):
    run_until(lambda: False, seconds)

def connect():
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    return Gio.DBusConnection.new_for_address_sync(os.environ["DBUS_SESSION_BUS_ADDRESS"], flags, None, None)

def call(bus, name, path, iface, method, args=None):
    done = []
    bus.call(name, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000, None,
             lambda _bus, result: done.append(result))
    run_until(lambda: done)
    return bus.call_finish(done[0]).unpack()

NOTIFICATIONS = '''
<node><interface name="org.freedesktop.Notifications">
  <method name="Notify">
    <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
  </method>
  <method name="CloseNotification"><arg type="u" direction="in"/></method>
  <method name="GetCapabilities"><arg type="as" direction="out"/></method>
  <method name="GetServerInformation">
    <arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/>
    <arg type="s" direction="out"/>
  </method>
  <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
  <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
</interface></node>'''

class Notifications:
    \"\"\"The desktop's notification server, recording each Notify.\"\"\"
    def __init__(self):
        self.bus = connect()
        self.calls, self.closed = [], []
        iface = Gio.DBusNodeInfo.new_for_xml(NOTIFICATIONS).interfaces[0]
        self.bus.register_object("/org/freedesktop/Notifications", iface, self._call, None, None)
        Gio.bus_own_name_on_connection(self.bus, "org.freedesktop.Notifications", Gio.BusNameOwnerFlags.NONE,
                                       None, None)
    def _call(self, _bus, _sender, _path, _iface, method, args, invocation):
        if method == "Notify":
            self.calls.append(args.unpack())
            invocation.return_value(GLib.Variant("(u)", (len(self.calls),)))
        elif method == "CloseNotification":
            self.closed.append(args.unpack()[0])
            invocation.return_value(None)
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["actions", "body"],)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant("(ssss)", ("fake", "tests", "1", "1.2")))
        else:
            invocation.return_value(None)
    def press(self, number, action):
        self.bus.emit_signal(None, "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                             "ActionInvoked", GLib.Variant("(us)", (number, action)))
    def seen(self):
        return [(summary, body, list(actions)) for _app, _id, _icon, summary, body, actions, *_ in self.calls]

MPRIS = ("org.mpris.MediaPlayer2.siphon", "/org/mpris/MediaPlayer2")

class App(siphon_app.SiphonApp):
    ended_at = None

    def do_activate(self):
        first = self._window is None
        siphon_app.SiphonApp.do_activate(self)
        if first:
            GLib.timeout_add(100, self.drive)

    def drive(self):
        try:
            self.steps()
        except Exception:
            traceback.print_exc()
            self.quit()
        return GLib.SOURCE_REMOVE

    def play(self):
        self.player.play_songs([Song(song_file, "A Song", "Someone", "", 0.0, None, 0.0, 1)])
        return run_until(lambda: self.player.state == "playing")

    def row(self):
        from siphon.ui.settings_dialog import TrayRow
        row = TrayRow(self)
        return row, lambda: [row.row.get_sensitive(), row.row.get_subtitle()]

    def steps(self):
        win = self._window
        self.notifications = Notifications()
        client = connect()
        linger(0.5)
        seen["no tray"] = self.tray is not None and not self.tray.available and self.runs_in_background()
        if mode == "idle":
            win.close()  # nothing playing or downloading: quits
            self.ended_at = time.monotonic()
            return
        if mode == "off":
            self.prefs.close_to_tray = False
            seen["playing"] = self.play()
            win.close()
            self.ended_at = time.monotonic()
            return
        if mode == "downloading":
            win.queue_links(["https://example.com/track/1"])
            seen["downloading"] = run_until(lambda: win.downloads.unfinished > 0)
        else:
            seen["playing"] = self.play()
        row, row_now = self.row()
        seen["row"] = row_now()
        win.close()
        seen["hidden"] = run_until(lambda: not win.get_visible()) and self._window is win
        seen["notified"] = run_until(lambda: self.notifications.calls) and self.notifications.seen()
        if mode == "downloading":
            seen["kept while downloading"] = run_until(lambda: win.downloads.unfinished == 0, 20) and \\
                self._window is win
            self.ended_at = time.monotonic()  # the download done: quits after the grace
            return
        if mode == "quit button":
            self.notifications.press(1, "app.quit")
            self.ended_at = time.monotonic()
            return
        if mode == "bar arrives":
            from test_tray import Watcher
            watcher = Watcher(os.environ["DBUS_SESSION_BUS_ADDRESS"])
            seen["tray came"] = run_until(lambda: self.tray.available)
            seen["row with the tray"] = row_now()
            self.player.stop()
            linger(GRACE + 1.5)
            seen["stays in the tray"] = self._window is win and not win.get_visible()
            watcher.stop()  # the bar goes again: the window, in its tray by now, comes back rather than quit
            seen["bar gone, back"] = run_until(win.get_visible, 5) and self._window is win
            self.activate_action("quit", None)
            return
        # main: the ways back, then idle quits it
        properties = call(client, *MPRIS, "org.freedesktop.DBus.Properties", "GetAll",
                          GLib.Variant("(s)", ("org.mpris.MediaPlayer2",)))[0]
        seen["mpris can"] = [properties["CanRaise"], properties["CanQuit"]]
        call(client, *MPRIS, "org.mpris.MediaPlayer2", "Raise")
        seen["raised"] = run_until(win.get_visible)
        seen["withdrawn once shown"] = run_until(lambda: self.notifications.closed) and self.notifications.closed
        win.close()
        run_until(lambda: not win.get_visible())
        linger(GRACE + 0.5)
        seen["playing, it stays"] = self._window is win and not win.get_visible() and self.player.state == "playing"
        second = subprocess.Popen([sys.executable, "-m", "siphon"], cwd=sys.argv[1], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
        seen["started again"] = run_until(lambda: win.get_visible() and second.poll() is not None, 30) and \\
            second.returncode
        win.close()
        run_until(lambda: not win.get_visible())
        seen["told once"] = len(self.notifications.calls)
        self.player.toggle()
        run_until(lambda: self.player.state == "paused")
        linger(GRACE + 0.8)
        seen["paused, it waits"] = self._window is win and not win.get_visible()
        self.player.stop()
        self.ended_at = time.monotonic()

GRACE = siphon_app._BACKGROUND_GRACE_S
app = App(siphon_app.load_core())
started = time.monotonic()
seen["exit"] = app.run([])
seen["ended after"] = round(time.monotonic() - app.ended_at, 1) if app.ended_at else None
seen["mpv stopped"] = app.player._mpv is None and app.player.state == "stopped"
seen["threads left"] = sorted(t.name for t in threading.enumerate()
                              if t is not threading.main_thread() and not t.daemon)
seen["windows left"] = len(app.get_windows())
print(json.dumps(seen))
"""

HEADING = "Siphon keeps playing in the background"
BODY = ("There is no system tray here. Start Siphon again or use the media controls to bring its window back; it "
        "quits by itself once the music stops and downloads finish.")
TOLD = [[HEADING, BODY, ["app.quit", "Quit"]]]
NO_TRAY_ROW = [True, "No system tray here: closing the window keeps Siphon running in the background while it "
                     "plays or downloads, and quits it otherwise"]


def run(display, bus, tmp_path, mode: str) -> dict:  # noqa: F811
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to make a song")
    music = tmp_path / "music"
    music.mkdir()
    song = music / "a.opus"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "60", "-c:a", "libopus", str(song)], check=True)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "siphon"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(json.dumps({"folder": str(music), "auto_update": False,
                                                      "auto_engine": False}))
    env = display | {"DBUS_SESSION_BUS_ADDRESS": bus, "SIPHON_AO": "null"}
    for name in ("SIPHON_FAKE_CORE", "SIPHON_FAKE_PLAYER", "SIPHON_NO_MPRIS", "SIPHON_NO_TRAY", "SIPHON_XEMBED"):
        env.pop(name, None)
    if mode == "downloading":
        env["SIPHON_FAKE_CORE"] = "1"  # offline "downloads" of a few seconds
    done = subprocess.run([sys.executable, "-c", _BACKGROUND, str(ROOT), str(song), mode, str(GRACE_S),
                           str(PAUSED_GRACE_S)], env=env, capture_output=True, text=True, timeout=150)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    assert seen.pop("mpv stopped") and seen.pop("threads left") == [] and seen.pop("windows left") == 0, seen
    assert seen.pop("exit") == 0 and seen.pop("no tray"), seen
    return seen


@pytest.mark.linux
def test_playing_it_keeps_going_hidden_comes_back_and_quits_once_idle(display, bus, tmp_path):  # noqa: F811
    seen = run(display, bus, tmp_path, "main")
    ended = seen.pop("ended after")
    assert seen == {"playing": True, "row": NO_TRAY_ROW, "hidden": True, "notified": TOLD, "mpris can": [True, True],
                    "raised": True, "withdrawn once shown": [1], "playing, it stays": True, "started again": 0, "told once": 1,
                    "paused, it waits": True}
    assert GRACE_S - 0.2 <= ended <= GRACE_S + 2.5  # stopped: quits after the grace, not before


@pytest.mark.linux
@pytest.mark.parametrize("mode", ["idle", "off"])
def test_closing_quits_when_idle_or_switched_off(display, bus, tmp_path, mode):  # noqa: F811
    seen = run(display, bus, tmp_path, mode)
    assert seen.pop("ended after") < GRACE_S  # at once, no grace
    assert seen == ({} if mode == "idle" else {"playing": True})


@pytest.mark.linux
def test_the_notifications_quit_button_quits(display, bus, tmp_path):  # noqa: F811
    seen = run(display, bus, tmp_path, "quit button")
    assert seen.pop("ended after") < GRACE_S
    assert seen == {"playing": True, "row": NO_TRAY_ROW, "hidden": True, "notified": TOLD}


@pytest.mark.linux
def test_downloading_it_keeps_going_until_the_download_is_done(display, bus, tmp_path):  # noqa: F811
    seen = run(display, bus, tmp_path, "downloading")
    ended = seen.pop("ended after")
    assert seen == {"downloading": True, "row": NO_TRAY_ROW, "hidden": True, "notified": TOLD,
                    "kept while downloading": True}
    assert GRACE_S - 0.2 <= ended <= GRACE_S + 2.5


@pytest.mark.linux
def test_a_bar_that_arrives_takes_the_hidden_window_into_its_tray(display, bus, tmp_path):  # noqa: F811
    seen = run(display, bus, tmp_path, "bar arrives")
    assert seen.pop("ended after") is None
    assert seen == {"playing": True, "row": NO_TRAY_ROW, "hidden": True, "notified": TOLD, "tray came": True,
                    "row with the tray": [True, "Closing the window keeps Siphon playing and downloading in the tray"],
                    "stays in the tray": True, "bar gone, back": True}
