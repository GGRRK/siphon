"""Download-engine updates: the newest yt-dlp, used from the next start.

update() fetches the newest yt-dlp wheel from PyPI, plus the yt-dlp-ejs wheel that release pins (the
YouTube challenge solver yt-dlp checks the version of), verifies both against PyPI's sha256 digests
and records them in <data>/engine/engine.json. activate() then puts them first on sys.path at the next
start: both are pure-Python wheels, which Python imports straight from the zip file. Nothing is
recorded until every check passed, so a failed download never replaces the engine in use.

A Linux venv updates this way too, not with pip: pip upgrades yt-dlp in place, under a running Siphon
that still imports extractors lazily, so one process could mix two versions. A new wheel has a new file
name and the running process keeps its own. The price: Python caches no bytecode for a zip, so importing
yt-dlp from the wheel takes 0.34 s instead of 0.11 s (measured 2026-09-26, Python 3.14).
"""

import hashlib
import http.client
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import paths

PYPI = "https://pypi.org/pypi"
MANIFEST = "engine.json"
_WHEEL_NAME = re.compile(r"[\w.+-]+-py3-none-any\.whl")


class UpdateError(Exception):
    """An update that could not be done, as one user-readable sentence."""


def engine_dir() -> Path:
    return paths.data_dir() / "engine"


def activate() -> str | None:
    """Put the downloaded engine first on sys.path when it is newer than the bundled one (a newer
    Siphon install brings a newer yt-dlp). Returns its version, or None when the bundled one stays."""
    if "yt_dlp" in sys.modules:
        return None
    found = installed()
    if found is None or _parts(found[0]) <= _parts(_bundled_version()):
        return None
    sys.path[0:0] = [str(wheel) for wheel in found[1]]
    return found[0]


def installed() -> tuple[str, list[Path]] | None:
    """(version, wheel files) of the downloaded engine, when there is a complete one."""
    try:
        manifest = json.loads((engine_dir() / MANIFEST).read_text(encoding="utf-8"))
        version, names = str(manifest["version"]), manifest["wheels"]
        wheels = [engine_dir() / name for name in names if _WHEEL_NAME.fullmatch(name)]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(wheels) != len(names) or not all(zipfile.is_zipfile(wheel) for wheel in wheels):
        return None
    return version, wheels


def update(current: str) -> str:
    """Download the newest yt-dlp unless `current` (or an earlier download) is already it.

    Returns the version the next start will use. Raises UpdateError."""
    try:
        release = _json(f"{PYPI}/yt-dlp/json")
        latest = str(release["info"]["version"])
        have = installed()
        newest = max(current, have[0], key=_parts) if have else current
        if _parts(latest) <= _parts(newest):
            return newest
        _check_python(release["info"].get("requires_python") or "")
        pin = _pin(release["info"].get("requires_dist") or [], "yt-dlp-ejs")
        solver = _json(f"{PYPI}/yt-dlp-ejs/{pin}/json" if pin else f"{PYPI}/yt-dlp-ejs/json")
        folder = engine_dir()
        folder.mkdir(parents=True, exist_ok=True)
        wheels = [_download(release, folder, "yt_dlp/version.py"),
                  _download(solver, folder, "yt_dlp_ejs/__init__.py")]
        version = _wheel_version(wheels[0])
        if _parts(version) != _parts(latest):
            raise UpdateError(f"PyPI's yt-dlp {latest} wheel says it is {version} - not installed.")
        from .library import write_atomic

        write_atomic(folder / MANIFEST, json.dumps({"version": version, "wheels": [w.name for w in wheels]}))
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile):
        raise UpdateError("PyPI sent something unexpected - try again later.") from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
        raise UpdateError(f"Couldn't reach PyPI ({getattr(e, 'reason', e)}) - check your connection.") from None
    except OSError as e:
        raise UpdateError(f"Couldn't save the engine: {e.strerror or e}.") from None
    _remove_old(folder, keep={w.name for w in wheels})
    return version


# ---------------------------------------------------------------- helpers


def _parts(version: str) -> tuple[int, ...]:
    """'2026.08.19' -> (2026, 8, 19): yt-dlp versions are dates, compared field by field."""
    return tuple(int(n) for n in re.findall(r"\d+", version))


def _bundled_version() -> str:
    # The packaged build must carry yt-dlp's dist-info for this; without it a download always wins.
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _check_python(requires: str) -> None:
    if (m := re.search(r">=\s*(\d+)\.(\d+)", requires)) and sys.version_info[:2] < (int(m[1]), int(m[2])):
        raise UpdateError(f"The newest yt-dlp needs Python {m[1]}.{m[2]} - install the latest Siphon instead.")


def _pin(requires_dist: list[str], project: str) -> str:
    """The exact version of project that a release's dependencies pin, or ''."""
    for requirement in requires_dist:
        if m := re.match(rf"{re.escape(project)}\s*==\s*([\w.]+)", requirement, re.IGNORECASE):
            return m[1]
    return ""


def _download(release: dict, folder: Path, must_hold: str) -> Path:
    """The release's pure-Python wheel, fetched into folder and checked; returns its path."""
    wheel = next((f for f in release["urls"] if f.get("packagetype") == "bdist_wheel"
                  and _WHEEL_NAME.fullmatch(f.get("filename", ""))), None)
    if wheel is None or not str(wheel["url"]).startswith("https://"):
        raise UpdateError(f"PyPI has no usable wheel of {release['info']['name']} {release['info']['version']}.")
    target, expected = folder / wheel["filename"], str(wheel["digests"]["sha256"]).lower()
    if target.is_file() and _sha256(target) == expected:
        return target  # fetched by an earlier attempt that failed later on
    fd, part = tempfile.mkstemp(prefix=f".{target.name}-", suffix=".part", dir=folder)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(wheel["url"], timeout=60) as response:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise UpdateError(f"{target.name} failed its checksum - not installed.")
        with zipfile.ZipFile(part) as archive:
            if must_hold not in archive.namelist() or archive.testzip() is not None:
                raise UpdateError(f"{target.name} is damaged - not installed.")
        os.replace(part, target)
    finally:
        Path(part).unlink(missing_ok=True)
    return target


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        source = archive.read("yt_dlp/version.py").decode("utf-8")
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", source, re.M)
    if m is None:
        raise ValueError("no __version__ in yt_dlp/version.py")
    return m[1]


def _remove_old(folder: Path, keep: set[str]) -> None:
    """Drop superseded wheels and leftover partial downloads, except wheels this process imports
    from: yt-dlp loads its extractors lazily, long after start-up."""
    keep = keep | {Path(entry).name for entry in sys.path if Path(entry).parent == folder}
    for old in folder.iterdir():
        if old.suffix in (".whl", ".part") and old.name not in keep:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass  # held open elsewhere (a virus scan, another Siphon): the next update removes it
