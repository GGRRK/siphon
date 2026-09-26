"""`siphon selftest [--net]`: prove that the tools and the whole pipeline work on this machine.

It checks ffmpeg, ffprobe, a JavaScript runtime and libmpv, then makes a 2 s tone in a temporary
music folder, tags it, scans it into a Library, round-trips a playlist and plays it silently to the
end. --net also downloads a YouTube music video as Opus, probes the file and has the JavaScript runtime
solve that video's YouTube challenge. Exit status 0 = all good.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__, paths

# Rick Astley's "Never Gonna Give You Up": as permanent as YouTube gets, and unlike "Me at the zoo" it downloads
# from data-centre addresses too (YouTube wants a sign-in for that one there, whichever client asks).
NET_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
NET_SECONDS = 213
PLAY_TIMEOUT = 20  # seconds for the 2 s tone to play out


class Failed(Exception):
    """A check that did not pass; the message says what was wrong."""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="siphon selftest", description="Check that Siphon works on this machine.")
    parser.add_argument("--net", action="store_true", help=f"also download {NET_URL} as Opus")
    args = parser.parse_args(argv)
    os.environ["SIPHON_AO"] = "null"  # the player check must never make a sound

    import yt_dlp

    from .core import engine_version

    print(f"Siphon {__version__}, Python {sys.version.split()[0]} on {sys.platform}")
    print(f"bundle   {paths.bundle_dir() or '(none: running from source)'}")
    print(f"engine   yt-dlp {engine_version()} from {Path(yt_dlp.__file__).parent}")
    passed = all([_check("ffmpeg", lambda: _tool("ffmpeg")),
                  _check("ffprobe", lambda: _tool("ffprobe")),
                  _check("js", _js_runtime),
                  _check("libmpv", _libmpv)])
    with tempfile.TemporaryDirectory(prefix="siphon-selftest-") as tmp:
        passed = passed and _pipeline(Path(tmp), args.net)
    print("all checks passed" if passed else "SELFTEST FAILED")
    return 0 if passed else 1


def _pipeline(tmp: Path, net: bool) -> bool:
    """The steps in order, each needing the one before; stops at the first failure."""
    from .library import Library
    from .playlists import Playlists

    music = tmp / "Music"
    music.mkdir()
    state: dict = {}
    steps: list[tuple[str, Callable[[], str]]] = [
        ("tone", lambda: _tone(music, state)),
        ("tags", lambda: _tags(state)),
        ("library", lambda: _library(Library(music, cache_file=tmp / "library.json"), state)),
        ("playlist", lambda: _playlist(Playlists(music / "Playlists", root=music), music, state)),
        ("playback", lambda: _playback(state)),
    ]
    if net:
        steps += [("download", lambda: _download(music)), ("challenge", _challenge)]
    return all(_check(name, step) for name, step in steps)


def _check(name: str, step: Callable[[], str]) -> bool:
    try:
        detail = step()
    except Failed as e:
        print(f"FAIL  {name:<9} {e}", flush=True)
        return False
    except Exception as e:  # an unexpected error is a failed check too, with its type for the log
        print(f"FAIL  {name:<9} {type(e).__name__}: {e}", flush=True)
        return False
    print(f"ok    {name:<9} {detail}", flush=True)
    return True


def _run(args: list[str], timeout: float = 60) -> str:
    done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                          creationflags=paths.no_window())
    if done.returncode != 0:
        raise Failed(f"{Path(args[0]).name} exited with {done.returncode}: {done.stderr.strip()[-300:]}")
    return done.stdout


# ---------------------------------------------------------------- tools


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise Failed(f"{name} is not on PATH")
    first = _run([found, "-version"]).splitlines()[0]  # "ffmpeg version 7.1.1 Copyright ..."
    return f"{first.split()[2]} ({found})"


def _js_runtime() -> str:
    from .core import NO_JS, js_runtime

    found = js_runtime()
    if found is None:
        raise Failed(NO_JS)
    name, exe = found
    run = {"deno": [exe, "eval"]}.get(name, [exe, "-e"])  # node and qjs both take -e
    if (answer := _run([*run, "console.log(6 * 7)"]).strip()) != "42":
        raise Failed(f"{name} printed {answer[:80]!r} for 6 * 7")
    return f"{name} ({exe})"


def _libmpv() -> str:
    import mpv

    major, minor = mpv._mpv_client_api_version()
    return f"client API {major}.{minor} ({mpv.backend._name})"


# ---------------------------------------------------------------- pipeline


def _tone(music: Path, state: dict) -> str:
    path = music / "Siphon Selftest - Tone.opus"
    _run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
          "-c:a", "libopus", "-b:a", "64k", str(path)])
    state["tone"] = path
    return f"{path.stat().st_size} bytes of Opus"


def _tags(state: dict) -> str:
    import mutagen

    from .core import Track, _tag

    _tag(state["tone"], Track(url="", title="Tone", artist="Siphon Selftest", album="Selftest"), {}, None)
    tags = mutagen.File(state["tone"], easy=True)
    got = (tags.get("title"), tags.get("artist"), tags.get("album"))
    if got != (["Tone"], ["Siphon Selftest"], ["Selftest"]):
        raise Failed(f"read back {got}")
    return "title, artist and album read back"


def _library(library, state: dict) -> str:
    songs = library.scan()
    if len(songs) != 1:
        raise Failed(f"expected 1 song, found {len(songs)}")
    song = songs[0]
    if (song.title, song.artist) != ("Tone", "Siphon Selftest") or not 1.9 <= song.duration <= 2.1:
        raise Failed(f"read {song.artist} - {song.title}, {song.duration:.2f} s")
    if library.get(song.path) != song:
        raise Failed("the song is not found again by its path")
    state["song"] = song
    return f"{song.artist} - {song.title}, {song.duration:.2f} s"


def _playlist(playlists, music: Path, state: dict) -> str:
    song = state["song"]
    playlists.add(playlists.create("Selftest"), [song.path])
    reread = playlists.all()
    if len(reread) != 1 or reread[0].paths != [song.path]:
        raise Failed(f"read back {[(pl.name, pl.paths) for pl in reread]}")
    entry = reread[0].file.read_text(encoding="utf-8").splitlines()[-1]
    return f"{reread[0].file.name} holds {entry!r}"


def _playback(state: dict) -> str:
    from gi.repository import GLib

    from .player import Player

    player, loop, seen = Player(), GLib.MainLoop(), {"top": 0.0, "played": False, "error": ""}

    def on_position(_player, seconds: float) -> None:
        seen["top"] = max(seen["top"], seconds)

    def on_changed(_player) -> None:
        seen["played"] = seen["played"] or player.state == "playing"
        if seen["played"] and player.state == "stopped":
            loop.quit()  # the queue ran out: end of file

    def on_error(_player, message: str) -> None:
        seen["error"] = message
        loop.quit()

    def on_timeout() -> bool:
        seen["error"] = f"still {player.state} at {seen['top']:.1f} s after {PLAY_TIMEOUT} s"
        loop.quit()
        return GLib.SOURCE_REMOVE

    player.connect("position", on_position)
    player.connect("changed", on_changed)
    player.connect("error", on_error)
    timer = GLib.timeout_add_seconds(PLAY_TIMEOUT, on_timeout)
    try:
        player.play_songs([state["song"]])
        loop.run()
    finally:
        if not seen["error"]:
            GLib.source_remove(timer)
        player.shutdown()
    if seen["error"]:
        raise Failed(seen["error"])
    if seen["top"] <= 1.0:
        raise Failed(f"ended at {seen['top']:.2f} s, before 1 s")
    return f"played to {seen['top']:.2f} s and stopped at the end"


def _download(music: Path) -> str:
    from .core import download, resolve

    found = resolve(NET_URL)
    if len(found.tracks) != 1:
        raise Failed(f"resolved {len(found.tracks)} tracks")
    path = download(found.tracks[0], music, "opus")
    probe = json.loads(_run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration",
                             "-of", "json", str(path)]))
    codec, seconds = probe["streams"][0]["codec_name"], float(probe["format"]["duration"])
    if codec != "opus" or abs(seconds - NET_SECONDS) > 3:
        raise Failed(f"{path.name}: {codec}, {seconds:.1f} s")
    return f"{path.name}: {codec}, {seconds:.1f} s"


def _challenge() -> str:
    """The embedded-player client gets its stream URLs only through YouTube's JavaScript challenge, so
    this proves the runtime works with yt-dlp even when the default clients need no challenge."""
    import yt_dlp

    from .core import js_runtime

    name, exe = js_runtime() or ("", "")
    heard: list[str] = []

    class Log:
        def debug(self, message: str) -> None:
            heard.append(message)

        info = warning = error = debug

    options = {"logger": Log(), "noprogress": True, "js_runtimes": {name: {"path": exe}} if name else {},
               "extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}
    for attempt in range(3):  # the embedded player now and then answers with no formats at all
        time.sleep(3 * attempt)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(NET_URL, download=False)
        if failed := [m for m in heard if "challenge" in m.lower() and "fail" in m.lower()]:
            raise Failed(failed[0][:200])
        formats = info.get("formats") or []
        if audio := [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]:
            break
    else:
        raise Failed("no audio formats from the embedded player")
    solved = any(f"Solving JS challenges using {name}" in m for m in heard)
    return f"{len(audio)} audio formats, " + (f"challenge solved by {name}" if solved else "no challenge asked")
