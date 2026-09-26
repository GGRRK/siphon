"""Siphon's and its download engine's updates, as state the window and the settings show.

Checks run on worker threads; the state changes, and the signals are emitted, on the main thread only.
Read the attributes after "changed":

- state: "idle" (not checked yet), "checking", "downloading" (fraction), "ready" (restart_to_update installs
  it; it also installs by itself: on Windows when Siphon quits, from a git clone at the next start),
  "up-to-date", "available" (the portable zip: page is the release to download), "unavailable" (this copy
  can't update itself; message says why) or "error" (message)
- version (running), latest (GitHub's latest release, "" before a check), fraction, message (one sentence
  for any state), page, checked (time.time() of the last finished check, 0 before)
- engine_state: "idle", "checking", "ready" (engine_pending is used from the next start), "up-to-date" or
  "error"; engine_version (in use), engine_pending, engine_message, engine_checked
"""

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from gi.repository import GLib, GObject

from .. import __version__, updater
from .downloader import unexpected

BUSY = ("checking", "downloading")


class Updates(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A check ended (its state is set); True when someone asked for it rather than the start-up.
        "app-checked": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "engine-checked": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, core: ModuleType, quit_app: Callable[[], None]) -> None:
        """quit_app closes Siphon the way the user would (asking first while downloads run)."""
        super().__init__()
        self._core = core
        self._quit_app = quit_app
        self._installer: Path | None = None
        self._restart = False
        self.version = __version__
        self.state, self.latest, self.fraction, self.message, self.page, self.checked = "idle", "", 0.0, "", "", 0.0
        self.engine_version = core.engine_version()
        self.engine_state, self.engine_pending, self.engine_message, self.engine_checked = "idle", "", "", 0.0

    @property
    def busy(self) -> bool:
        return self.state in BUSY

    @property
    def engine_busy(self) -> bool:
        return self.engine_state == "checking"

    def start(self, auto_update: bool, auto_engine: bool) -> None:
        """At launch: the checks the settings allow, in the background."""
        if auto_update:
            self._check(manual=False)
        if auto_engine:
            self._update_engine(manual=False)

    def check_now(self) -> None:
        """Check GitHub and get a newer Siphon ready, whatever the settings say."""
        self._check(manual=True)

    def update_engine_now(self) -> None:
        """Fetch the newest yt-dlp for the next start, whatever the settings say."""
        self._update_engine(manual=True)

    def restart_to_update(self) -> None:
        """Quit and start the updated Siphon."""
        if self.state == "ready":
            self._restart = True
            self._quit_app()

    def cancel_restart(self) -> None:
        """The quit was called off: downloads kept running."""
        self._restart = False

    def finish(self) -> None:
        """At shutdown: the Windows installer runs whenever an update is ready (it opens Siphon again only after
        Restart); a git clone is updated by bin/siphon at its next start, which Restart makes now."""
        if self.state != "ready" or (self._installer is None and not self._restart):
            return
        try:
            if self._installer is not None:
                updater.run_installer(self._installer, relaunch=self._restart)
            else:
                updater.relaunch()
        except OSError as exc:
            _log(f"couldn't start the update: {exc}")

    # -- Siphon

    def _check(self, manual: bool) -> None:
        if self.busy or self.state == "ready":
            return
        self._set({"state": "checking", "fraction": 0.0, "message": "Checking for updates…"})
        threading.Thread(target=self._check_work, args=(manual,), name="siphon-update", daemon=True).start()

    def _check_work(self, manual: bool) -> None:
        installer = None
        try:
            release = updater.latest_release()
            found = {"latest": release.version, "page": release.page}
            if not updater.newer(release.version):
                updater.forget_downloads()
                result = found | {"state": "up-to-date", "message": f"Siphon {__version__} is up to date."}
            else:
                GLib.idle_add(self._set, found)
                state, installer = updater.prepare(release, self._progress(release.version))
                if state == "available":
                    message = f"Siphon {release.version} is available from its release page."
                elif installer is not None:
                    message = f"Siphon {release.version} is ready. It installs when Siphon closes."
                else:
                    message = f"Siphon {release.version} is ready. It installs when Siphon next starts."
                result = found | {"state": state, "message": message}
        except updater.Unavailable as exc:
            result = {"state": "unavailable", "message": str(exc)}
        except updater.UpdateError as exc:
            result = {"state": "error", "message": str(exc)}
        except Exception as exc:
            result = {"state": "error", "message": unexpected(exc)}
        _log(result["message"])
        GLib.idle_add(self._checked, result | {"fraction": 1.0 if installer else 0.0}, installer, manual)

    def _progress(self, version: str) -> Callable[[float], None]:
        shown = -1

        def report(fraction: float) -> None:  # on the download's thread; the main loop hears of whole percents
            nonlocal shown
            if int(fraction * 100) != shown:
                shown = int(fraction * 100)
                GLib.idle_add(self._set, {"state": "downloading", "fraction": fraction,
                                          "message": f"Downloading Siphon {version}…"})

        return report

    def _checked(self, result: dict, installer: Path | None, manual: bool) -> bool:
        self._installer = installer
        self._set(result | {"checked": time.time()})
        self.emit("app-checked", manual)
        return GLib.SOURCE_REMOVE

    # -- the engine

    def _update_engine(self, manual: bool) -> None:
        if self.engine_busy:
            return
        self._set({"engine_state": "checking", "engine_message": "Checking for a newer download engine…"})
        threading.Thread(target=self._engine_work, args=(manual,), name="siphon-update-engine", daemon=True).start()

    def _engine_work(self, manual: bool) -> None:
        try:
            after = self._core.update_engine()
            if after != self.engine_version:
                result = {"engine_state": "ready", "engine_pending": after,
                          "engine_message": f"yt-dlp {after} will be used from the next start."}
            else:
                result = {"engine_state": "up-to-date", "engine_pending": "",
                          "engine_message": f"The engine is up to date (yt-dlp {after})."}
        except self._core.SiphonError as exc:
            result = {"engine_state": "error", "engine_message": str(exc) or "The engine update failed."}
        except Exception as exc:
            result = {"engine_state": "error", "engine_message": unexpected(exc)}
        _log(result["engine_message"])
        GLib.idle_add(self._engine_checked, result, manual)

    def _engine_checked(self, result: dict, manual: bool) -> bool:
        self._set(result | {"engine_checked": time.time()})
        self.emit("engine-checked", manual)
        return GLib.SOURCE_REMOVE

    def _set(self, fields: dict) -> bool:
        for name, value in fields.items():
            setattr(self, name, value)
        self.emit("changed")
        return GLib.SOURCE_REMOVE


def _log(message: str) -> None:
    # stderr: the terminal or the session's journal, and siphon.log in the windowed Windows build
    print(f"siphon: {message}", file=sys.stderr)
