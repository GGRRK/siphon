"""Siphon's icon in the system tray: its menu and tooltip, built from the player, on either platform.

Linux publishes a StatusNotifierItem and its dbusmenu on the session bus (siphon/sni.py), Windows puts an icon in
the notification area (siphon/wintray.py). Both draw the menu that menu() describes and hand what the user picks
back to the GTK main loop, where Tray emits it. The icon shows for as long as Siphon runs; with the window closed
to the tray it is the way back in.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from gi.repository import GObject

from . import paths

SHOW, PLAY_PAUSE, NEXT, PREVIOUS, QUIT = "show", "play-pause", "next", "previous", "quit"
# The menu's item ids: dbusmenu's, and the WM_COMMAND ids of the Windows menu (the smoke test posts QUIT's).
IDS = {SHOW: 1, PLAY_PAUSE: 4, NEXT: 5, PREVIOUS: 6, QUIT: 8}
COMMANDS = {number: command for command, number in IDS.items()}
SONG_ID = 2  # the current song, a label that does nothing
_SEPARATORS = (3, 7)
_LABEL_CHARS = 60  # a long title would make the whole menu that wide


@dataclass(frozen=True)
class State:
    """What the tray shows of the player."""
    song: str = ""  # "Title – Artist" of the current song, "" without one
    playing: bool = False
    can_play: bool = False
    can_next: bool = False
    can_previous: bool = False


@dataclass(frozen=True)
class Item:
    id: int
    label: str = ""  # "" for a separator
    enabled: bool = True
    command: str = ""  # "" for the song's label and the separators

    @property
    def separator(self) -> bool:
        return not self.label


def song_label(song: Any) -> str:
    return " – ".join(filter(None, (song.title, song.artist)))


def state_of(player: Any) -> State:
    song = player.current
    if song is None:
        return State()
    return State(song=song_label(song), playing=player.state == "playing", can_play=True,
                 can_next=player.has_next(), can_previous=True)


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def menu(state: State) -> list[Item]:
    """The menu, top to bottom."""
    items = [Item(IDS[SHOW], "Show Siphon", command=SHOW)]
    if state.song:
        items.append(Item(SONG_ID, shorten(state.song, _LABEL_CHARS), enabled=False))
    return items + [
        Item(_SEPARATORS[0]),
        Item(IDS[PLAY_PAUSE], "Pause" if state.playing else "Play", state.can_play, PLAY_PAUSE),
        Item(IDS[NEXT], "Next", state.can_next, NEXT),
        Item(IDS[PREVIOUS], "Previous", state.can_previous, PREVIOUS),
        Item(_SEPARATORS[1]),
        Item(IDS[QUIT], "Quit Siphon", command=QUIT),
    ]


def title(state: State) -> str:
    """One line naming Siphon and what it plays."""
    return f"Siphon: {state.song}" if state.song else "Siphon"


class Backend(Protocol):
    """What a platform's tray does for Tray. It calls Tray's run, scroll, set_available, set_token, open and
    end_session on the main thread only."""
    session_ending: bool

    def update(self, state: State) -> None: ...
    def balloon(self, heading: str, body: str) -> bool: ...
    def quitting(self) -> None: ...
    def close(self) -> None: ...


class Tray(GObject.Object):
    """The platform's tray icon, kept in step with the player.

    "command" carries SHOW, PLAY_PAUSE, NEXT, PREVIOUS or QUIT; "scroll" a number of wheel notches (up is
    positive); "open" the command line of a second Siphon (Windows); "session-end" says Windows is logging off
    or shutting down. available is False while nothing shows the icon: closing the window must quit then.
    """

    __gsignals__ = {
        "command": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "scroll": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "open": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "session-end": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }
    available = GObject.Property(type=bool, default=False)

    def __init__(self, player: Any, make_backend: Callable[["Tray"], Backend]) -> None:
        super().__init__()
        self._player = player
        self.state = state_of(player)
        self._token = ""
        self._backend = make_backend(self)
        self._changed = player.connect("changed", self._on_player_changed)

    @property
    def session_ending(self) -> bool:
        """Windows is ending the session (set from the tray's own thread, the moment Windows asks)."""
        return self._backend.session_ending

    def balloon(self, heading: str, body: str) -> bool:
        """A notification from the icon itself (Windows); False where the desktop's notifications are used."""
        return self._backend.balloon(heading, body)

    def take_token(self) -> str:
        """The activation token the tray host gave with the last click (Wayland), once."""
        token, self._token = self._token, ""
        return token

    def quitting(self) -> None:
        """Siphon's main loop is over (the start of its shutdown): on Windows a Siphon started from now on runs
        by itself instead of handing its links to this one."""
        if self._changed:
            self._backend.quitting()

    def close(self) -> None:
        """Remove the icon; nothing is emitted afterwards."""
        if self._changed:
            self._player.disconnect(self._changed)
            self._changed = 0
            self._backend.close()
            self.available = False

    # -- from the backend, on the main thread

    def run(self, command: str) -> None:
        if self._changed and command in IDS:
            self.emit("command", command)

    def scroll(self, notches: float) -> None:
        if self._changed:
            self.emit("scroll", notches)

    def set_available(self, available: bool) -> None:
        if self._changed and available != self.available:
            self.available = available

    def set_token(self, token: str) -> None:
        self._token = token

    def open(self, args: list[str]) -> None:
        if self._changed:
            self.emit("open", args)

    def end_session(self) -> None:
        if self._changed:
            self.emit("session-end")

    def _on_player_changed(self, player: Any) -> None:
        state = state_of(player)
        if state != self.state:  # "changed" also fires for the volume, shuffle and repeat
            self.state = state
            self._backend.update(state)


def create(player: Any, bus: Any) -> Tray | None:
    """The tray for this platform: Windows's notification area, or a StatusNotifierItem on the session bus
    (None without one). SIPHON_NO_TRAY=1 leaves it out."""
    if os.environ.get("SIPHON_NO_TRAY") == "1":
        return None
    if paths.windows():
        from .wintray import NotifyIcon

        return Tray(player, NotifyIcon)
    if bus is None:
        return None
    from .sni import StatusNotifierItem

    return Tray(player, lambda tray: StatusNotifierItem(bus, tray))
