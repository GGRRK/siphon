"""Audio playback through libmpv: a play queue with shuffle, repeat and gapless song changes.

mpv runs its own event thread; every callback from it is handed to the GLib main loop, so all state lives on the
main thread and every signal is emitted there. mpv's playlist always holds at most two entries - the song playing
and the one that follows it - so mpv can cross into the next song without a gap, while the queue, the shuffle
order and repeat stay here.

The equalizer is mpv's filter chain (eq.chain). mpv builds a song's filters from its `af` value when the song starts
and again after every seek. Rewriting `af` under a playing song rebuilds the filters it changes from silence, which
clicks, so each song is loaded with the chain as a per-file option and a playing song's filters change by af-command,
which keeps their state. A band then moves cleanly even by 6 dB; only the preamp, a plain gain, jumps, so a big
preamp move is made in steps (eq.step). Filters are put into a playing song all at 0 dB, which leaves the sound as it
was (eq.filters), then moved; switching off leaves them at 0 dB. The next seek (whose own reset hides it) or song
rebuilds them as they are.

Measured on a 100 Hz tone at 0.25 of full scale, as the sharpest bend in the wave against a clean sine's at +12 dB:
Rock put in whole 114x, taken out 376x; filters at 0 dB put in or taken out 0.3x (the same as no change).
"""

import locale
import os
import random
import time
from collections.abc import Callable, Iterable, Sequence

import mpv
from gi.repository import GLib, GObject

from . import eq
from .library import Song

_TICK = 0.25  # seconds between "position" emissions while playing
_RESTART_AFTER = 3.0  # previous() restarts the song instead once this far in
_STEP_MS = 25  # between the equalizer's steps: about one block of sound for Opus, AAC and MP3 (20-26 ms)

_ERROR = mpv.MpvEventEndFile.ERROR
# mpv's own on-screen tools, each a Lua interpreter on a thread of its own
_TOOLS = ("load-stats-overlay", "load-console", "load-select", "load-positioning", "load-commands",
          "load-context-menu", "load-auto-profiles")


class Player(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "position": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # the position jumped (a seek, or the same song starting over): MPRIS clients need to hear about it
        "seeked": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.state = "stopped"
        self.current: Song | None = None
        self.position = 0.0
        self.duration = 0.0
        self.queue: list[Song] = []
        self.index = -1
        self.shuffle = False
        self.repeat = "off"
        self.volume = 1.0
        self._order: list[int] = []  # queue indices in play order; list order unless shuffled
        self._entry: int | None = None  # mpv playlist entry of the current song; None while stopped
        self._entries: dict[int, int] = {}  # mpv entries that may still start -> queue index
        self._failures = 0
        self._seeking = False  # drop time-pos reports from before a seek until mpv has landed
        self._opened = 0  # the mpv entry whose file mpv last finished opening
        self._pending_seek: float | None = None
        self._last_tick = 0.0  # event thread only
        self._playing_entry = 0  # event thread only: the entry mpv last started
        self._eq: tuple[float, ...] | None = None  # the gains asked for; None = no filters
        self._entry_eq: dict[int, tuple[float, ...] | None] = {}  # the gains each mpv entry was loaded with
        self._live_eq: tuple[float, ...] | None = None  # what the current song's filters play now; None = none there
        self._built_eq: tuple[float, ...] | None = None  # what its `af` holds: a seek rebuilds the filters from it
        self._ramp = 0  # GLib source of the equalizer's next step
        options = dict(video=False, ytdl=False, input_default_bindings=False, terminal=False,
                       gapless_audio="weak", audio_display=False, idle=True, audio_client_name="Siphon")
        if ao := os.environ.get("SIPHON_AO"):
            options["ao"] = ao
        # libmpv refuses to start under a non-C LC_NUMERIC (python-mpv then crashes). python-mpv sets it only
        # when first imported, and GTK's start-up sets the user's locale, so it is set again here.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self._mpv: mpv.MPV | None = mpv.MPV(**options)
        # Siphon has no mpv window to show mpv's tools in: off, they are 6 threads fewer and some memory (measured
        # 2026-09-28, mpv 0.41: they sleep while a song plays, with or without the equalizer). Switched off here rather
        # than among the options above, where one an older mpv lacks would stop mpv from starting; mpv closes the ones
        # it started already.
        for tool in _TOOLS:
            try:
                self._mpv[tool] = "no"
            except AttributeError:  # this mpv has no such tool
                pass
        self._mpv.register_event_callback(self._on_mpv_event)
        self._mpv.observe_property("time-pos", self._on_mpv_time)
        self._mpv.observe_property("duration", self._on_mpv_duration)
        self._mpv.observe_property("pause", self._on_mpv_pause)
        self._mpv.observe_property("idle-active", self._on_mpv_idle)

    # -- public controls ------------------------------------------------------------------------------------

    def play_songs(self, songs: list[Song], start: int = 0) -> None:
        if not songs:
            return
        self.queue = list(songs)
        self.index = min(max(start, 0), len(songs) - 1)
        self._reorder()
        self._load(self.index)

    def enqueue(self, songs: Iterable[Song]) -> None:
        songs = list(songs)
        if not songs:
            return
        first = len(self.queue)
        self.queue.extend(songs)
        new = range(first, len(self.queue))
        if self.index < 0:
            self._reorder()
            self._select(self._order[0])
        elif self.shuffle:
            here = self._order.index(self.index)
            for i in new:
                self._order.insert(random.randint(here + 1, len(self._order)), i)
        else:
            self._order.extend(new)
        self._queue_upcoming()
        self.emit("changed")

    def play_next(self, songs: Iterable[Song]) -> None:
        songs = list(songs)
        if not songs:
            return
        if self.index < 0:
            self.enqueue(songs)
            return
        at, count = self.index + 1, len(songs)
        self.queue[at:at] = songs
        self._order = [i + count if i >= at else i for i in self._order]
        self._entries = {entry: i + count if i >= at else i for entry, i in self._entries.items()}
        here = self._order.index(self.index)
        self._order[here + 1:here + 1] = range(at, at + count)
        self._queue_upcoming()
        self.emit("changed")

    def jump(self, index: int) -> None:
        if 0 <= index < len(self.queue):
            self._load(index)

    def toggle(self) -> None:
        if self.state == "playing":
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if self._mpv is None or self.current is None:
            return
        if self.state == "stopped":
            self._load(self.index)
        elif self.state == "paused":
            self._mpv.pause = False
            self._set_state("playing")
            self._retune()  # changes made while paused are stepped in now

    def pause(self) -> None:
        if self._mpv is not None and self.state == "playing":
            self._mpv.pause = True
            self._set_state("paused")

    def stop(self) -> None:
        if self._mpv is None or self.state == "stopped":
            return
        self._mpv.command("stop")
        self._entry = None
        self._entries.clear()
        self.position = 0.0
        self._set_state("stopped")

    def next(self) -> None:
        target = self._step(1, wrap=self.repeat != "off")
        if target is not None:
            self._load(target)

    def previous(self) -> None:
        if self.current is None:
            return
        target = self._step(-1, wrap=self.repeat != "off")
        if self.position > _RESTART_AFTER or target is None:
            if self.state == "stopped":
                self.play()
            else:
                self.seek(0.0)
        else:
            self._load(target)

    def seek(self, seconds: float) -> None:
        if self._mpv is None or self.state == "stopped":
            return
        seconds = max(0.0, seconds)
        if self.duration > 0:
            seconds = min(seconds, self.duration)
        if self._opened == self._entry:
            self._seek_now(seconds)
        else:
            self._pending_seek = seconds  # mpv refuses to seek until the file is open
        self.position = seconds
        self.emit("position", seconds)
        self.emit("seeked", seconds)

    def set_volume(self, volume: float) -> None:
        self.volume = min(max(volume, 0.0), 1.0)
        if self._mpv is not None:
            self._mpv.volume = self.volume * 100
        self.emit("changed")

    def set_equalizer(self, gains: Sequence[float] | None) -> None:
        """Play through eq.chain(gains) from now on; None, or every band at 0 dB, runs no filter at all (a song
        already playing through filters keeps them, at 0 dB, until its next seek)."""
        gains = tuple(gains) if gains is not None and any(gains) else None
        if gains == self._eq:
            return
        self._eq = gains
        if self._mpv is not None and self._entry is not None:
            self._queue_upcoming()  # first, so the follow-on song starts with the new chain even if this one ends now
            self._retune()

    def set_shuffle(self, on: bool) -> None:
        if on == self.shuffle:
            return
        self.shuffle = on
        self._reorder()
        self._queue_upcoming()
        self.emit("changed")

    def set_repeat(self, mode: str) -> None:
        if mode not in ("off", "all", "one"):
            raise ValueError(f"unknown repeat mode {mode!r}")
        if mode == self.repeat:
            return
        self.repeat = mode
        self._queue_upcoming()
        self.emit("changed")

    def has_next(self) -> bool:
        """Whether next() would move anywhere (not at the end of the queue with repeat off)."""
        return self._step(1, wrap=self.repeat != "off") is not None

    def shutdown(self) -> None:
        """Stop mpv and wait for it to exit; the player does nothing afterwards."""
        player, self._mpv = self._mpv, None
        if self._ramp:
            GLib.source_remove(self._ramp)
            self._ramp = 0
        if player is not None:
            player.terminate()

    # -- queue bookkeeping ------------------------------------------------------------------------------------

    def _reorder(self) -> None:
        """Rebuild the play order: list order, or a shuffle that starts at the current song."""
        count = len(self.queue)
        if not self.shuffle:
            self._order = list(range(count))
            return
        rest = [i for i in range(count) if i != self.index]
        random.shuffle(rest)
        self._order = ([self.index] if 0 <= self.index < count else []) + rest

    def _step(self, delta: int, wrap: bool) -> int | None:
        """The queue index `delta` places from the current song in play order, or None past either end."""
        if self.index < 0:
            return None
        spot = self._order.index(self.index) + delta
        if 0 <= spot < len(self._order):
            return self._order[spot]
        return self._order[spot % len(self._order)] if wrap else None

    def _upcoming(self) -> int | None:
        """What plays when the current song ends by itself."""
        if self.repeat == "one":
            return self.index
        return self._step(1, wrap=self.repeat == "all")

    def _select(self, index: int) -> None:
        self.index = index
        self.current = self.queue[index]
        self.position = 0.0
        self.duration = self.current.duration
        self._pending_seek = None

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("changed")

    # -- driving mpv ------------------------------------------------------------------------------------------

    def _load(self, index: int, skipping: bool = False) -> None:
        """Replace whatever mpv is doing with queue[index], playing from the start.

        `skipping` is set when moving past a song that failed, so the count of failures in a row carries on.
        """
        if self._mpv is None:
            return
        if not skipping:
            self._failures = 0
        restart = self.state != "stopped" and self.queue[index] == self.current
        self._select(index)
        self._mpv.pause = False
        self._entry = self._append(index, "replace")
        self._entries = {self._entry: index}
        self._entry_eq = {self._entry: self._eq}
        self._live_eq = self._built_eq = self._eq
        self._seeking = False
        self.state = "playing"
        self._queue_upcoming()
        self.emit("changed")
        if restart:
            self.emit("seeked", 0.0)

    def _seek_now(self, seconds: float) -> None:
        # the seek rebuilds the filters from `af`, so it must hold what plays now; the seek's own reset hides this one
        if self._built_eq != self._eq:
            self._mpv.af = eq.chain(self._eq)
            self._built_eq = self._eq
        self._live_eq = self._built_eq
        self._mpv.command("seek", seconds, "absolute+exact")
        self._seeking = True

    def _append(self, index: int, mode: str) -> int:
        # raw bytes: a file name need not be valid UTF-8. A per-file `af` is the song's own; mpv puts the global
        # value back when the song ends, before the next song's filters are made.
        result = self._mpv.command("loadfile", os.fsencode(self.queue[index].path), mode, -1,
                                   f"af=[{eq.chain(self._eq)}]")
        entry = result["playlist_entry_id"]
        self._entry_eq[entry] = self._eq
        return entry

    def _retune(self) -> None:
        """Move the playing song's filters a step toward self._eq, and schedule the next step.

        Only while playing: steps made in a pause would all land together at the resume. Until mpv has opened the
        song, its filters do not exist (a command would reach the previous song's), so _loaded() calls this again.
        """
        if self._mpv is None or self._opened != self._entry or self.state != "playing":
            return
        target = self._eq if self._live_eq is None else self._eq or eq.FLAT  # filters in place stay, at 0 dB
        if self._live_eq == target:
            return
        if self._live_eq is None:
            self._mpv.af = eq.filters(eq.FLAT)
            self._live_eq = self._built_eq = eq.FLAT
        here = self._live_eq
        step = eq.step(here, target)
        try:
            for args in eq.commands(here, step):
                self._mpv.command("af-command", *args)
        except SystemError:  # MPV_ERROR_COMMAND: mpv makes a filter when the sound next reaches it; try again then
            step = here
        self._live_eq = step
        if step != target and not self._ramp:
            self._ramp = GLib.timeout_add(_STEP_MS, self._on_ramp)

    def _on_ramp(self) -> bool:
        self._ramp = 0
        self._retune()
        return GLib.SOURCE_REMOVE

    def _queue_upcoming(self) -> None:
        """Make mpv's follow-on entry match what should play next (playlist-clear keeps the playing entry)."""
        if self._mpv is None or self._entry is None:
            return
        self._mpv.command("playlist-clear")
        target = self._upcoming()
        if target is not None:
            self._entries[self._append(target, "append")] = target

    def _finish(self) -> None:
        """The queue ran out: rewind to its start, stopped."""
        self._entry = None
        self._entries.clear()
        if self._order:
            self._select(self._order[0])
        self.state = "stopped"
        self.emit("changed")

    # -- mpv callbacks (event thread): read what is needed now, handle it on the main loop ------------------

    def _post(self, handler: Callable[..., None], *args) -> None:
        def run() -> bool:
            if self._mpv is not None:
                handler(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)

    def _on_mpv_event(self, event: mpv.MpvEvent) -> None:
        kind = event.event_id.value
        if kind == mpv.MpvEventID.START_FILE:
            self._playing_entry = event.data.playlist_entry_id
            self._post(self._started, self._playing_entry)
        elif kind == mpv.MpvEventID.END_FILE:
            data = event.data
            self._post(self._ended, data.playlist_entry_id, data.reason, data.error)
        elif kind == mpv.MpvEventID.FILE_LOADED:
            self._post(self._loaded, self._playing_entry)
        elif kind == mpv.MpvEventID.PLAYBACK_RESTART:
            self._post(self._landed)

    def _on_mpv_time(self, _name: str, value: float | None) -> None:
        now = time.monotonic()
        if value is not None and now - self._last_tick >= _TICK:
            self._last_tick = now
            self._post(self._ticked, self._playing_entry, value)

    def _on_mpv_duration(self, _name: str, value: float | None) -> None:
        if value:
            self._post(self._timed, self._playing_entry, value)

    def _on_mpv_pause(self, _name: str, value: bool) -> None:
        self._post(self._paused, bool(value))

    def _on_mpv_idle(self, _name: str, value: bool) -> None:
        if value:
            self._post(self._idled, self._playing_entry)

    # -- main-loop handlers -----------------------------------------------------------------------------------

    def _started(self, entry: int) -> None:
        if entry == self._entry or entry not in self._entries:
            return  # the song we just asked for, or one superseded by a later replace
        # mpv moved on to the follow-on entry by itself
        index = self._entries[entry]
        self._entries = {e: i for e, i in self._entries.items() if e >= entry}
        self._entry_eq = {e: gains for e, gains in self._entry_eq.items() if e >= entry}
        restart = self.queue[index] == self.current
        self._entry = entry
        self._live_eq = self._built_eq = self._entry_eq[entry]
        self._select(index)
        self._seeking = False
        self._queue_upcoming()
        self.emit("changed")
        if restart:
            self.emit("seeked", 0.0)

    def _ended(self, entry: int, reason: int, error: int) -> None:
        if entry not in self._entries:
            return
        del self._entries[entry]
        if entry == self._entry and reason == _ERROR:
            self._failed(error)

    def _failed(self, error: int) -> None:
        song = self.current
        why = "the file is missing" if not song.path.exists() else mpv.ErrorCode.human_readable(error)
        self.emit("error", f"Couldn't play “{song.title}”: {why}.")
        self._failures += 1
        target = self._step(1, wrap=self.repeat != "off")
        # every song in the queue failed in a row: with repeat on, skipping further would go round for ever
        if target is None or self._failures >= len(self.queue):
            self._mpv.command("stop")
            self._finish()
        else:
            self._load(target, skipping=True)

    def _loaded(self, entry: int) -> None:
        self._opened = entry
        if entry == self._entry:
            self._failures = 0
            self._retune()  # the equalizer changed while the song was being opened
            if self._pending_seek is not None:
                self._seek_now(self._pending_seek)
                self._pending_seek = None

    def _landed(self) -> None:
        self._seeking = False

    def _ticked(self, entry: int, seconds: float) -> None:
        if entry == self._entry and self.state == "playing" and not self._seeking:
            self.position = seconds
            self.emit("position", seconds)

    def _timed(self, entry: int, seconds: float) -> None:
        if entry == self._entry and seconds != self.duration:
            self.duration = seconds
            self.emit("changed")

    def _paused(self, paused: bool) -> None:
        if self.state != "stopped":
            self._set_state("paused" if paused else "playing")

    def _idled(self, entry: int) -> None:
        # the song mpv ended on is still ours, so nothing followed it: the queue ran out (and mpv has played out
        # its buffer). An older idle report, from before a newer load, carries an older entry and is ignored.
        if entry == self._entry and self.state != "stopped":
            self._finish()
