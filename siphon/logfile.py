"""The windowed Windows build has no console, so its output goes to <data>/siphon.log instead."""

import ctypes
import faulthandler
import sys
from pathlib import Path

from . import paths

NAME = "siphon.log"


def redirect() -> None:
    """Send this session's output to the log: Python's, and that of the C libraries (GLib, GTK, libmpv)."""
    log_path = paths.data_dir() / NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"")  # one session per log
    # Append mode everywhere: Python and the C runtime write through separate handles.
    log = open(log_path, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
    sys.stdout = sys.stderr = log
    faulthandler.enable(log)
    if paths.windows():
        _reopen_c_streams(log_path)


def _reopen_c_streams(log_path: Path) -> None:
    # A windowed program's C stdout and stderr belong to no file (descriptor -2), so dup2 onto 1 and 2 cannot
    # reach them: reopen the C runtime's own streams. GLib prints debug and info messages to stdout.
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    # The C runtime as already loaded, the one Python and GLib share: loading it by name could bring a copy.
    ucrt = ctypes.CDLL("ucrtbase", handle=kernel32.GetModuleHandleW("ucrtbase.dll"))
    ucrt.__acrt_iob_func.argtypes = [ctypes.c_uint]
    ucrt.__acrt_iob_func.restype = ctypes.c_void_p
    ucrt._wfreopen.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
    ucrt._wfreopen.restype = ctypes.c_void_p
    for stream in (1, 2):
        ucrt._wfreopen(str(log_path), "a", ucrt.__acrt_iob_func(stream))
