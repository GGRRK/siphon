"""Networked end-to-end check of the engine: manual, not collected by pytest.

    .venv/bin/python tests/live_check.py [--out DIR]

Downloads a few real links, checks every file with ffprobe (codec, length, tags, square
cover), cancels a download midway, and exits 1 if anything failed.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from siphon import core  # noqa: E402

ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
RICK = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
ALBUM = "https://open.spotify.com/album/4LH4d3cOWNNsVw41Gqt2kv"
PLAYLIST = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
OTHER_SITES = (
    "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
    "https://benprunty.bandcamp.com/track/lanius-battle",
)
LONG = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    if not ok:
        failures.append(name)
    return ok


def verify(name: str, path: Path, codec: str | None, duration: float,
           tags: dict[str, str] | None = None) -> None:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, text=True)
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    pics = [s for s in streams if s.get("disposition", {}).get("attached_pic")]
    found = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
    found |= {k.lower(): v for k, v in (audio.get("tags") or {}).items()}
    length = float(info.get("format", {}).get("duration") or 0)

    if codec:
        check(f"{name}: codec", audio.get("codec_name") == codec, audio.get("codec_name"))
    check(f"{name}: duration", abs(length - duration) <= 2, f"{length:.1f}s vs {duration:.1f}s")
    for key, want in (tags or {"title": "", "artist": ""}).items():
        got = found.get(key, "")
        check(f"{name}: tag {key}", got == want if want else bool(got), repr(got))
    size = f"{pics[0].get('width')}x{pics[0].get('height')}" if pics else "none"
    check(f"{name}: square cover", bool(pics) and pics[0]["width"] == pics[0]["height"], size)


def fetch(url: str, outdir: Path, fmt: str, cancel: threading.Event | None = None,
          on_progress=None) -> tuple[Path, list[core.Progress]]:
    seen: list[core.Progress] = []

    def progress(p: core.Progress) -> None:
        seen.append(p)
        if on_progress:
            on_progress(p)

    track = core.resolve(url).tracks[0]
    return core.download(track, outdir, fmt, progress, cancel), seen


def youtube(out: Path) -> None:
    source = core.resolve(ZOO).tracks[0]
    for fmt, codec in (("mp3", "mp3"), ("opus", "opus")):
        t = time.monotonic()
        path, seen = fetch(ZOO, out, fmt)
        stages = [p.stage for p in seen]
        check(f"youtube {fmt}: stages", stages[-1] == "done" and "tagging" in stages,
              f"{sorted(set(stages))} in {time.monotonic() - t:.1f}s")
        verify(f"youtube {fmt}", path, codec, source.duration)
    _, seen = fetch(ZOO, out, "mp3")
    check("youtube mp3 again: already downloaded", seen[-1].detail == "already downloaded")


def spotify_track(out: Path) -> None:
    t = time.monotonic()
    path, seen = fetch(RICK, out, "m4a")
    matched = next((p.detail for p in seen if p.stage == "matching" and p.fraction == 1.0), "")
    check("spotify track: matched the song", matched and not any(
        w in matched.lower() for w in ("remix", "live", "cover")), f"{matched!r} in "
        f"{time.monotonic() - t:.1f}s")
    verify("spotify track m4a", path, "aac", 213.6, {
        "title": "Never Gonna Give You Up", "artist": "Rick Astley",
        "album": "Whenever You Need Somebody"})


def spotify_lists(_out: Path) -> None:
    album = core.resolve(ALBUM)
    check("spotify album: 10 tracks", len(album.tracks) == 10, f"{album.title} / {album.folder}")
    check("spotify album: numbered, named, covered",
          [t.track_no for t in album.tracks] == list(range(1, 11))
          and all(t.album == album.title and t.cover_url and t.query for t in album.tracks))
    playlist = core.resolve(PLAYLIST)
    check("spotify playlist: tracks", playlist.kind == "playlist" and len(playlist.tracks) > 10,
          f"{len(playlist.tracks)} tracks, folder {playlist.folder!r}, note {playlist.note!r}")
    check("spotify playlist: searchable", all(t.query and t.duration for t in playlist.tracks))


def other_site(out: Path) -> None:
    for url in OTHER_SITES:
        try:
            track = core.resolve(url).tracks[0]
            path = core.download(track, out, "best")
        except core.SiphonError as e:
            print(f"      {url} did not work: {e}")
            continue
        check(f"{track.source}: downloaded as original", path.is_file(), path.name)
        verify(track.source, path, None, track.duration)
        return
    check("SoundCloud or Bandcamp", False, "no site worked")


def cancelling(out: Path) -> None:
    for url, stage in ((LONG, "downloading"), (RICK, "matching")):
        folder = out / f"cancel-{stage}"
        cancel = threading.Event()
        when: list[float] = []

        def stop(p: core.Progress, stage: str = stage) -> None:
            if p.stage == stage and not cancel.is_set():
                when.append(time.monotonic())
                cancel.set()

        try:
            fetch(url, folder, "flac", cancel, stop)
            check(f"cancel while {stage}", False, "download finished anyway")
            continue
        except core.Cancelled:
            delay = time.monotonic() - when[0]
        leftovers = [p.name for p in folder.iterdir()] if folder.exists() else []
        check(f"cancel while {stage}: prompt", delay < 3, f"{delay:.2f}s")
        check(f"cancel while {stage}: nothing left", not leftovers, leftovers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None)
    base = parser.parse_args().out
    if base:
        base.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="siphon-live-", dir=base))  # fresh, so reruns re-download
    print(f"yt-dlp {core.engine_version()}, writing to {out}")
    for step in (youtube, spotify_track, spotify_lists, other_site, cancelling):
        try:
            step(out)
        except core.SiphonError as e:
            check(step.__name__, False, f"SiphonError: {e}")
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
