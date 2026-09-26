"""Human-readable counts and durations for the music pages."""

from pathlib import Path

from ..paths import windows


def clock(seconds: float) -> str:
    """3:05, or 1:02:03 past an hour; unknown lengths read as 0:00."""
    total = max(0, int(seconds or 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def span(seconds: float) -> str:
    """A total play time: 45 min, 3 h 20 min, 2 h."""
    minutes = round(max(0.0, seconds) / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def songs(count: int) -> str:
    return f"{count} song" if count == 1 else f"{count} songs"


def summary(items: list, of: int | None = None) -> str:
    """'12 songs · 40 min', or '12 of 312 songs · 40 min' for a filtered view."""
    count = songs(len(items)) if of is None else f"{len(items)} of {songs(of)}"
    total = sum(song.duration for song in items)
    return f"{count} · {span(total)}" if total > 0 else count


def pretty_path(path: Path) -> str:
    """A folder for display: ~/Music on Linux; Windows users know the full path, not '~'."""
    if windows():
        return str(path)
    home = Path.home()
    if path == home:
        return "~"
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def split_stem(stem: str) -> tuple[str, str]:
    """(artist, title) from an "Artist - Title" file name, the way the library reads untagged files."""
    artist, sep, title = stem.partition(" - ")
    return (artist.strip(), title.strip()) if sep and artist.strip() and title.strip() else ("", stem)


def drop_index(src: int, target: int, after: bool) -> int:
    """Where a row dragged from `src` lands when dropped before (or after) row `target`, counted after removal."""
    dst = target + (1 if after else 0)
    return dst - 1 if src < dst else dst
