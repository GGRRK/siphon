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
