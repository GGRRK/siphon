"""Where Siphon keeps its files on each platform, and the start-up that finds a packaged build's tools.

Linux follows the XDG base directories. Windows keeps settings in %APPDATA%\\Siphon, the cache and
engine updates in %LOCALAPPDATA%\\Siphon and downloads in the Music known folder. A packaged build
ships ffmpeg, ffprobe, a JavaScript runtime and libmpv in <bundle>/bin.
"""

import os
import re
import sys
from pathlib import Path, PureWindowsPath

CREATE_NO_WINDOW = 0x08000000  # Windows: a console tool started from the window app flashes no console
# Converting and the like run below normal priority, so a game (or anything else the user runs) keeps the CPU it
# needs and the work fills what is left; the player is in Siphon's own process, which keeps normal priority.
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # Windows: one class below normal
BACKGROUND_NICE = 10  # POSIX: added to the nice value a helper starts with, up to 19


def windows() -> bool:
    return sys.platform == "win32"


def no_window() -> int:
    """subprocess creationflags for the helper tools Siphon runs."""
    return CREATE_NO_WINDOW if windows() else 0


def background() -> int:
    """subprocess creationflags that start a helper tool below normal priority on Windows (for POSIX, where a
    program cannot be started so, see lower_priority)."""
    return BELOW_NORMAL_PRIORITY_CLASS if windows() else 0


def lower_priority(pid: int) -> None:
    """POSIX: a helper tool just started drops below normal priority (Windows starts it there: background())."""
    if windows():
        return
    try:
        os.setpriority(os.PRIO_PROCESS, pid, min(19, os.getpriority(os.PRIO_PROCESS, pid) + BACKGROUND_NICE))
    except OSError:  # it has finished already
        pass


def config_dir() -> Path:
    if windows():
        return _windows_base("APPDATA", "Roaming") / "Siphon"
    return _xdg("XDG_CONFIG_HOME", ".config") / "siphon"


def cache_dir() -> Path:
    if windows():
        return _windows_base("LOCALAPPDATA", "Local") / "Siphon" / "cache"
    return _xdg("XDG_CACHE_HOME", ".cache") / "siphon"


def data_dir() -> Path:
    if windows():
        return _windows_base("LOCALAPPDATA", "Local") / "Siphon"
    return _xdg("XDG_DATA_HOME", ".local/share") / "siphon"


def music_dir() -> Path:
    """The default download folder: the user's Music folder."""
    if windows():
        return Path(_known_music_folder() or Path(os.environ.get("USERPROFILE") or Path.home()) / "Music")
    music = os.environ.get("XDG_MUSIC_DIR") or _user_dir("MUSIC")
    return Path(music).expanduser() if music else Path.home() / "Music"


def bundle_dir() -> Path | None:
    """The folder of a packaged build (the one holding bin/); None when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    bundle = os.environ.get("SIPHON_BUNDLE")
    return Path(os.path.abspath(bundle)) if bundle else None  # python-mpv refuses a DLL on a relative PATH entry


def setup_environment() -> None:
    """Run first thing: put a packaged build's tools on PATH and load a downloaded engine update."""
    bundle = bundle_dir()
    tools = bundle / "bin" if bundle is not None else None
    if tools is not None and tools.is_dir():
        # yt-dlp finds ffmpeg and the JS runtime on PATH; python-mpv looks for libmpv-2.dll there too.
        os.environ["PATH"] = os.pathsep.join(filter(None, (str(tools), os.environ.get("PATH"))))
        if windows():
            os.add_dll_directory(str(tools))  # the DLLs libmpv-2.dll itself depends on
    from . import engine

    engine.activate()


# ---------------------------------------------------------------- helpers


def _xdg(variable: str, fallback: str) -> Path:
    base = os.environ.get(variable, "")
    # The XDG spec says relative paths are invalid and must be ignored.
    return Path(base) if os.path.isabs(base) else Path.home() / fallback


def _windows_base(variable: str, appdata_child: str) -> Path:
    base = os.environ.get(variable, "")
    if PureWindowsPath(base).is_absolute():
        return Path(base)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "AppData" / appdata_child


def _user_dir(name: str) -> str:
    """A folder from ~/.config/user-dirs.dirs (xdg-user-dirs), or ''."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        text = (config / "user-dirs.dirs").read_text()
    except OSError:
        return ""
    m = re.search(rf'^XDG_{name}_DIR="([^"]*)"', text, re.M)
    return m.group(1).replace("$HOME", str(Path.home())) if m else ""


def _known_music_folder() -> str:
    """The Music known folder (it can be moved, e.g. into OneDrive), or '' when Windows won't say."""
    import ctypes
    import uuid
    from ctypes import wintypes

    folder_id = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("4BD8D571-6D19-48D3-BE97-422220080E43").bytes_le)
    found = ctypes.c_wchar_p()
    get = ctypes.windll.shell32.SHGetKnownFolderPath
    get.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    get.restype = ctypes.c_long  # HRESULT, checked by hand instead of raising
    try:
        if get(ctypes.byref(folder_id), 0, None, ctypes.byref(found)) != 0:
            return ""
        return found.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(found)
