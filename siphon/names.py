"""The extra rules Windows puts on file names and paths (Linux only forbids '/' and NUL).

Windows refuses <>:"/\\|?* and control characters, drops trailing dots and spaces, reserves device
names such as CON or NUL even with an extension, and - unless long paths are switched on, which
neither Siphon nor ffmpeg can count on - refuses paths longer than MAX_PATH characters.
"""

import re
from pathlib import Path

MAX_PATH = 259  # characters in a path, not counting the terminating NUL
# NUL.txt and NUL.tar.gz are the device NUL too; so are the superscript-digit ports.
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]) *(?:\..*)?", re.IGNORECASE | re.DOTALL)
_CHARS = str.maketrans({'"': "'", "\\": "-", "|": "-", "<": "", ">": "", "?": "", "*": ""})


def windows_chars(text: str) -> str:
    """text with the characters Windows forbids in names replaced: 'Song: Live?' -> 'Song - Live'."""
    return re.sub(r":\s", " - ", text).replace(":", "-").translate(_CHARS)


def unreserved(name: str) -> str:
    """name, prefixed with '_' when Windows would take it for a device."""
    return f"_{name}" if _RESERVED.fullmatch(name) else name


def room(folder: Path) -> int:
    """Characters left for one name inside folder before the path reaches MAX_PATH."""
    return MAX_PATH - _units(str(folder)) - 1


def shorten(name: str, limit: int) -> str:
    """name cut to at most limit characters (UTF-16 units, as Windows counts), with no trailing dot or space."""
    while _units(name) > limit:
        name = name[:-1]
    return name.rstrip(" .")


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
