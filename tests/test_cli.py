"""`siphon get` with the engine faked: what it prints and the playlist a playlist link leaves behind."""

from pathlib import Path

import pytest

from siphon import cli
from siphon.core import Resolved, SiphonError, Track

LINK = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
PICTURE = "https://image-cdn-ak.spotifycdn.com/image/ab67706f0000000309dec89719704eea4f218966"
JPEG = b"\xff\xd8\xff\xe0" + b"square" * 8
SONGS = ["One", "Two", "Three"]


@pytest.fixture
def engine(monkeypatch):
    """A playlist or album of SONGS; downloads fail for the titles in engine.failing."""
    state = type("Engine", (), {"failing": set(), "kind": "playlist", "resolved": []})()

    def resolve(url: str) -> Resolved:
        state.resolved.append(url)
        tracks = [Track(url=f"https://open.spotify.com/track/{n}", title=title, artist="Band", index=n)
                  for n, title in enumerate(SONGS)]
        return Resolved(title="Top Hits", kind=state.kind, tracks=tracks, folder="Top Hits", cover_url=PICTURE)

    def download(track: Track, outdir: Path, fmt: str, progress=None, cancel=None) -> Path:
        if track.title in state.failing:
            raise SiphonError(f"No good match on YouTube for Band - {track.title}.")
        path = outdir / f"Band - {track.title}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(cli, "resolve", resolve)
    monkeypatch.setattr(cli, "download", download)
    monkeypatch.setattr(cli, "cover_image", lambda urls: JPEG if urls == [PICTURE] else None)
    return state


def entries(file: Path) -> list[str]:
    """The playlist's songs, with Linux separators (Windows writes backslashes)."""
    lines = file.read_text(encoding="utf-8").splitlines()
    return [line.replace("\\", "/") for line in lines if not line.startswith("#")]


def test_a_playlist_link_writes_its_playlist_and_picture(engine, tmp_path, capsys):
    engine.failing = {"Two"}
    assert cli.main([LINK + "?si=abc", "-o", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    file = tmp_path / "Playlists" / "Top Hits.m3u8"
    assert f"Playlist: {file}\n" in out
    assert entries(file) == ["../Band - One.opus", "../Band - Three.opus"]
    assert (tmp_path / "Playlists" / "Top Hits.jpg").read_bytes() == JPEG
    head = file.read_text(encoding="utf-8").splitlines()[:4]
    assert head == ["#EXTM3U", "#PLAYLIST:Top Hits", "#EXTIMG:Top Hits.jpg", f"#SIPHON-SOURCE:{LINK}"]

    # The same playlist again, as a URI: the failed song now works and joins at its place.
    engine.failing = set()
    assert cli.main(["spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "-o", str(tmp_path)]) == 0
    assert f"Playlist: {file} (already there: new songs are added)\n" in capsys.readouterr().out
    assert entries(file) == ["../Band - One.opus", "../Band - Two.opus", "../Band - Three.opus"]
    assert sorted(f.name for f in (tmp_path / "Playlists").iterdir()) == ["Top Hits.jpg", "Top Hits.m3u8"]


def test_a_playlist_without_a_picture_still_gets_its_playlist(engine, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cover_image", lambda urls: None)
    assert cli.main([LINK, "-o", str(tmp_path)]) == 0
    assert sorted(f.name for f in (tmp_path / "Playlists").iterdir()) == ["Top Hits.m3u8"]
    assert len(entries(tmp_path / "Playlists" / "Top Hits.m3u8")) == 3


def test_an_album_makes_no_playlist(engine, tmp_path, capsys):
    engine.kind = "album"
    assert cli.main(["https://open.spotify.com/album/4LH4d3cOWNNsVw41Gqt2kv", "-o", str(tmp_path)]) == 0
    assert "Playlist" not in capsys.readouterr().out
    assert not (tmp_path / "Playlists").exists()


def test_an_unwritable_playlist_folder_does_not_stop_the_downloads(engine, tmp_path, capsys):
    (tmp_path / "Playlists").write_text("a file where the folder should be")
    assert cli.main([LINK, "-o", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "Couldn't save the playlist" in captured.err
    assert (tmp_path / "Band - Three.opus").exists()
