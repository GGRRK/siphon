"""Runs downloads in worker threads, a few at a time, and feeds rows their progress.

Every method here runs on the GTK main thread except `Job.report` and
`Downloader._work`, which run on a worker and only talk back via GLib.idle_add.
"""

import threading
import traceback
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from gi.repository import GLib

from .row import State, TrackRow

_STAGES = {"matching": State.MATCHING, "downloading": State.DOWNLOADING,
           "converting": State.CONVERTING, "tagging": State.TAGGING}


def unexpected(exc: BaseException) -> str:
    """A one-line message for an error the engine did not phrase for users."""
    traceback.print_exception(exc)
    first = (str(exc).splitlines() or [type(exc).__name__])[0]
    return f"Something went wrong ({first[:120]})."


class Job:
    def __init__(self, track, outdir: Path, fmt: str, row: TrackRow) -> None:
        self.track = track
        self.outdir = outdir
        self.fmt = fmt
        self.row = row
        self.path: Path | None = None
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.cancel = threading.Event()
        self.already = False
        self.finished = False
        self._pending = None

    def report(self, progress) -> None:
        """Progress callback for the engine (worker thread). Bursts collapse into one UI update."""
        if progress.stage == "done" and progress.detail == "already downloaded":
            self.already = True
        with self._lock:
            flush_queued = self._pending is not None
            self._pending = progress
        if not flush_queued:
            GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        with self._lock:
            progress, self._pending = self._pending, None
        if progress is None:  # the job was reset for a retry since this was queued
            return GLib.SOURCE_REMOVE
        state = _STAGES.get(progress.stage)
        if state and not self.finished and not self.cancel.is_set():
            self.row.set_state(state, progress.fraction if state is State.DOWNLOADING else None)
        return GLib.SOURCE_REMOVE


class Downloader:
    def __init__(self, core: ModuleType, on_error: Callable[[str], None], on_file: Callable[[Path], None],
                 limit: int = 2) -> None:
        self._core = core
        self._on_error = on_error
        self._on_file = on_file
        self._limit = limit
        self._waiting: deque[Job] = deque()
        self._running: set[Job] = set()

    @property
    def running(self) -> int:
        return len(self._running)

    def submit(self, job: Job) -> None:
        job.reset()
        job.row.set_state(State.QUEUED)
        self._waiting.append(job)
        self._pump()

    def cancel(self, job: Job) -> None:
        if job in self._waiting:
            self._waiting.remove(job)
            job.row.set_state(State.CANCELLED)
        elif job in self._running and not job.cancel.is_set():
            job.cancel.set()
            job.row.set_state(State.CANCELLING)

    def _pump(self) -> None:
        while self._waiting and len(self._running) < self._limit:
            job = self._waiting.popleft()
            self._running.add(job)
            # Tracks from Spotify and the like have no url until they are matched.
            job.row.set_state(State.DOWNLOADING if job.track.url else State.MATCHING)
            threading.Thread(target=self._work, args=(job,), name="siphon-download", daemon=True).start()

    def _work(self, job: Job) -> None:
        try:
            outcome = self._core.download(job.track, job.outdir, job.fmt, job.report, job.cancel)
        except self._core.SiphonError as exc:
            outcome = exc
        except Exception as exc:
            outcome = RuntimeError(unexpected(exc))
        GLib.idle_add(self._finish, job, outcome)

    def _finish(self, job: Job, outcome: Path | Exception) -> bool:
        self._running.discard(job)
        job.finished = True
        if isinstance(outcome, Path):
            job.path = outcome
            job.row.set_state(State.ALREADY if job.already else State.DONE)
            self._on_file(outcome)
        elif job.cancel.is_set() or isinstance(outcome, self._core.Cancelled):
            job.row.set_state(State.CANCELLED)
        else:
            message = str(outcome) or "The download failed."
            job.row.fail(message)
            self._on_error(message)
        self._pump()
        return GLib.SOURCE_REMOVE
