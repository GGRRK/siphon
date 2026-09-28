"""`siphon get URL... [--format F] [--out DIR]`: download from the terminal."""

import argparse
import sys
from pathlib import Path

from .core import (FORMATS, Progress, Resolved, SiphonError, cover_image, default_outdir, download, label, resolve,
                   source_link)
from .library import PLAYLISTS_DIR
from .playlists import LinkedPlaylist, Playlists


class _Line:
    """One status line per track, rewritten in place when stdout is a terminal."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.live = sys.stdout.isatty()
        self.skipped = False

    def update(self, p: Progress) -> None:
        self.skipped = p.stage == "done" and p.detail == "already downloaded"
        if not self.live:
            return
        if p.stage == "downloading":
            state = f"{p.fraction:.0%}" if p.fraction is not None else p.detail
        else:
            state = "" if p.stage == "done" else p.stage
        print(f"\r{self.label}  {state}\x1b[K", end="", flush=True)

    def finish(self, text: str) -> None:
        print(f"\r{self.label}  {text}\x1b[K" if self.live else f"{self.label}  {text}", flush=True)


def _link(found: Resolved, url: str, outdir: Path) -> LinkedPlaylist | None:
    """The playlist a playlist link makes in outdir's Playlists folder, with its picture, as the window does."""
    try:
        linked = Playlists(outdir / PLAYLISTS_DIR).link(found.title, source_link(found.link or url))
        picture = cover_image([found.cover_url]) if found.cover_url else None
        if picture:
            linked.set_cover(picture)
        playlist = linked.playlist()
    except OSError as e:
        print(f"Couldn't save the playlist: {e.strerror or e}.", file=sys.stderr)
        return None
    if playlist is None:  # deleted the moment it was made
        return None
    print(f"Playlist: {playlist.file}" + ("" if linked.created else " (already there: new songs are added)"))
    return linked


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="siphon get",
                                     description="Save the audio of one or more links.")
    parser.add_argument("urls", nargs="+", metavar="URL")
    parser.add_argument("-f", "--format", choices=FORMATS, default=FORMATS[0])
    parser.add_argument("-o", "--out", type=Path, help=f"folder (default: {default_outdir()})")
    args = parser.parse_args(argv)
    outdir = args.out or default_outdir()

    failures = 0
    try:
        for url in args.urls:
            try:
                found = resolve(url)
            except SiphonError as e:
                print(f"{url}  failed: {e}", file=sys.stderr)
                failures += 1
                continue
            if found.folder:
                print(f"{found.title} ({len(found.tracks)} tracks)")
            if found.note:
                print(found.note)
            target = outdir  # no per-link subfolders, like the window
            linked = _link(found, url, target) if found.kind == "playlist" else None
            for n, track in enumerate(found.tracks, 1):
                name = label(track)
                line = _Line(f"[{n}/{len(found.tracks)}] {name}" if found.folder else name)
                try:
                    path = download(track, target, args.format, line.update)
                    line.finish(f"already there: {path}" if line.skipped else f"-> {path}")
                except SiphonError as e:
                    line.finish(f"failed: {e}")
                    failures += 1
                    continue
                if linked is not None:
                    try:
                        linked.add({n - 1: path})
                    except OSError as e:
                        print(f"Couldn't add it to the playlist: {e.strerror or e}.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 1 if failures else 0
