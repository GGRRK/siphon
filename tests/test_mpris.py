"""MPRIS, end to end: Siphon's player in a child process on a private D-Bus, driven by playerctl and gdbus.

The bus is our own dbus-daemon with no service directories, so nothing can be auto-started on it, and nothing
here touches the desktop's session bus. Run as a script, this file is that child process (see _serve).
"""

import os
import select
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

TOOLS = ("dbus-daemon", "playerctl", "gdbus", "ffmpeg")
pytestmark = [
    pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason=f"needs {', '.join(TOOLS)}"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning:gi.events"),  # PyGObject's MainContext.iteration()
]

_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:abstract={name}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
_PLAYER = "org.mpris.MediaPlayer2.siphon"
_TITLES = ("First Tone", "Second Tone")


def _read_line(stream, timeout: float) -> str:
    ready, _, _ = select.select([stream], [], [], timeout)
    assert ready, "the child process did not answer in time"
    return stream.readline().strip()


@pytest.fixture(scope="module")
def bus(tmp_path_factory):
    """The address of a private session bus (an abstract socket: no file to leave behind)."""
    config = tmp_path_factory.mktemp("bus") / "session.conf"
    config.write_text(_BUS_CONFIG.format(name=f"siphon-test-{uuid.uuid4().hex}"))
    daemon = subprocess.Popen(["dbus-daemon", "--nofork", f"--config-file={config}", "--print-address=1"],
                              stdout=subprocess.PIPE, text=True)
    try:
        yield _read_line(daemon.stdout, 5)
    finally:
        daemon.terminate()
        daemon.wait(5)


@pytest.fixture(scope="module")
def audio(tmp_path_factory) -> Path:
    """Two 30 s tones; the first carries an embedded PNG cover."""
    folder = tmp_path_factory.mktemp("audio")
    tone = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi"]
    subprocess.run([*tone, "-i", "color=c=red:s=32x32", "-frames:v", "1", str(folder / "cover.png")], check=True)
    subprocess.run([*tone, "-i", "sine=frequency=440:duration=30", "-i", str(folder / "cover.png"),
                    "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "png", "-id3v2_version", "3",
                    "-disposition:v", "attached_pic", str(folder / "1.mp3")], check=True)
    subprocess.run([*tone, "-i", "sine=frequency=550:duration=30", "-c:a", "libmp3lame", str(folder / "2.mp3")],
                   check=True)
    return folder


@pytest.fixture
def env(bus, tmp_path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(DBUS_SESSION_BUS_ADDRESS=bus, SIPHON_AO="null", NO_AT_BRIDGE="1", GTK_A11Y="none",
               XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "data"))
    return env


@pytest.fixture
def siphon(env, audio):
    """A running child that plays the two tones and publishes them over MPRIS."""
    child = subprocess.Popen([sys.executable, __file__, str(audio)], env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert _read_line(child.stdout, 10) == "ready"
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(5)


def playerctl(env, *args: str) -> str:
    done = subprocess.run(["playerctl", "--player=siphon", *args], env=env, capture_output=True, text=True,
                          timeout=5)
    return done.stdout.strip()


def gdbus(env, method: str, *args: str) -> str:
    done = subprocess.run(["gdbus", "call", "--session", "-d", _PLAYER, "-o", "/org/mpris/MediaPlayer2", "-m", method,
                           *args], env=env, capture_output=True, text=True, timeout=5, check=True)
    return done.stdout.strip()


def settle(check, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "the player never got there"
        time.sleep(0.05)


def test_it_is_listed_with_its_identity(siphon, env):
    assert "siphon" in playerctl(env, "--list-all").split()
    props = gdbus(env, "org.freedesktop.DBus.Properties.GetAll", "org.mpris.MediaPlayer2")
    for expected in ("'Identity': <'Siphon'>", "'DesktopEntry': <'io.github.ggrrk.Siphon'>",
                     "'CanRaise': <true>", "'CanQuit': <true>", "'HasTrackList': <false>"):
        assert expected in props


def test_metadata_describes_the_song_with_its_cover(siphon, env, audio, tmp_path):
    fields = playerctl(env, "metadata", "--format",
                       "{{title}}|{{artist}}|{{album}}|{{mpris:length}}|{{xesam:trackNumber}}|{{xesam:url}}|"
                       "{{mpris:artUrl}}").split("|")
    assert fields[:6] == ["First Tone", "Tester", "Tones", "30000000", "1", (audio / "1.mp3").as_uri()]
    cover = Path(fields[6].removeprefix("file://"))
    assert fields[6].startswith("file://") and cover.is_relative_to(tmp_path / "cache" / "siphon" / "covers")
    assert cover.read_bytes().startswith(b"\x89PNG")


def test_play_pause_and_stop(siphon, env):
    assert playerctl(env, "status") == "Playing"
    playerctl(env, "play-pause")
    settle(lambda: playerctl(env, "status") == "Paused")
    playerctl(env, "play")
    settle(lambda: playerctl(env, "status") == "Playing")
    playerctl(env, "pause")
    settle(lambda: playerctl(env, "status") == "Paused")
    playerctl(env, "stop")
    settle(lambda: playerctl(env, "status") == "Stopped")
    playerctl(env, "play")
    settle(lambda: playerctl(env, "status") == "Playing")


def test_next_and_previous(siphon, env):
    playerctl(env, "next")
    settle(lambda: playerctl(env, "metadata", "title") == "Second Tone")
    playerctl(env, "previous")
    settle(lambda: playerctl(env, "metadata", "title") == "First Tone")


def test_position_set_and_relative_seek(siphon, env):
    playerctl(env, "position", "12")
    settle(lambda: 12 <= float(playerctl(env, "position")) < 14)
    playerctl(env, "position", "5-")
    settle(lambda: 7 <= float(playerctl(env, "position")) < 9)
    playerctl(env, "position", "100+")  # past the end acts like Next
    settle(lambda: playerctl(env, "metadata", "title") == "Second Tone")


def test_volume_loop_and_shuffle_round_trip(siphon, env):
    playerctl(env, "volume", "0.35")
    settle(lambda: playerctl(env, "volume") == "0.350000")
    for mode in ("Track", "Playlist", "None"):
        playerctl(env, "loop", mode)
        settle(lambda: playerctl(env, "loop") == mode)
    playerctl(env, "shuffle", "On")
    settle(lambda: playerctl(env, "shuffle") == "On")
    playerctl(env, "shuffle", "Off")
    settle(lambda: playerctl(env, "shuffle") == "Off")


def test_changes_are_announced(siphon, env, bus):
    from gi.repository import Gio, GLib

    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    connection = Gio.DBusConnection.new_for_address_sync(bus, flags, None, None)  # this bus only, never the desktop's
    heard: list[tuple[str, object]] = []

    def on_signal(_conn, _sender, _path, _iface, name, args):
        if name == "Seeked":
            heard.append(("Seeked", args.unpack()[0]))
        else:
            heard.extend(args.unpack()[1].items())

    for iface, name in (("org.freedesktop.DBus.Properties", "PropertiesChanged"),
                        ("org.mpris.MediaPlayer2.Player", "Seeked")):
        connection.signal_subscribe(None, iface, name, "/org/mpris/MediaPlayer2", None, Gio.DBusSignalFlags.NONE,
                                    on_signal)
    context = GLib.MainContext.default()

    def hear(expected) -> None:
        deadline = time.monotonic() + 3
        while expected not in heard:
            assert time.monotonic() < deadline, f"never heard {expected}, only {heard}"
            context.iteration(False)
            time.sleep(0.01)

    connection.flush_sync(None)  # the subscriptions reach the bus before anything is triggered
    playerctl(env, "pause")
    hear(("PlaybackStatus", "Paused"))
    playerctl(env, "loop", "Track")
    hear(("LoopStatus", "Track"))
    playerctl(env, "volume", "0.5")
    hear(("Volume", 0.5))
    playerctl(env, "position", "3")
    hear(("Seeked", 3_000_000))
    playerctl(env, "next")
    deadline = time.monotonic() + 3
    while not any(name == "Metadata" and meta.get("xesam:title") == "Second Tone" for name, meta in heard):
        assert time.monotonic() < deadline, "never heard the new song's metadata"
        context.iteration(False)
        time.sleep(0.01)
    connection.close_sync(None)


def test_raise_open_uri_and_quit(siphon, env):
    gdbus(env, "org.mpris.MediaPlayer2.Raise")
    assert _read_line(siphon.stdout, 3) == "raised"
    gdbus(env, "org.mpris.MediaPlayer2.Player.OpenUri", "file:///nowhere.mp3")
    assert playerctl(env, "metadata", "title") == "First Tone"
    gdbus(env, "org.mpris.MediaPlayer2.Quit")
    assert siphon.wait(3) == 0
    assert siphon.stdout.read().split() == ["bye"]


def test_without_a_session_bus_mpris_is_simply_off(env):
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:abstract=siphon-test-nobody-{uuid.uuid4().hex}"
    done = subprocess.run([sys.executable, __file__, "--no-bus"], env=env, capture_output=True, text=True, timeout=10)
    assert (done.returncode, done.stdout.strip()) == (0, "off")
    assert "media keys are off" in done.stderr


def _no_bus() -> None:
    """The child for a dead bus address: Mpris logs one warning and the player carries on."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gi.repository import Gio

    from siphon.mpris import Mpris
    from siphon.player import Player

    player = Player()
    mpris = Mpris(player, Gio.Application(application_id="io.github.ggrrk.SiphonMprisTest"))
    player.set_volume(0.5)
    mpris.close()
    player.shutdown()
    print("off", flush=True)


def _serve(folder: Path) -> None:
    """The child: a player on the (private) session bus, playing the tones, until MPRIS says Quit."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gi.repository import Gio

    from siphon.library import Song
    from siphon.mpris import Mpris
    from siphon.player import Player

    songs = [Song(folder / f"{n}.mp3", title, "Tester", "Tones" if n == 1 else "", 30.0, n, 0.0, 0)
             for n, title in enumerate(_TITLES, 1)]
    app = Gio.Application(application_id="io.github.ggrrk.SiphonMprisTest", flags=Gio.ApplicationFlags.NON_UNIQUE)
    running: list = []

    def activate(app: Gio.Application) -> None:
        if running:
            print("raised", flush=True)
            return
        app.hold()
        player = Player()
        running.extend((player, Mpris(player, app)))
        player.play_songs(songs)
        print("ready", flush=True)

    app.connect("activate", activate)
    app.run([])
    player, mpris = running
    mpris.close()
    player.shutdown()
    print("bye", flush=True)


if __name__ == "__main__":
    _no_bus() if sys.argv[1] == "--no-bus" else _serve(Path(sys.argv[1]))
