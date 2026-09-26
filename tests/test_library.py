import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mutagen.id3 import TALB, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

from siphon import library
from siphon.library import Library, Song
from siphon.playlists import Playlists

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
TAGS = {"title": "Tune", "artist": "Ärtist", "album": "Albüm", "track": "3/9"}


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict[str, Path]:
    """One second of sine per format: tagged where the container has tags,
    named "Some Artist - Some Title" where it doesn't (aac, webm)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("samples")
    files = {}
    for ext in ("mp3", "m4a", "opus", "ogg", "flac", "wav", "aac", "webm"):
        tagged = ext not in ("aac", "webm", "wav")
        path = out / (f"{ext}.{ext}" if tagged or ext == "wav" else f"Some Artist - Some Title.{ext}")
        args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=1", "-ac", "1"]
        if ext == "ogg":
            args += ["-c:a", "libvorbis"]
        for key, value in TAGS.items() if tagged else ():
            args += ["-metadata", f"{key}={value}"]
        subprocess.run([*args, str(path)], check=True)
        files[ext] = path
    wav = WAVE(files["wav"])  # ffmpeg writes no ID3 chunk: tag it the way other taggers do
    wav.add_tags()
    for frame, text in ((TIT2, "Tune"), (TPE1, "Ärtist"), (TALB, "Albüm"), (TRCK, "3/9")):
        wav.tags.add(frame(encoding=3, text=text))
    wav.save()
    return files


def junk(path: Path, mtime: float | None = None) -> Path:
    """A file with an audio name and no audio: tags come from its name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really audio " + path.name.encode())
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def titles(songs: list[Song]) -> list[str]:
    return [s.title for s in songs]


def make(tmp_path) -> tuple[Path, Library]:
    root = tmp_path / "Music"
    root.mkdir(exist_ok=True)
    return root, Library(root, tmp_path / "cache" / "library.json")


def test_reads_tags_and_length_of_every_format(tmp_path, samples):
    root, lib = make(tmp_path)
    for path in samples.values():
        shutil.copy2(path, root / path.name)
    songs = {s.path.suffix[1:]: s for s in lib.scan()}
    assert sorted(songs) == sorted(samples)
    for ext in ("mp3", "m4a", "opus", "ogg", "flac", "wav"):
        s = songs[ext]
        assert (s.title, s.artist, s.album, s.track_no) == ("Tune", "Ärtist", "Albüm", 3), ext
        assert 0.9 < s.duration < 1.2, ext
    for ext in ("aac", "webm"):
        assert (songs[ext].title, songs[ext].artist) == ("Some Title", "Some Artist"), ext
    assert 0.9 < songs["aac"].duration < 1.2
    assert songs["webm"].duration == 0  # mutagen can't read Matroska; mpv reports it at play time
    assert songs["mp3"].size == (root / "mp3.mp3").stat().st_size


def test_name_fallbacks_and_damaged_files(tmp_path, samples):
    root, lib = make(tmp_path)
    junk(root / "Broken Band - Broken Song.mp3")
    junk(root / "just a stem.opus")
    junk(root / " - dash first.flac")
    shutil.copy2(samples["webm"], root / "Plain.webm")
    by_title = {s.title: s for s in lib.scan()}
    assert by_title["Broken Song"].artist == "Broken Band"
    assert by_title["Broken Song"].duration == 0
    assert by_title["just a stem"].display_artist == "Unknown artist"
    assert by_title["- dash first"].artist == ""
    assert by_title["Plain"].artist == ""


def test_title_tag_without_artist_takes_artist_from_siphon_name(tmp_path, samples):
    root, lib = make(tmp_path)
    for name, title in (("The Band - Tune.m4a", "Tune"), ("Other - Thing.m4a", "Different")):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(samples["m4a"]),
                        "-map_metadata", "-1", "-metadata", f"title={title}", "-c", "copy",
                        str(root / name)], check=True)
    by_title = {s.title: s for s in lib.scan()}
    assert by_title["Tune"].artist == "The Band"
    assert by_title["Different"].artist == ""  # the name doesn't match the title: don't guess


def test_skips_hidden_work_dirs_playlists_and_other_files(tmp_path):
    root, lib = make(tmp_path)
    junk(root / ".siphon-abc123" / "Partial - Download.mp3")
    junk(root / ".hidden.mp3")
    junk(root / "Playlists" / "Not - Library.mp3")
    junk(root / "Albums" / "Playlists" / "Nested - Kept.mp3")
    junk(root / "A" / "B" / "Deep - Song.OPUS")
    (root / "notes.txt").write_text("x")
    (root / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    assert sorted(titles(lib.scan())) == ["Kept", "Song"]


def test_newest_first(tmp_path):
    root, lib = make(tmp_path)
    junk(root / "A - Old.mp3", 1_000_000)
    junk(root / "A - New.mp3", 3_000_000)
    junk(root / "A - Mid.mp3", 2_000_000)
    assert titles(lib.scan()) == ["New", "Mid", "Old"]
    assert lib.songs()[0].added == 3_000_000


def test_symlinks_never_lead_out_of_the_root(tmp_path):
    root, lib = make(tmp_path)
    outside = tmp_path / "Elsewhere"
    junk(outside / "Out - Outside.mp3")
    junk(root / "Real" / "In - Inside.mp3")
    (root / "link-out").symlink_to(outside)
    (root / "file-out.mp3").symlink_to(outside / "Out - Outside.mp3")
    (root / "link-in").symlink_to(root / "Real")
    (root / "loop").symlink_to(root)
    (root / "dangling.mp3").symlink_to(root / "gone.mp3")
    assert titles(lib.scan()) == ["Inside"]
    assert lib.songs()[0].path.parent.name in ("Real", "link-in")


def test_rescan_only_stats_unchanged_files(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    for i in range(5):
        junk(root / f"A - Song {i}.mp3", 1_000_000 + i)
    first = lib.scan()
    reads = []
    real_read = library._read_song
    monkeypatch.setattr(library, "_read_song", lambda *a: reads.append(a[0]) or real_read(*a))
    cache_file = tmp_path / "cache" / "library.json"
    before = cache_file.stat().st_mtime_ns
    assert lib.scan() == first
    assert reads == []
    assert cache_file.stat().st_mtime_ns == before  # nothing changed: no rewrite
    changed = junk(root / "A - Song 2.mp3", 5_000_000)
    assert lib.scan()[0].title == "Song 2"
    assert reads == [changed]


def test_restart_shows_the_library_before_the_first_scan(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    junk(root / "A - Kept.mp3", 1_000_000)
    lib.scan()
    monkeypatch.setattr(library, "_read_song", lambda *a: pytest.fail("tags re-read"))
    again = Library(root, tmp_path / "cache" / "library.json")
    assert titles(again.songs()) == ["Kept"]
    assert titles(again.scan()) == ["Kept"]
    elsewhere = Library(tmp_path / "Other", tmp_path / "cache" / "library.json")
    assert elsewhere.songs() == []


def test_cache_is_versioned_atomic_and_survives_garbage(tmp_path):
    root, lib = make(tmp_path)
    junk(root / "A - Song.mp3")
    lib.scan()
    cache_file = tmp_path / "cache" / "library.json"
    data = json.loads(cache_file.read_text())
    assert data["version"] == library.CACHE_VERSION
    assert list(data["songs"]) == [str(root / "A - Song.mp3")]
    assert [p.name for p in cache_file.parent.iterdir()] == ["library.json"]  # no temp files left
    for text in ("{broken", '{"version": 999, "songs": {}}', '{"version": 1, "songs": {"x": [1]}}', "[]"):
        cache_file.write_text(text)
        fresh = Library(root, cache_file)
        assert fresh.songs() == []
        assert titles(fresh.scan()) == ["Song"]


def test_add_and_forget_persist(tmp_path):
    root, lib = make(tmp_path)
    lib.scan()
    song = lib.add(junk(root / "New - Download.mp3"))
    assert song is not None and lib.get(root / "New - Download.mp3") == song
    assert lib.get(root / "sub" / ".." / "New - Download.mp3") == song
    assert lib.add(junk(root / ".siphon-x" / "A - B.mp3")) is None
    assert lib.add(junk(root / "Playlists" / "A - B.mp3")) is None
    assert lib.add(junk(tmp_path / "Outside - Root.mp3")) is None
    assert lib.add(root / "Not - There.mp3") is None
    (root / "readme.txt").write_text("x")
    assert lib.add(root / "readme.txt") is None
    assert titles(Library(root, tmp_path / "cache" / "library.json").songs()) == ["Download"]
    lib.forget(song.path)
    assert lib.songs() == [] and lib.get(song.path) is None
    assert Library(root, tmp_path / "cache" / "library.json").songs() == []


def test_add_and_forget_during_a_scan_win_over_the_walk(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    old = junk(root / "A - Old.mp3")
    real_walk = library._walk

    def walk_then_race(top):
        yield from real_walk(top)
        # The walk is over; a download finishes and a song is deleted before the swap.
        lib.add(junk(root / "A - Fresh.mp3"))
        lib.forget(old)

    monkeypatch.setattr(library, "_walk", walk_then_race)
    assert titles(lib.scan()) == ["Fresh"]
    monkeypatch.setattr(library, "_walk", real_walk)
    old.unlink()
    assert titles(lib.scan()) == ["Fresh"]


def test_set_root_during_a_scan_discards_the_old_scan(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    junk(root / "A - Old root.mp3")
    other = tmp_path / "Other"
    junk(other / "A - New root.mp3")
    real_walk = library._walk

    def walk_then_switch(top):
        yield from real_walk(top)
        lib.set_root(other)

    monkeypatch.setattr(library, "_walk", walk_then_switch)
    assert lib.scan() == []
    assert lib.root == other
    monkeypatch.setattr(library, "_walk", real_walk)
    assert titles(lib.scan()) == ["New root"]


def test_songs_returns_copies(tmp_path):
    root, lib = make(tmp_path)
    junk(root / "A - Song.mp3")
    lib.scan()
    lib.songs().clear()
    assert len(lib.songs()) == 1


def test_search_is_case_and_accent_insensitive_and_needs_every_word(tmp_path):
    root, lib = make(tmp_path)
    junk(root / "Beyoncé - Halo.mp3")
    junk(root / "Sigur Rós - Hoppípolla.mp3")
    junk(root / "Halo Band - Other.mp3")
    lib.scan()
    assert sorted(titles(lib.search("BEYONCE halo"))) == ["Halo"]
    assert sorted(titles(lib.search("hoppipolla"))) == ["Hoppípolla"]
    assert sorted(titles(lib.search("sigur RÓS"))) == ["Hoppípolla"]
    assert sorted(titles(lib.search("béyoncé"))) == ["Halo"]
    assert sorted(titles(lib.search("halo"))) == ["Halo", "Other"]
    assert lib.search("halo nope") == []
    assert len(lib.search("  ")) == 3
    subset = [s for s in lib.songs() if s.title == "Other"]
    assert titles(lib.search("halo", subset)) == ["Other"]


def test_trash_forgets_the_song_and_drops_it_from_playlists(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    keep, gone = junk(root / "A - Keep.mp3"), junk(root / "A - Gone.mp3")
    lib.scan()
    playlists = Playlists(root / "Playlists")
    pl = playlists.create("Mix")
    playlists.add(pl, [gone, keep, gone])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(library, "trash_file", lambda p: p.rename(bin_dir / p.name))
    lib.trash(lib.get(gone), playlists)
    assert (bin_dir / gone.name).exists() and not gone.exists()
    assert titles(lib.songs()) == ["Keep"]
    assert playlists.all()[0].paths == [keep]


def test_failed_trash_keeps_the_song(tmp_path, monkeypatch):
    root, lib = make(tmp_path)
    path = junk(root / "A - Song.mp3")
    lib.scan()

    def refuse(p):
        raise OSError("no trash here")

    monkeypatch.setattr(library, "trash_file", refuse)
    with pytest.raises(OSError):
        lib.trash(lib.get(path))
    assert titles(lib.songs()) == ["Song"]


def test_trash_file_of_a_missing_file_is_not_an_error(tmp_path):
    # A subprocess, so GLib reads the private XDG dirs fresh; a missing file never touches a trash.
    code = "import sys; from pathlib import Path; from siphon.library import trash_file; " \
           "trash_file(Path(sys.argv[1]))"
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"), "GIO_USE_VFS": "local"}
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "missing.mp3")], check=True,
                   env=env, cwd=Path(__file__).resolve().parent.parent)
