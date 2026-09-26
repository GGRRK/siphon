"""The window's shared music state (siphon.ui.music.Music) against the offline fakes."""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

import siphon.ui  # noqa: F401  (pins GTK 4 before anything imports Gtk)
from siphon.ui.library_page import sort_songs
from siphon.ui.music import Music

from gi.repository import GLib  # noqa: E402

TESTS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"siphon_test_{name}", TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fake = _load("fake_library")
fake_player = _load("fake_player")


def run_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        if not context.iteration(False):
            time.sleep(0.005)


@pytest.fixture(autouse=True)
def quick_fakes(monkeypatch, tmp_path):
    monkeypatch.setenv("SIPHON_FAKE_SCAN_SECONDS", "0")
    monkeypatch.setenv("SIPHON_FAKE_PLAYLISTS", "0")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def make_music(root: Path) -> tuple[Music, list[str]]:
    music = Music(fake.Library(root), fake.Playlists, fake_player.Player(), fake.cover_file, fake.Song)
    events: list[str] = []
    for name in ("library-changed", "playlists-changed"):
        music.connect(name, lambda _m, name=name: events.append(name))
    music.connect("error", lambda _m, message: events.append(f"error: {message}"))
    return music, events


def touch(path: Path, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_scan_fills_the_snapshot_newest_first(tmp_path):
    touch(tmp_path / "A - Old.mp3", age=100)
    touch(tmp_path / "B - New.opus")
    touch(tmp_path / ".siphon-work" / "C - Partial.mp3")
    music, events = make_music(tmp_path)
    music.rescan()
    assert music.scanning
    run_until(lambda: not music.scanning)
    assert [s.title for s in music.songs] == ["New", "Old"]
    assert not music.scanning
    assert events[-1] == "library-changed"


def test_folder_change_during_a_scan_discards_the_old_result(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    touch(old / "A - Old Song.mp3")
    touch(new / "B - New Song.mp3")
    music, _events = make_music(old)
    music.rescan()
    music.set_root(new)
    assert music.root == new
    run_until(lambda: not music.scanning)
    assert [s.title for s in music.songs] == ["New Song"]
    assert music.library.root == new


def test_download_during_a_scan_waits_for_it_then_shows(tmp_path):
    touch(tmp_path / "A - Before.mp3", age=50)
    music, _events = make_music(tmp_path)
    music.rescan()
    arrived = touch(tmp_path / "B - Downloaded.mp3")
    music.add_file(arrived)
    run_until(lambda: not music.scanning)
    assert music.songs[0].title == "Downloaded"
    assert len(music.songs) == 2


def test_download_outside_the_music_folder_is_ignored(tmp_path):
    music, events = make_music(tmp_path / "music")
    elsewhere = touch(tmp_path / "elsewhere" / "A - Stray.mp3")
    music.add_file(elsewhere)
    assert music.songs == [] and "library-changed" not in events


def test_trash_removes_the_song_everywhere(tmp_path):
    keep = touch(tmp_path / "A - Keep.mp3", age=10)
    gone = touch(tmp_path / "B - Gone.mp3")
    music, events = make_music(tmp_path)
    music.rescan()
    run_until(lambda: not music.scanning)
    playlist = music.change_playlists(music.playlists.create, "Mix")
    music.change_playlists(music.playlists.add, playlist, [gone, keep, gone])
    events.clear()
    assert music.trash(music.song_at(gone))
    assert [s.title for s in music.songs] == ["Keep"]
    assert music.playlists.all()[0].paths == [keep]
    assert {"library-changed", "playlists-changed"} <= set(events)
    assert not gone.exists()


def test_a_failed_playlist_save_becomes_an_error_signal(tmp_path):
    music, events = make_music(tmp_path)

    def full_disk(*_args):
        raise OSError(28, "No space left on device")

    assert music.change_playlists(full_disk) is None
    assert events == ["error: Could not save the playlist (No space left on device).", "playlists-changed"]


def test_entries_keep_missing_files_and_songs_outside_the_library(tmp_path):
    here = touch(tmp_path / "music" / "A - Here.mp3")
    outside = touch(tmp_path / "other" / "Some Artist - Outside.mp3")
    music, _events = make_music(tmp_path / "music")
    music.rescan()
    run_until(lambda: not music.scanning)
    playlist = music.playlists.create("Mix")
    playlist.paths = [here, tmp_path / "music" / "Z - Deleted.mp3", outside]
    entries = music.entries(playlist)
    assert [(song.title, missing) for song, missing in entries] == [
        ("Here", False), ("Deleted", True), ("Outside", False)]
    assert entries[2][0].artist == "Some Artist"
    assert music.find_playlist(playlist.file) is playlist


def _song(title: str, artist: str = "", album: str = "", track: int | None = None, added: float = 0.0):
    return fake.Song(Path(f"/m/{title}.mp3"), title, artist, album, 1.0, track, added, 0)


def test_sort_orders():
    songs = [_song("beta", "Zed", added=3), _song("Álpha", "", added=2), _song("gamma", "abba", "B", 2, added=1),
             _song("delta", "Abba", "B", 1, added=0)]
    assert [s.title for s in sort_songs(songs, "added")] == ["beta", "Álpha", "gamma", "delta"]
    assert [s.title for s in sort_songs(songs, "title")] == ["Álpha", "beta", "delta", "gamma"]
    # By artist, then album and track number; songs without an artist last.
    assert [s.title for s in sort_songs(songs, "artist")] == ["delta", "gamma", "beta", "Álpha"]
