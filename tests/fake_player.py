"""Stand-in for siphon.player.Player that makes no sound: a timer plays the queue.

Selected with SIPHON_FAKE_PLAYER=1. Songs whose file name contains
"broken" fail to load. SIPHON_FAKE_SPEED (default 1) speeds time up.
"""

import os
import random

from gi.repository import GLib, GObject

_TICK_MS = 250


class Player(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "position": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "seeked": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.state = "stopped"
        self.current = None
        self.position = 0.0
        self.duration = 0.0
        self.queue: list = []
        self.index = -1
        self.shuffle = False
        self.repeat = "off"
        self.volume = 0.8
        self.equalizer = None  # the gains of the last set_equalizer(), None when flat or off
        self._speed = float(os.environ.get("SIPHON_FAKE_SPEED", "1"))
        self._timer = 0

    # -- queue

    def play_songs(self, songs: list, start: int = 0) -> None:
        self.queue = list(songs)
        self._load(start)

    def enqueue(self, songs) -> None:
        self.queue.extend(songs)
        if self.current is None:
            self._load(len(self.queue) - len(songs))
        else:
            self.emit("changed")

    def play_next(self, songs) -> None:
        self.queue[self.index + 1:self.index + 1] = list(songs)
        if self.current is None:
            self._load(self.index + 1)
        else:
            self.emit("changed")

    def jump(self, index: int) -> None:
        self._load(index)

    # -- transport

    def toggle(self) -> None:
        self.pause() if self.state == "playing" else self.play()

    def play(self) -> None:
        if self.current is not None:
            self._set_state("playing")

    def pause(self) -> None:
        if self.state == "playing":
            self._set_state("paused")

    def stop(self) -> None:
        self.current = None
        self.position = self.duration = 0.0
        self._set_state("stopped")

    def next(self) -> None:
        self._advance(by_user=True)

    def previous(self) -> None:
        if self.position > 3 or self.index <= 0:
            self.seek(0)
        else:
            self._load(self.index - 1)

    def seek(self, seconds: float) -> None:
        self.position = max(0.0, min(seconds, self.duration))
        self.emit("position", self.position)
        self.emit("seeked", self.position)

    def set_volume(self, v: float) -> None:
        self.volume = max(0.0, min(1.0, v))
        self.emit("changed")

    def set_equalizer(self, gains) -> None:
        self.equalizer = tuple(gains) if gains is not None and any(gains) else None

    def set_shuffle(self, on: bool) -> None:
        self.shuffle = on
        self.emit("changed")

    def set_repeat(self, mode: str) -> None:
        self.repeat = mode
        self.emit("changed")

    def has_next(self) -> bool:
        return self.current is not None and (self.index + 1 < len(self.queue) or self.repeat != "off")

    def shutdown(self) -> None:
        self.stop()

    # -- internals

    def _load(self, index: int) -> None:
        if not 0 <= index < len(self.queue):
            self.stop()
            return
        self.index = index
        song = self.queue[index]
        if "broken" in song.path.name:
            self.emit("error", f"Could not play “{song.title}”.")
            self._advance(by_user=False)
            return
        self.current = song
        self.position = 0.0
        self.duration = song.duration or 180.0
        self._set_state("playing")
        self.emit("position", 0.0)

    def _advance(self, by_user: bool) -> None:
        if self.repeat == "one" and not by_user and self.current is not None:
            self._load(self.index)
        elif self.shuffle and len(self.queue) > 1:
            self._load(random.choice([i for i in range(len(self.queue)) if i != self.index]))
        elif self.index + 1 < len(self.queue):
            self._load(self.index + 1)
        elif self.repeat == "all" and self.queue:
            self._load(0)
        else:
            self.stop()

    def _set_state(self, state: str) -> None:
        self.state = state
        if state == "playing" and not self._timer:
            self._timer = GLib.timeout_add(_TICK_MS, self._tick)
        elif state != "playing" and self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.emit("changed")

    def _tick(self) -> bool:
        self.position += _TICK_MS / 1000 * self._speed
        if self.position >= self.duration:
            self._timer = 0
            self._advance(by_user=False)
            return GLib.SOURCE_REMOVE
        self.emit("position", self.position)
        return GLib.SOURCE_CONTINUE
