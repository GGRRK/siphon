from dataclasses import dataclass
from pathlib import Path

import pytest

from siphon.ui.fmt import clock, drop_index, pretty_path, songs, span, split_stem, summary


@dataclass
class _Song:
    duration: float


@pytest.mark.parametrize("seconds, text", [(0, "0:00"), (None, "0:00"), (-4, "0:00"), (5.9, "0:05"),
                                           (65, "1:05"), (3599, "59:59"), (3723, "1:02:03")])
def test_clock(seconds, text):
    assert clock(seconds) == text


@pytest.mark.parametrize("seconds, text", [(10, "1 min"), (2700, "45 min"), (3540, "59 min"), (3570, "1 h"),
                                           (3600, "1 h"), (7200, "2 h"), (12000, "3 h 20 min")])
def test_span(seconds, text):
    assert span(seconds) == text


def test_song_counts_and_summaries():
    assert songs(1) == "1 song"
    assert songs(0) == "0 songs"
    items = [_Song(1800), _Song(900)]
    assert summary(items) == "2 songs · 45 min"
    assert summary(items, of=300) == "2 of 300 songs · 45 min"
    assert summary([]) == "0 songs"
    assert summary([_Song(0)]) == "1 song"  # no length known: no "0 min"


@pytest.mark.linux
def test_pretty_path_shortens_home():
    home = Path.home()
    assert pretty_path(home) == "~"
    assert pretty_path(home / "Music") == "~/Music"
    assert pretty_path(Path("/srv/audio")) == "/srv/audio"


@pytest.mark.parametrize("stem, parts", [
    ("Kaito Mori - Neon Harbour", ("Kaito Mori", "Neon Harbour")),
    ("A - B - C", ("A", "B - C")),
    ("No Separator", ("", "No Separator")),
    (" - Title", ("", " - Title")),
    ("Artist - ", ("", "Artist - ")),
])
def test_split_stem(stem, parts):
    assert split_stem(stem) == parts


def _move(items: list, src: int, target: int, after: bool) -> list:
    moved = list(items)
    moved.insert(drop_index(src, target, after), moved.pop(src))
    return moved


@pytest.mark.parametrize("src, target, after, expected", [
    (0, 3, False, "bcade"),  # down, dropped on the top half of d
    (0, 3, True, "bcdae"),   # down, bottom half of d
    (4, 1, False, "aebcd"),  # up, top half of b
    (4, 1, True, "abecd"),   # up, bottom half of b
    (2, 2, False, "abcde"),  # onto itself
    (2, 1, True, "abcde"),   # just below its upper neighbour: where it already is
    (0, 4, True, "bcdea"),   # to the very end
])
def test_drop_index(src, target, after, expected):
    assert "".join(_move(list("abcde"), src, target, after)) == expected
