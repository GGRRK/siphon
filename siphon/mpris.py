"""MPRIS 2 on the session bus: media keys (playerctl) and desktop bars see and drive the player."""

import logging

from gi.repository import Gio, GLib

from .covers import cover_file
from .library import Song
from .player import Player

log = logging.getLogger(__name__)

PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
_TRACK_PATH = "/io/github/ggrrk/Siphon/Track/"
_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

_STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"}
_LOOP = {"off": "None", "one": "Track", "all": "Playlist"}
_REPEAT = {loop: repeat for repeat, loop in _LOOP.items()}

_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_ROOT_PROPS = {
    "CanQuit": GLib.Variant("b", True),
    "CanRaise": GLib.Variant("b", True),
    "HasTrackList": GLib.Variant("b", False),
    "Identity": GLib.Variant("s", "Siphon"),
    "DesktopEntry": GLib.Variant("s", "io.github.ggrrk.Siphon"),
    "SupportedUriSchemes": GLib.Variant("as", []),
    "SupportedMimeTypes": GLib.Variant("as", []),
}


def _usec(seconds: float) -> int:
    return round(seconds * 1_000_000)


class Mpris:
    """Owns org.mpris.MediaPlayer2.<suffix> for as long as it lives; without a session bus it does nothing."""

    def __init__(self, player: Player, app: Gio.Application, bus_name_suffix: str = "siphon") -> None:
        self._player = player
        self._app = app
        self._bus: Gio.DBusConnection | None = None
        self._objects: list[int] = []
        self._owner = 0
        self._art: tuple[str, str] = ("", "")  # (song key, artUrl) of the last cover lookup
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            for iface in Gio.DBusNodeInfo.new_for_xml(_XML).interfaces:
                self._objects.append(self._bus.register_object(
                    PATH, iface, self._on_call, self._on_get_property, self._on_set_property))
        except GLib.Error as exc:
            log.warning("media keys are off: no MPRIS on the session bus (%s)", exc.message)
            self._unregister()
            return
        self._owner = Gio.bus_own_name_on_connection(
            self._bus, f"org.mpris.MediaPlayer2.{bus_name_suffix}", Gio.BusNameOwnerFlags.NONE, None, None)
        self._published = self._player_props()
        self._handlers = [player.connect("changed", self._on_changed), player.connect("seeked", self._on_seeked)]

    def close(self) -> None:
        if self._owner:
            for handler in self._handlers:
                self._player.disconnect(handler)
            Gio.bus_unown_name(self._owner)
            self._owner = 0
        self._unregister()

    def _unregister(self) -> None:
        for registration in self._objects:
            self._bus.unregister_object(registration)
        self._objects.clear()

    # -- properties -------------------------------------------------------------------------------------------

    def _trackid(self) -> str:
        return f"{_TRACK_PATH}{self._player.index}" if self._player.current else _NO_TRACK

    def _metadata(self) -> dict[str, GLib.Variant]:
        song = self._player.current
        if song is None:
            return {}
        meta = {
            "mpris:trackid": GLib.Variant("o", self._trackid()),
            "xesam:title": GLib.Variant("s", song.title),
            "xesam:url": GLib.Variant("s", song.path.as_uri()),
        }
        if song.artist:
            meta["xesam:artist"] = GLib.Variant("as", [song.artist])
        if song.album:
            meta["xesam:album"] = GLib.Variant("s", song.album)
        if song.track_no:
            meta["xesam:trackNumber"] = GLib.Variant("i", song.track_no)
        if length := self._length():
            meta["mpris:length"] = GLib.Variant("x", _usec(length))
        if art := self._art_url(song):
            meta["mpris:artUrl"] = GLib.Variant("s", art)
        return meta

    def _length(self) -> float:
        song = self._player.current
        return (self._player.duration or song.duration) if song else 0.0

    def _art_url(self, song: Song) -> str:
        if self._art[0] != song.key:  # "changed" fires often; extract or look up the cover once per song
            cover = cover_file(song.path)
            self._art = (song.key, cover.as_uri() if cover else "")
        return self._art[1]

    def _player_props(self) -> dict[str, GLib.Variant]:
        """Every Player property that PropertiesChanged reports (Position is polled, by the spec)."""
        player = self._player
        has_song = player.current is not None
        return {
            "PlaybackStatus": GLib.Variant("s", _STATUS[player.state]),
            "LoopStatus": GLib.Variant("s", _LOOP[player.repeat]),
            "Rate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", player.shuffle),
            "Metadata": GLib.Variant("a{sv}", self._metadata()),
            "Volume": GLib.Variant("d", player.volume),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", player.has_next()),
            "CanGoPrevious": GLib.Variant("b", has_song),
            "CanPlay": GLib.Variant("b", has_song),
            "CanPause": GLib.Variant("b", has_song),
            "CanSeek": GLib.Variant("b", self._length() > 0),
            "CanControl": GLib.Variant("b", True),
        }

    def _on_get_property(self, _bus, _sender, _path, iface: str, name: str) -> GLib.Variant | None:
        if iface == ROOT_IFACE:
            return _ROOT_PROPS.get(name)
        if name == "Position":
            return GLib.Variant("x", _usec(self._player.position))
        return self._player_props().get(name)

    def _on_set_property(self, _bus, _sender, _path, iface: str, name: str, value: GLib.Variant) -> bool:
        if iface != PLAYER_IFACE:
            return False
        if name == "LoopStatus" and value.get_string() in _REPEAT:
            self._player.set_repeat(_REPEAT[value.get_string()])
        elif name == "Shuffle":
            self._player.set_shuffle(value.get_boolean())
        elif name == "Volume":
            self._player.set_volume(value.get_double())
        return True  # Rate is fixed at 1.0 and an unknown LoopStatus is ignored, as the spec allows

    # -- methods ----------------------------------------------------------------------------------------------

    def _on_call(self, _bus, _sender, _path, iface: str, method: str, args: GLib.Variant,
                 invocation: Gio.DBusMethodInvocation) -> None:
        player = self._player
        match (iface, method):
            case (_, "Raise"):
                self._app.activate()
            case (_, "Quit"):
                # the window's quit path asks first while downloads are running
                if self._app.lookup_action("quit"):
                    self._app.activate_action("quit", None)
                else:
                    self._app.quit()
            case (_, "Next"):
                player.next()
            case (_, "Previous"):
                player.previous()
            case (_, "Pause"):
                player.pause()
            case (_, "PlayPause"):
                player.toggle()
            case (_, "Stop"):
                player.stop()
            case (_, "Play"):
                player.play()
            case (_, "Seek"):
                self._seek_by(args.unpack()[0] / 1_000_000)
            case (_, "SetPosition"):
                track, usec = args.unpack()
                if player.current and track == self._trackid() and 0 <= usec <= _usec(self._length()):
                    player.seek(usec / 1_000_000)
            case _:  # OpenUri: no URI schemes are supported, so opening one does nothing
                pass
        invocation.return_value(None)

    def _seek_by(self, offset: float) -> None:
        player = self._player
        if player.current is None:
            return
        target = player.position + offset
        if player.duration and target > player.duration:
            player.next()  # the spec: seeking past the end acts like Next
        else:
            player.seek(max(0.0, target))

    # -- change notifications ---------------------------------------------------------------------------------

    def _on_changed(self, _player: Player) -> None:
        props = self._player_props()
        changed = {name: value for name, value in props.items() if self._published.get(name) != value}
        self._published = props
        if changed:
            self._bus.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", (PLAYER_IFACE, changed, [])))

    def _on_seeked(self, _player: Player, seconds: float) -> None:
        self._bus.emit_signal(None, PATH, PLAYER_IFACE, "Seeked", GLib.Variant("(x)", (_usec(seconds),)))
