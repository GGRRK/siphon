import os
import shutil

import pytest

from siphon import core
from siphon.core import SiphonError, Track


@pytest.mark.parametrize("text, ok", [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", True),
    ("  https://soundcloud.com/a/b \n", True),
    ("http://example.org", True),
    ("spotify:track:4cOdK2wGLETKBW3PvgPWqT", True),
    ("ftp://example.org/x", False),
    ("https://localhost", False),
    ("hello world", False),
    ("https://example.org/a b", False),
    ("", False),
])
def test_is_supported(text, ok):
    assert core.is_supported(text) is ok


def test_resolve_rejects_non_links_offline():
    with pytest.raises(SiphonError, match="isn't a link"):
        core.resolve("never gonna give you up")


def test_safe_name():
    assert core.safe_name("AC/DC - Back In Black") == "AC-DC - Back In Black"
    assert core.safe_name("..hidden\x00 name\n") == "hidden name"
    assert core.safe_name("   ") == "Untitled"
    long = core.safe_name("é" * 200)
    assert len(long.encode()) <= core.NAME_LIMIT and long == "é" * 90


def test_file_stem_leaves_room_for_the_extension():
    stem = core.file_stem(Track(url="", title="x" * 300, artist="A"))
    assert stem.startswith("A - x") and len((stem + ".flac").encode()) <= core.NAME_LIMIT
    assert core.file_stem(Track(url="", title="Only Title")) == "Only Title"


@pytest.mark.parametrize("fmt, selector, codec, quality", [
    ("mp3", "bestaudio/best", "mp3", "0"),
    ("m4a", "bestaudio[ext=m4a]/bestaudio/best", "m4a", "192"),
    ("opus", "bestaudio[acodec=opus]/bestaudio/best", "opus", "160"),
    ("flac", "bestaudio/best", "flac", None),
    ("best", "bestaudio/best", "best", None),
])
def test_ytdlp_options(fmt, selector, codec, quality, tmp_path):
    opts = core.ytdlp_options(fmt, tmp_path)
    (extract,) = opts["postprocessors"]
    assert opts["format"] == selector and opts["noplaylist"] is True
    assert extract == {"key": "FFmpegExtractAudio", "preferredcodec": codec,
                       **({"preferredquality": quality} if quality else {})}
    assert opts["outtmpl"].startswith(str(tmp_path))
    assert opts["updatetime"] is False  # dated now, so a download sorts first in the library


def test_every_format_has_a_label_and_options():
    assert set(core.FORMATS) == set(core.FORMAT_LABELS) == set(core._FORMAT_OPTS)


def test_download_rejects_unknown_format(tmp_path):
    with pytest.raises(SiphonError, match="Unknown format"):
        core.download(Track(url="https://youtu.be/x", title="t"), tmp_path, "wav")


def test_download_returns_existing_file_untouched(tmp_path):
    track = Track(url="https://youtu.be/x", title="Song", artist="Band")
    existing = tmp_path / "Band - Song.opus"
    existing.write_bytes(b"old")
    seen = []
    assert core.download(track, tmp_path, "opus", seen.append) == existing
    assert existing.read_bytes() == b"old"
    assert [(p.stage, p.detail) for p in seen] == [("done", "already downloaded")]
    assert core.download(track, tmp_path, "best") == existing  # any audio type counts for "best"


def test_download_honours_a_cancel_set_before_it_starts(tmp_path):
    import threading

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(core.Cancelled):
        core.download(Track(url="https://youtu.be/x", title="t"), tmp_path, "mp3", cancel=cancel)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("message, expected", [
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot", "bot"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "try Update engine"),
    ("ERROR: [youtube] abc: Requested format is not available", "try Update engine"),
    ("ERROR: [youtube] abc: Private video. Sign in", "private"),
    ("ERROR: [youtube] abc: Video unavailable", "isn't available"),
    ("ERROR: [generic] x: Unable to download webpage: HTTP Error 404: Not Found", "doesn't exist"),
    ("ERROR: [generic] x: Unable to download webpage: <urlopen error timed out>", "Network problem"),
    ("ERROR: Unsupported URL: https://example.com/", "can't find any audio"),
    ("ERROR: [DRM] The requested site is known to use DRM protection.", "DRM"),
    ("ERROR: [youtube] abc: Something odd happened", "Download failed: Something odd happened"),
    ("ERROR: [Bandcamp] x: Unable to extract tralbum data; please report this issue on  https://",
     "try Update engine"),
    ("ERROR: [Bandcamp] x: Oddity; please report this issue on  https://", "Download failed: Oddity"),
])
def test_explain(message, expected):
    assert expected in core.explain(message)


@pytest.mark.linux
def test_default_outdir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_MUSIC_DIR", str(tmp_path / "Tunes"))
    assert core.default_outdir() == tmp_path / "Tunes"

    monkeypatch.delenv("XDG_MUSIC_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "user-dirs.dirs").write_text('XDG_MUSIC_DIR="$HOME/Musik"\n')
    assert core.default_outdir() == tmp_path / "Musik"

    (tmp_path / "cfg" / "user-dirs.dirs").unlink()
    assert core.default_outdir() == tmp_path / "Music"


def test_apple_music_page(fixture_text):
    url = "https://music.apple.com/us/album/never-gonna-give-you-up/1559885420?i=1559885421"
    t = core.track_from_meta(core.page_meta(fixture_text("apple_music_song.html")), url)
    assert (t.title, t.artist, t.duration, t.source) == (
        "Never Gonna Give You Up", "Rick Astley", 213.0, "Apple Music")
    assert t.query == "Rick Astley - Never Gonna Give You Up" and t.cover_url and t.url == url


def test_deezer_page(fixture_text):
    t = core.track_from_meta(core.page_meta(fixture_text("deezer_song.html")),
                             "https://www.deezer.com/track/14408104")
    assert (t.title, t.artist, t.duration, t.source) == (
        "Never Gonna Give You Up", "Rick Astley", 211.0, "Deezer")


def test_pages_that_are_not_songs():
    assert core.track_from_meta({"og:title": "News", "og:type": "article"}, "https://x.org") is None
    with pytest.raises(SiphonError, match="single songs"):
        core.track_from_meta({"og:title": "An Album", "og:type": "music.album"}, "https://x.org")


def test_page_meta_decodes_entities_and_keeps_the_first():
    meta = core.page_meta('<meta property="og:title" content="A &amp; B">'
                          "<meta name='og:title' content='second'><meta content=x>")
    assert meta == {"og:title": "A & B"}


def test_source_names():
    assert core._source({"extractor_key": "Youtube"}, "https://www.youtube.com/watch?v=x") == "YouTube"
    assert core._source({"ie_key": "Youtube"}, "https://music.youtube.com/watch?v=x") == "YouTube Music"
    assert core._source({"extractor_key": "SoundcloudSet"}, "https://soundcloud.com/a") == "SoundCloud"
    assert core._source({"extractor_key": "Generic"}, "https://www.example.org/a") == "example.org"


def test_tagging_round_trip(tmp_path):
    """Tags and a cover land in every container Siphon writes (needs ffmpeg, no network)."""
    import subprocess

    import mutagen

    cover = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64",
                    "-frames:v", "1", str(cover)], check=True)
    track = Track(url="", title="Title", artist="Artist", album="Album", track_no=3)
    for ext, codec in (("mp3", "libmp3lame"), ("m4a", "aac"), ("opus", "libopus"),
                       ("flac", "flac"), ("ogg", "libvorbis")):
        path = tmp_path / f"t.{ext}"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-c:a", codec,
                        str(path)], check=True)
        core._tag(path, track, {}, cover.read_bytes())
        easy = mutagen.File(path, easy=True)
        assert easy["title"] == ["Title"] and easy["artist"] == ["Artist"], ext
        assert easy["album"] == ["Album"] and easy["tracknumber"][0].startswith("3"), ext
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "stream_disposition=attached_pic", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True).stdout
        assert "1" in probe.split(), ext


class FlakyYDL:
    """process_ie_result/extract_info fail with the given errors in turn, then succeed."""

    def __init__(self, *errors: str):
        self.errors, self.calls, self.params = list(errors), [], {}

    def _next(self, name: str) -> dict:
        from yt_dlp.utils import DownloadError

        client = self.params.get("extractor_args", {}).get("youtube", {}).get("player_client")
        self.calls.append(f"{name} {client[0]}" if client else name)
        if self.errors:
            raise DownloadError(self.errors.pop(0))
        return {"done": True}

    def process_ie_result(self, info, download):
        return self._next("process")

    def extract_info(self, url, download):
        return self._next(f"extract {url}")


FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def test_fetch_retries_refused_downloads_with_a_fresh_extraction(monkeypatch):
    pauses = []
    monkeypatch.setattr(core.time, "sleep", pauses.append)
    ydl = FlakyYDL(FORBIDDEN, FORBIDDEN, FORBIDDEN)
    assert core._fetch(ydl, {"webpage_url": "u"}, None) == {"done": True}
    # a refusal burst outlasts plain retries: the third and fifth try use the embedded player
    assert ydl.calls == ["process", "extract u", "extract u web_embedded", "extract u"] and pauses == [2, 4, 6]


def test_fetch_gives_up_and_leaves_other_errors_alone(monkeypatch):
    from yt_dlp.utils import DownloadError

    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    with pytest.raises(DownloadError):
        core._fetch(FlakyYDL(*[FORBIDDEN] * 5), {"webpage_url": "u"}, None)
    ydl = FlakyYDL("ERROR: [youtube] x: Private video")
    with pytest.raises(DownloadError):
        core._fetch(ydl, {"webpage_url": "u"}, None)
    assert ydl.calls == ["process"]


def test_fetch_cancel_during_the_pause():
    import threading

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(core.Cancelled):
        core._fetch(FlakyYDL(FORBIDDEN), {"webpage_url": "u"}, cancel)


# ---------------------------------------------------------------- album and playlist pictures


class _FakeYdl:
    def __init__(self, info: dict) -> None:
        self.info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def extract_info(self, url: str, download: bool = False) -> dict:
        return self.info


def _entry(n: int) -> dict:
    return {"title": f"Band - Song {n}", "url": f"https://soundcloud.com/band/song-{n}", "ie_key": "Soundcloud",
            "thumbnails": [{"url": f"https://i1.sndcdn.com/song-{n}-large.jpg"}]}


def _resolve_info(monkeypatch, info: dict):
    monkeypatch.setattr(core, "_ydl", lambda opts: _FakeYdl(info))
    return core.resolve("https://soundcloud.com/band/sets/mix")


def test_a_playlist_gets_its_largest_picture(monkeypatch):
    # yt-dlp lists thumbnails smallest first: YouTube's own square playlist picture comes in 240, 480 and 720 px
    square = "https://i.ytimg.com/pl_c/PLx/studio_square_thumbnail.jpg?sqp={}"
    found = _resolve_info(monkeypatch, {
        "title": "Mix", "extractor_key": "SoundcloudSet", "entries": [_entry(1), _entry(2)],
        "thumbnails": [{"url": square.format(px), "width": px, "height": px} for px in (240, 480, 720)]})
    assert (found.kind, found.cover_url) == ("playlist", square.format(720))


def test_a_playlist_without_a_picture_takes_its_first_songs(monkeypatch):
    found = _resolve_info(monkeypatch, {"title": "Mix", "extractor_key": "BandcampAlbum",
                                        "entries": [None, _entry(1), _entry(2)]})
    assert (found.kind, found.cover_url) == ("album", "https://i1.sndcdn.com/song-1-large.jpg")


def test_a_youtube_playlist_without_a_picture_takes_its_first_videos_largest(monkeypatch):
    entry = {"title": "Song", "url": "https://www.youtube.com/watch?v=abcdefghijk", "ie_key": "Youtube",
             "thumbnails": [{"url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg", "width": 336, "height": 188}]}
    monkeypatch.setattr(core, "_ydl", lambda opts: _FakeYdl({"title": "Mix", "extractor_key": "YoutubeTab",
                                                             "entries": [entry]}))
    monkeypatch.setattr(core, "_require_js", lambda: None)
    found = core.resolve("https://www.youtube.com/playlist?list=PLx")
    assert found.cover_url == "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg"


def test_a_single_track_has_no_collection_picture(monkeypatch):
    found = _resolve_info(monkeypatch, {**_entry(1), "webpage_url": "https://soundcloud.com/band/song-1"})
    assert found.kind == "track" and found.cover_url == ""


SPOTIFY = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
YOUTUBE = "https://www.youtube.com/playlist?list=PLaklDK5Yxuss"


@pytest.mark.parametrize("link, expected", [
    (SPOTIFY, SPOTIFY),
    (SPOTIFY + "?si=4f2a9c", SPOTIFY),
    ("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x&nd=1", SPOTIFY),
    ("https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M", SPOTIFY),
    ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", SPOTIFY),
    ("  spotify:user:someone:playlist:37i9dQZF1DXcBWIGoYBM5M ", SPOTIFY),
    ("https://spotify.link/AbCdEf", "https://spotify.link/AbCdEf"),  # short links need resolving: kept
    (YOUTUBE, YOUTUBE),
    (YOUTUBE + "&si=abc", YOUTUBE),
    ("https://music.youtube.com/playlist?list=PLaklDK5Yxuss&si=abc", YOUTUBE),
    ("https://youtube.com/playlist?list=PLaklDK5Yxuss", YOUTUBE),
    ("https://m.youtube.com/playlist?list=PLaklDK5Yxuss#top", YOUTUBE),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLaklDK5Yxuss&index=3", YOUTUBE),
    ("https://soundcloud.com/band/sets/mix?si=1a2b&utm_source=clipboard&utm_medium=text",
     "https://soundcloud.com/band/sets/mix"),
    ("https://www.soundcloud.com/band/sets/mix/", "https://soundcloud.com/band/sets/mix"),
    ("https://example.com/list?id=5&utm_campaign=x#part", "https://example.com/list?id=5"),
])
def test_source_link(link, expected):
    assert core.source_link(link) == expected


def test_youtube_playlist_ids_keep_their_case():
    assert core.source_link("https://www.youtube.com/playlist?list=PLabcDEF") != \
        core.source_link("https://www.youtube.com/playlist?list=PLabcdef")


def _png(width: int, height: int) -> bytes:
    import struct
    import zlib

    rows = b"".join(b"\x00" + b"\x10\x80\xf0" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _size(image: bytes) -> tuple[int, int]:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader()
    loader.write(image)
    loader.close()
    pixbuf = loader.get_pixbuf()
    return pixbuf.get_width(), pixbuf.get_height()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_cover_image_is_the_first_that_downloads_squared(monkeypatch):
    def get(url: str, **_kw) -> tuple[str, bytes]:
        if "gone" in url:
            raise SiphonError("Couldn't find that page - check the link.")
        return url, _png(480, 360)  # an old 4:3 YouTube thumbnail

    monkeypatch.setattr(core, "http_get", get)
    image = core.cover_image(["https://example.com/gone.jpg", "", "https://example.com/hq.jpg"])
    assert image.startswith(b"\xff\xd8\xff") and _size(image) == (360, 360)
    assert core.cover_image(["https://example.com/gone.jpg"]) is None


def test_cover_image_without_ffmpeg_is_none(monkeypatch):
    def no_ffmpeg(*_args, **_kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(core, "http_get", lambda url, **_kw: (url, _png(8, 8)))
    monkeypatch.setattr(core.subprocess, "Popen", no_ffmpeg)
    assert core.cover_image(["https://example.com/a.jpg"]) is None


# -- helper programs run below normal priority, so a game keeps the CPU it needs (the player keeps normal priority)

class FakePopen:
    """Stands in for yt-dlp's Popen: records how it was started."""

    def __init__(self, args, **kwargs) -> None:
        self.args, self.kwargs, self.pid = args, kwargs, 4242


@pytest.fixture
def lowered(monkeypatch) -> list[tuple[int, int]]:
    """(pid, nice) of every POSIX priority change, none made for real; each program starts at nice 3."""
    changes = []
    monkeypatch.setattr(core.paths.os, "PRIO_PROCESS", 0, raising=False)  # POSIX's names, on Windows too
    monkeypatch.setattr(core.paths.os, "getpriority", lambda which, pid: 3, raising=False)
    monkeypatch.setattr(core.paths.os, "setpriority", lambda which, pid, nice: changes.append((pid, nice)),
                        raising=False)
    return changes


def test_yt_dlps_programs_start_below_normal_on_windows(monkeypatch, lowered):
    monkeypatch.setattr(core.paths, "windows", lambda: True)
    popen = type("Popen", (FakePopen,), {})
    core._in_background(popen)
    core._in_background(popen)  # once only, however many downloads start
    assert popen(["ffmpeg"], text=True).kwargs == {"text": True, "creationflags": 0x00004000}
    assert popen(["qjs"], creationflags=0x08000000).kwargs["creationflags"] == 0x08004000  # the flag is added
    assert lowered == []


def test_yt_dlps_programs_are_niced_once_started_elsewhere(monkeypatch, lowered):
    monkeypatch.setattr(core.paths, "windows", lambda: False)
    popen = type("Popen", (FakePopen,), {})
    core._in_background(popen)
    core._in_background(popen)
    assert popen(["ffmpeg"]).kwargs == {"creationflags": 0}
    assert lowered == [(4242, 13)]
    monkeypatch.setattr(core.paths.os, "getpriority", lambda which, pid: 15)
    popen(["ffprobe"])
    assert lowered[-1] == (4242, 19)  # the lowest priority there is


def test_a_yt_dlp_without_its_popen_class_still_downloads():
    core._in_background(None)


@pytest.mark.linux
def test_yt_dlps_programs_really_run_niced():
    import yt_dlp

    core._ydl({}).close()  # what every resolve and download does first
    own = os.getpriority(os.PRIO_PROCESS, 0)
    with yt_dlp.utils.Popen(["sleep", "5"]) as program:
        try:
            assert os.getpriority(os.PRIO_PROCESS, program.pid) == min(19, own + core.paths.BACKGROUND_NICE)
        finally:
            program.kill()


def test_the_cover_crop_runs_below_normal(monkeypatch, lowered):
    started = []

    class Crop(FakePopen):
        returncode = 1

        def __enter__(self):
            started.append(self)
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(core, "http_get", lambda url, **_kw: (url, _png(8, 8)))
    monkeypatch.setattr(core.subprocess, "Popen", Crop)
    monkeypatch.setattr(core.paths, "windows", lambda: True)
    assert core.cover_image(["https://example.com/a.jpg"]) is None
    assert started[-1].kwargs["creationflags"] == 0x08000000 | 0x00004000 and lowered == []
    monkeypatch.setattr(core.paths, "windows", lambda: False)
    core.cover_image(["https://example.com/a.jpg"])
    assert started[-1].kwargs["creationflags"] == 0 and lowered == [(4242, 13)]
