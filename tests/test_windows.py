"""Windows behaviour, checked on any system: sys.platform is switched to win32 and Windows paths are
compared as PureWindowsPath, so these run on the Linux dev machine and in Windows CI alike."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import siphon
from siphon import core, engine, library, names, paths, playlists as playlists_mod
from siphon.core import SiphonError, Track
from siphon.library import Library, Song
from siphon.playlists import Playlists
from siphon.ui.fmt import pretty_path

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


def same_windows_path(path: Path | str, expected: str) -> bool:
    return PureWindowsPath(str(path)) == PureWindowsPath(expected)


# ---------------------------------------------------------------- folders


def test_windows_folders_come_from_appdata(win, monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\Me\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Me\AppData\Local")
    assert same_windows_path(paths.config_dir(), r"C:\Users\Me\AppData\Roaming\Siphon")
    assert same_windows_path(paths.cache_dir(), r"C:\Users\Me\AppData\Local\Siphon\cache")
    assert same_windows_path(paths.data_dir(), r"C:\Users\Me\AppData\Local\Siphon")


def test_windows_folders_without_appdata_fall_back_to_the_profile(win, monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Me")
    assert same_windows_path(paths.config_dir(), r"C:\Users\Me\AppData\Roaming\Siphon")
    assert same_windows_path(paths.data_dir(), r"C:\Users\Me\AppData\Local\Siphon")


def test_windows_music_is_the_known_folder_or_the_profile(win, monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Me")
    monkeypatch.setattr(paths, "_known_music_folder", lambda: r"D:\OneDrive\Music")
    assert same_windows_path(paths.music_dir(), r"D:\OneDrive\Music")
    monkeypatch.setattr(paths, "_known_music_folder", lambda: "")
    assert same_windows_path(paths.music_dir(), r"C:\Users\Me\Music")


@pytest.mark.linux
def test_linux_folders_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.data_dir() == tmp_path / "data" / "siphon"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert paths.data_dir() == Path.home() / ".local" / "share" / "siphon"
    assert paths.cache_dir() == tmp_path / "xdg" / "XDG_CACHE_HOME" / "siphon"
    assert paths.no_window() == 0


def test_bundle_dir(tmp_path, monkeypatch):
    assert paths.bundle_dir() is None
    monkeypatch.setenv("SIPHON_BUNDLE", str(tmp_path))
    assert paths.bundle_dir() == tmp_path
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "Siphon.exe"))
    assert paths.bundle_dir() == tmp_path / "app"


def test_setup_environment_puts_the_bundled_tools_first(win, tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    dll_dirs, activated = [], []
    monkeypatch.setenv("SIPHON_BUNDLE", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(os, "add_dll_directory", dll_dirs.append, raising=False)
    monkeypatch.setattr(engine, "activate", lambda: activated.append(True))
    paths.setup_environment()
    assert os.environ["PATH"] == os.pathsep.join([str(tmp_path / "bin"), "/usr/bin"])
    assert dll_dirs == [str(tmp_path / "bin")] and activated == [True]
    assert paths.no_window() == 0x08000000


def test_setup_environment_from_source_changes_nothing(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(engine, "activate", lambda: pytest.fail("activated from source"))
    paths.setup_environment()
    assert os.environ["PATH"] == "/usr/bin"


def test_pretty_path_shows_windows_folders_in_full(win):
    assert pretty_path(Path.home() / "Music") == str(Path.home() / "Music")


# ---------------------------------------------------------------- names


def test_windows_names_lose_forbidden_characters(win):
    assert core.safe_name('AC/DC: Live at "Donington" <1990>?') == "AC-DC - Live at 'Donington' 1990"
    assert core.safe_name(r"Back\Slash | Pipe * Star") == "Back-Slash - Pipe Star"
    assert core.safe_name("10:30 Trailing dots. . .") == "10-30 Trailing dots"
    assert core.safe_name("a\x01b\x7f") == "ab"


@pytest.mark.parametrize("name", ["CON", "con", "Nul", "PRN", "aux", "COM1", "lpt9", "COM¹", "NUL.txt",
                                  "Con.Air", "CON .x", "..CON"])
def test_windows_reserved_names_get_a_prefix(win, name):
    assert core.safe_name(name).startswith("_")
    assert names.unreserved(name.lstrip(".")) == "_" + name.lstrip(".")


@pytest.mark.parametrize("name", ["Con Air", "CONSOLE", "COM0", "COM10", "Nullify", "LPT"])
def test_windows_ordinary_names_stay(win, name):
    assert core.safe_name(name) == name


def test_windows_names_keep_the_byte_limit(win):
    stem = core.file_stem(Track(url="", title="é" * 300, artist="CON"))
    assert len(stem.encode()) <= core.NAME_LIMIT - len(".flac") and stem.startswith("CON - é")
    assert core.file_stem(Track(url="", title="NUL")) == "_NUL"


@pytest.mark.linux
def test_linux_names_are_unchanged():
    assert core.safe_name('a:b?"c"<d>|e*f\\g') == 'a:b?"c"<d>|e*f\\g'
    assert core.safe_name("CON") == "CON"


def test_shorten_counts_utf16_units():
    assert names.shorten("😀" * 10, 5) == "😀😀"
    assert names.shorten("abc. def", 5) == "abc"


def test_windows_long_folders_shorten_the_file_name(win):
    outdir = Path("C:/" + "d" * 150)
    stem = core._fit("s" * 175, outdir)
    assert len(str(outdir)) + 1 + len(stem) + len(".flac") == names.MAX_PATH
    with pytest.raises(SiphonError, match="too long for Windows"):
        core._fit("song", Path("C:/" + "d" * 230))


@pytest.mark.linux
def test_linux_long_folders_keep_the_name():
    assert core._fit("s" * 175, Path("/" + "d" * 4000)) == "s" * 175


# ---------------------------------------------------------------- JavaScript runtime


@pytest.fixture
def on_path(monkeypatch):
    """Pretend only the given executables are installed."""
    def install(*found: str) -> None:
        monkeypatch.setattr(shutil, "which", lambda exe, path=None: f"/opt/{exe}" if exe in found else None)
    return install


@pytest.mark.parametrize("found, expected", [
    (("node", "deno", "qjs"), ("node", "/opt/node")),
    (("deno", "qjs"), ("deno", "/opt/deno")),
    (("qjs",), ("quickjs", "/opt/qjs")),
    ((), None),
])
def test_js_runtime_preference(on_path, found, expected):
    on_path(*found)
    assert core.js_runtime() == expected


def test_a_packaged_build_prefers_its_own_runtime(tmp_path, monkeypatch):
    system, bundle = tmp_path / "system", tmp_path / "Siphon"
    suffix = ".exe" if os.name == "nt" else ""
    for exe in (system / f"node{suffix}", bundle / "bin" / f"qjs{suffix}"):
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(system))
    assert core.js_runtime()[0] == "node"
    monkeypatch.setenv("SIPHON_BUNDLE", str(bundle))
    name, found = core.js_runtime()
    assert name == "quickjs" and Path(found).parent == bundle / "bin"


def test_ytdlp_gets_the_runtime_that_exists(on_path):
    on_path("deno")
    with core._ydl({}) as ydl:
        assert ydl.params["js_runtimes"] == {"deno": {"path": "/opt/deno"}}
    on_path()
    with core._ydl({}) as ydl:
        assert ydl.params["js_runtimes"] == {}


def test_youtube_without_a_runtime_fails_clearly(on_path):
    on_path()
    with pytest.raises(SiphonError, match="YouTube needs Node.js or Deno"):
        core.resolve("https://youtu.be/jNQXAC9IVRw")
    with pytest.raises(SiphonError, match="YouTube needs Node.js or Deno"):
        core.download(Track(url="https://www.deezer.com/track/1", title="T", query="A T"), Path("/nonexistent"), "opus")
    assert core._is_youtube("https://music.youtube.com/watch?v=x") and core._is_youtube("https://youtu.be/x")
    assert not core._is_youtube("https://soundcloud.com/someone/song")  # other sites need no runtime
    assert not core._is_youtube("https://notyoutube.com/watch?v=x")


# ---------------------------------------------------------------- library


@pytest.fixture
def music(tmp_path) -> Path:
    root = tmp_path / "Music"
    for name in ("Alpha - One.mp3", "Sub/Beta - Two.opus", "playlists/Stray - Song.mp3"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x")
    return root


def test_windows_library_ignores_case(win, music, tmp_path):
    lib = Library(music, cache_file=tmp_path / "library.json")
    titles = {s.title for s in lib.scan()}
    assert titles == {"One", "Two"}  # "playlists" is the Playlists folder, whatever its case
    one = music / "Alpha - One.mp3"
    assert lib.get(Path(str(one).upper())).path == one  # found by any case, shown as it is on disk
    reloaded = Library(music, cache_file=tmp_path / "library.json")
    assert reloaded.get(Path(str(one).lower())).path == one
    reloaded.forget(Path(str(one).swapcase()))
    assert reloaded.get(one) is None


@pytest.mark.linux
def test_linux_library_keeps_case(music, tmp_path):
    lib = Library(music, cache_file=tmp_path / "library.json")
    assert {s.title for s in lib.scan()} == {"One", "Two", "Song"}
    assert lib.get(Path(str(music / "Alpha - One.mp3").upper())) is None


def test_windows_hidden_flag():
    entry = lambda attributes: SimpleNamespace(stat=lambda follow_symlinks: SimpleNamespace(st_file_attributes=attributes))
    assert library._hidden(entry(0x2 | 0x20)) and not library._hidden(entry(0x20))
    assert not library._hidden(SimpleNamespace(stat=lambda follow_symlinks: os.stat_result((0,) * 10)))


def test_windows_walk_skips_hidden_entries(win, music, monkeypatch):
    monkeypatch.setattr(library, "_hidden", lambda entry: entry.name == "Sub")
    assert {Path(p).name for p, _ in library._walk(music)} == {"Alpha - One.mp3"}


@pytest.mark.linux
def test_linux_inside_checks():
    assert library._inside("/music/a.mp3", "/music") and not library._inside("/musicx/a.mp3", "/music")


def test_windows_inside_ignores_case(win):
    assert library._inside(r"C:\Music\Sub\a.mp3", r"c:\music")
    assert library._inside(r"D:\x.mp3", "D:\\")
    assert not library._inside(r"C:\MusicOther\a.mp3", r"C:\Music")


# ---------------------------------------------------------------- playlists


def test_windows_playlists_are_written_with_backslashes(win, music, monkeypatch):
    lists = Playlists(music / "Playlists")
    pl = lists.create("Mix")
    lists.add(pl, [music / "Sub" / "Beta - Two.opus"])
    assert pl.file.read_text(encoding="utf-8").splitlines()[-1] == r"..\Sub\Beta - Two.opus"
    monkeypatch.setattr(sys, "platform", "linux")  # the same music folder, read from Linux
    assert Playlists(music / "Playlists").all()[0].paths == [music / "Sub" / "Beta - Two.opus"]


def test_windows_playlist_pictures_are_named_with_backslashes(win, music, monkeypatch):
    art = music / "Sub" / "cover.jpg"
    art.write_bytes(b"\xff\xd8\xff" + b"x" * 8)
    lists = Playlists(music / "Playlists")
    pl = lists.create("Mix", source="https://www.youtube.com/playlist?list=PLx")
    pl.cover = art  # a picture another player chose
    lists.add(pl, [])
    assert pl.file.read_text(encoding="utf-8").splitlines()[2:4] == [
        r"#EXTIMG:..\Sub\cover.jpg", "#SIPHON-SOURCE:https://www.youtube.com/playlist?list=PLx"]
    monkeypatch.setattr(sys, "platform", "linux")  # the same music folder, read from Linux
    assert Playlists(music / "Playlists").all()[0].cover == art


def test_windows_picture_names_stay_under_max_path(win, tmp_path):
    lists = Playlists(tmp_path / "Playlists")
    lists.folder.mkdir()
    stem = "x" * (names.room(lists.folder) - len(".m3u8") - 14)  # the longest name a playlist gets
    pl = lists.create(stem)
    assert pl.file.stem == stem
    (lists.folder / f"{stem}.jpg").write_bytes(b"unrelated")
    assert not lists.set_cover(pl, b"\xff\xd8\xff" + b"x" * 8)  # "... (2).jpg" would not fit: no picture
    assert pl.cover is None and (lists.folder / f"{stem}.jpg").read_bytes() == b"unrelated"
    (lists.folder / f"{stem}.jpg").unlink()
    assert lists.set_cover(pl, b"\xff\xd8\xff" + b"x" * 8) and pl.cover.name == f"{stem}.jpg"


@pytest.mark.linux
def test_linux_keeps_real_backslashes_in_names(music):
    odd = music / "back\\slash.mp3"
    odd.write_bytes(b"x")
    (music / "Playlists").mkdir()
    (music / "Playlists" / "Odd.m3u8").write_text("#EXTM3U\n../back\\slash.mp3\n")
    assert Playlists(music / "Playlists").all()[0].paths == [odd]


@pytest.mark.parametrize("uri, expected", [
    ("file:///C:/Music/a%20b.mp3", "C:/Music/a b.mp3"),
    ("file://localhost/C:/Music/a.mp3", "C:/Music/a.mp3"),
    ("file://C:/Music/a.mp3", "C:/Music/a.mp3"),
    ("file://server/share/a.mp3", "//server/share/a.mp3"),
])
def test_windows_file_uris(win, uri, expected):
    assert same_windows_path(playlists_mod.file_uri_path(uri), expected)


@pytest.mark.linux
def test_linux_file_uris():
    assert playlists_mod.file_uri_path("file:///home/me/a%20b.mp3") == "/home/me/a b.mp3"


def test_windows_playlist_file_names(win):
    assert playlists_mod._file_stem("CON") == "_CON"
    assert playlists_mod._file_stem("Best of: 2026?") == "Best of - 2026"
    root = "C:\\" if os.name == "nt" else "/"  # absolute where the test runs
    lists = Playlists(Path(root + "p" * (121 - len(root))))  # 121 characters: 137 left for a name
    assert len(lists._fit("x" * 150)) == 137 - len(".m3u8") - 14


@pytest.mark.linux
def test_linux_playlist_file_names():
    assert playlists_mod._file_stem("CON") == "CON"
    assert playlists_mod._file_stem("Best of: 2026?") == "Best of: 2026?"


def test_song_key_keeps_the_real_path(win):
    song = Song(Path("C:/Music/Alpha - One.mp3"), "One", "Alpha", "", 1.0, None, 0.0, 1)
    assert song.key == str(song.path)  # UI actions turn the key back into a path: its case must survive


# ---------------------------------------------------------------- entry points

# Stands in for the windowed build (no console, so no sys.stderr) and writes the way Python, GLib and a crash
# would: on Windows through the C runtime's own stdout and stderr, elsewhere through descriptor 2.
_WINDOWED = """
import ctypes, os, sys
sys.stdout = sys.stderr = None
from siphon import logfile
logfile.redirect()
print("python says hello")
if os.name == "nt":
    ucrt = ctypes.CDLL("ucrtbase")
    ucrt.__acrt_iob_func.restype = ctypes.c_void_p
    ucrt.__acrt_iob_func.argtypes = [ctypes.c_uint]
    for stream, text in ((1, b"c stdout says hello\\n"), (2, b"c stderr says hello\\n")):
        ucrt.fputs(ctypes.c_char_p(text), ctypes.c_void_p(ucrt.__acrt_iob_func(stream)))
        ucrt.fflush(ctypes.c_void_p(ucrt.__acrt_iob_func(stream)))
"""


def test_a_windowed_session_writes_everything_to_the_log(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "XDG_DATA_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}
    log = tmp_path / ("Siphon" if os.name == "nt" else "siphon") / "siphon.log"
    log.parent.mkdir()
    log.write_text("the last session\n")
    done = subprocess.run([sys.executable, "-c", _WINDOWED], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    expected = ["python says hello"] + (["c stdout says hello", "c stderr says hello"] if os.name == "nt" else [])
    assert sorted(log.read_text(encoding="utf-8").splitlines()) == sorted(expected)


def test_the_windows_ffmpeg_keeps_the_equalizers_filters():
    """build-av.sh trims ffmpeg for Windows; mpv silently drops a filter libavfilter lacks, so the equalizer
    (ffmpeg's aformat, volume and equalizer filters) would just stop working there. The selftest checks the built
    result."""
    script = (ROOT / "packaging" / "windows" / "build-av.sh").read_text()
    configure = script[script.index("./configure"):script.index("make -j")]
    assert not re.search(r"--disable-(everything|avfilter|filters\b|filter=)", configure)


def test_version_flag():
    done = subprocess.run([sys.executable, "-m", "siphon", "--version"], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})
    lines = done.stdout.splitlines()
    assert done.returncode == 0 and lines[0] == f"Siphon {siphon.__version__}" and lines[1].startswith("yt-dlp 20")


@pytest.mark.skipif(not (shutil.which("ffmpeg") and core.js_runtime()), reason="needs ffmpeg and a JS runtime")
def test_selftest_offline(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "TMPDIR": str(tmp_path), "SIPHON_AO": "null"}
    done = subprocess.run([sys.executable, "-m", "siphon", "selftest"], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "all checks passed" in done.stdout and "ok    playback" in done.stdout
