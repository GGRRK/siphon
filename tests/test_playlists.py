import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from siphon import playlists as playlists_mod
from siphon.library import Song
from siphon.playlists import Playlist, Playlists


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "Music"
    for name in ("Alpha - One.mp3", "sub/Beta - Two.opus"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x")
    return root


def song(path: Path, title: str, artist: str, duration: float) -> Song:
    return Song(path, title, artist, "", duration, None, 0.0, 1)


@pytest.fixture
def known(root) -> dict[Path, Song]:
    return {root / "Alpha - One.mp3": song(root / "Alpha - One.mp3", "One", "Alpha", 61.4),
            root / "sub/Beta - Two.opus": song(root / "sub/Beta - Two.opus", "Two", "", 0)}


@pytest.fixture
def lists(root, known) -> Playlists:
    return Playlists(root / "Playlists", lookup=known.get)


def body(pl: Playlist) -> list[str]:
    return pl.file.read_text(encoding="utf-8").splitlines()


def rel(path: str) -> str:
    """A relative playlist line as Siphon writes it: with backslashes on Windows."""
    return path.replace("/", "\\") if sys.platform == "win32" else path


def test_m3u8_format_relative_inside_root_absolute_outside(tmp_path, root, lists):
    outside = tmp_path / "Elsewhere" / "Gamma - Three.flac"
    pl = lists.create("Road Trip")
    lists.add(pl, [root / "Alpha - One.mp3", root / "sub/Beta - Two.opus", outside])
    assert pl.file == root / "Playlists" / "Road Trip.m3u8"
    assert body(pl) == [
        "#EXTM3U", "#PLAYLIST:Road Trip",
        "#EXTINF:61,Alpha - One", rel("../Alpha - One.mp3"),
        "#EXTINF:-1,Two", rel("../sub/Beta - Two.opus"),
        "#EXTINF:-1,Gamma - Three", str(outside),
    ]
    again = Playlists(root / "Playlists").all()
    assert [(p.name, p.file, p.paths) for p in again] == [(pl.name, pl.file, pl.paths)]


def test_mpv_reads_our_playlists(tmp_path, root, lists):
    mpv = pytest.importorskip("mpv")
    pl = lists.create("Check")
    lists.add(pl, [root / "sub/Beta - Two.opus", root / "Alpha - One.mp3"])
    # Silent by construction: no audio track selected and a null output.
    player = mpv.MPV(ao="null", vo="null", aid="no", pause=True, idle=True, ytdl=False,
                     config=False)
    try:
        player.command("loadlist", str(pl.file), "replace")
        for _ in range(100):
            entries = player.playlist
            if len(entries) == 2:
                break
            time.sleep(0.02)
    finally:
        player.terminate()
    assert [Path(os.path.normpath(e["filename"])) for e in entries] == pl.paths
    assert [e.get("title") for e in entries] == ["Two", "Alpha - One"]


def test_create_makes_unique_names(root, lists):
    names = [lists.create(n).name for n in ("Mix", "Mix", "mix", "  Mix \n", "", "a/b", "..hidden")]
    assert names == ["Mix", "Mix 2", "mix 3", "Mix 4", "Playlist", "a/b", "..hidden"]
    files = sorted(p.file.name for p in lists.all())
    assert files == ["Mix 2.m3u8", "Mix 4.m3u8", "Mix.m3u8", "Playlist.m3u8", "a-b.m3u8",
                     "hidden.m3u8", "mix 3.m3u8"]
    assert [p.name for p in lists.all()] == ["..hidden", "a/b", "Mix", "Mix 2", "mix 3", "Mix 4",
                                            "Playlist"]


def test_names_are_unique_against_names_other_players_wrote(root, lists):
    folder = root / "Playlists"
    folder.mkdir()
    (folder / "export-01.m3u8").write_text("#EXTM3U\n#PLAYLIST:Chill\n")
    assert lists.create("chill").name == "chill 2"


def test_remove_and_move_persist(root, lists):
    a, b = root / "Alpha - One.mp3", root / "sub/Beta - Two.opus"
    pl = lists.create("Order")
    lists.add(pl, [a, b, a])
    lists.move(pl, 0, 2)
    assert pl.paths == [b, a, a]
    lists.move(pl, 2, 0)
    assert pl.paths == [a, b, a]
    lists.remove(pl, 1)
    assert pl.paths == [a, a]
    assert lists.all()[0].paths == [a, a]
    with pytest.raises(IndexError):
        lists.remove(pl, 5)


def test_rename_moves_the_file_and_keeps_the_songs(root, lists):
    a = root / "Alpha - One.mp3"
    pl = lists.create("Old")
    lists.add(pl, [a])
    other = lists.create("Taken")
    old_file = pl.file
    lists.rename(pl, "Fresh")
    assert (pl.name, pl.file.name) == ("Fresh", "Fresh.m3u8")
    assert not old_file.exists()
    assert [(p.name, p.paths) for p in lists.all()] == [("Fresh", [a]), ("Taken", [])]
    lists.rename(pl, "taken")
    assert pl.name == "taken 2"
    lists.rename(pl, "  ")
    lists.rename(other, "Taken")
    assert sorted(p.name for p in lists.all()) == ["Taken", "taken 2"]


def test_delete_trashes_the_file(root, lists, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(playlists_mod, "trash_file", lambda p: p.rename(bin_dir / p.name))
    pl = lists.create("Doomed")
    lists.delete(pl)
    assert lists.all() == []
    assert (bin_dir / "Doomed.m3u8").exists()


def test_drop_path_removes_a_song_everywhere(root, lists):
    a, b = root / "Alpha - One.mp3", root / "sub/Beta - Two.opus"
    one, two, untouched = lists.create("One"), lists.create("Two"), lists.create("Three")
    lists.add(one, [a, b, a])
    lists.add(two, [a])
    lists.add(untouched, [b])
    stamp = untouched.file.stat().st_mtime_ns
    lists.drop_path(root / "sub" / ".." / "Alpha - One.mp3")
    assert {p.name: p.paths for p in lists.all()} == {"One": [b], "Two": [], "Three": [b]}
    assert untouched.file.stat().st_mtime_ns == stamp


def test_moved_songs_stay_listed_and_flagged_missing(root, lists):
    a, b = root / "Alpha - One.mp3", root / "sub/Beta - Two.opus"
    pl = lists.create("Moves")
    lists.add(pl, [a, b])
    a.rename(root / "Renamed.mp3")
    plain = Playlists(root / "Playlists")  # no library lookup: labels come from the file
    reread = plain.all()[0]
    assert reread.paths == [a, b]
    assert reread.missing() == {a}
    plain.add(reread, [b])  # re-saving keeps the gone song and its old label
    assert body(reread)[2:4] == ["#EXTINF:61,Alpha - One", rel("../Alpha - One.mp3")]


def test_reads_files_written_by_other_players(root, lists):
    folder = root / "Playlists"
    folder.mkdir()
    a = root / "Alpha - One.mp3"
    text = ("﻿#EXTM3U\r\n# a comment\r\n"
            '#EXTINF:12 tvg-id="x",Someone - Something\r\n'
            "../Alpha%20One.mp3\r\n"
            f"{a.as_uri()}\r\n"
            "https://example.com/stream.mp3\r\n\r\n"
            "../sub/Beta - Two.opus\r\n")
    (folder / "From VLC.m3u8").write_text(text, encoding="utf-8")
    (folder / "garbage.m3u8").write_bytes(b"\x00\xff\xfe\x80 not a playlist \x81")
    (folder / ".hidden.m3u8").write_text("#EXTM3U\n")
    (folder / "notes.txt").write_text("x")
    unreadable = folder / "locked.m3u8"
    unreadable.write_text("#EXTM3U\n")
    unreadable.chmod(0)  # unreadable on Linux; Windows has no such permission, so it is left out there
    try:
        by_name = {p.name: p for p in lists.all() if p.name != "locked" or os.name == "posix"}
    finally:
        unreadable.chmod(0o644)
    assert sorted(by_name) == ["From VLC", "garbage"]
    vlc = by_name["From VLC"]
    assert vlc.paths == [root / "Alpha%20One.mp3", a, root / "sub/Beta - Two.opus"]
    lists.add(vlc, [])
    assert body(vlc)[2] == "#EXTINF:12,Someone - Something"


def test_missing_folder_means_no_playlists(tmp_path):
    assert Playlists(tmp_path / "nowhere" / "Playlists").all() == []


def test_saves_are_atomic(root, lists):
    pl = lists.create("Atomic")
    for _ in range(3):
        lists.add(pl, [root / "Alpha - One.mp3"])
    assert [f.name for f in (root / "Playlists").iterdir()] == ["Atomic.m3u8"]


def test_labels_come_from_tags_without_a_lookup(tmp_path, root):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = root / "tagged.opus"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", "-metadata", "title=Tagged",
                    "-metadata", "artist=Tagger", str(path)], check=True)
    lists = Playlists(root / "Playlists")
    pl = lists.create("Tags")
    lists.add(pl, [path, root / "Alpha - One.mp3"])
    assert body(pl)[2:] == ["#EXTINF:2,Tagger - Tagged", rel("../tagged.opus"),
                            "#EXTINF:-1,Alpha - One", rel("../Alpha - One.mp3")]
