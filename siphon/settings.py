"""The app's remembered choices: download format and folder, player state, library sort, last page.

Stored as JSON in $XDG_CONFIG_HOME/siphon/settings.json (~/.config by
default; %APPDATA%\\Siphon on Windows). A missing or damaged file silently falls back to the defaults,
one value at a time, so files from older versions keep loading.
"""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths

REPEAT_MODES = ("off", "all", "one")
SORTS = ("added", "title", "artist")
PAGES = ("download", "library", "playlists")


@dataclass
class Settings:
    format: str
    folder: Path
    volume: float = 0.8
    shuffle: bool = False
    repeat: str = "off"
    sort: str = "added"
    page: str = "download"


def config_path() -> Path:
    return paths.config_dir() / "settings.json"


def _volume(value: object, default: float) -> float:
    # bool is an int subclass; `true` is not a volume.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(1.0, max(0.0, float(value)))


def load(defaults: Settings, formats: tuple[str, ...], path: Path | None = None) -> Settings:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults

    def choice(key: str, allowed: tuple[str, ...]) -> str:
        value = data.get(key)
        return value if value in allowed else getattr(defaults, key)

    folder = data.get("folder")
    shuffle = data.get("shuffle")
    return Settings(
        format=choice("format", formats),
        folder=Path(folder).expanduser() if isinstance(folder, str) and folder.strip() else defaults.folder,
        volume=_volume(data.get("volume"), defaults.volume),
        shuffle=shuffle if isinstance(shuffle, bool) else defaults.shuffle,
        repeat=choice("repeat", REPEAT_MODES),
        sort=choice("sort", SORTS),
        page=choice("page", PAGES),
    )


def save(settings: Settings, path: Path | None = None) -> None:
    """Write atomically, so a crash mid-write never leaves a half file behind."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings) | {"folder": str(settings.folder)}, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
