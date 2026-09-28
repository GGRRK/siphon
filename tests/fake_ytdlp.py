"""yt-dlp as siphon/core.py drives it, answered from recordings, so list and download tests need no network.

FakeYdl stands in for core._ydl: every yt-dlp core opens is a session of it. A list link reads as `flat`; a song
link read by itself (process=False) as `pages[url]`, or raises it when it is an exception; SoundCloud's api-v2
tracks?ids= answers from `api`, served to yt-dlp's real SoundCloud extractor, which turns them into songs the way
it does for SoundCloud's own lists. `locked` links fail as SoundCloud's DRM songs do unless read with
ignore_no_formats_error. Downloads write a fake audio file (see fetch)."""

import copy
import json
import threading
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from siphon import core

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DRM = "ERROR: [soundcloud] 75206121: This video is DRM protected"


def soundcloud_set() -> tuple[dict, list[dict]]:
    """The Royal Concept EP as yt-dlp reads it flat, and SoundCloud's api-v2 answer for its six songs (recorded)."""
    data = json.loads((FIXTURES / "soundcloud_set.json").read_text(encoding="utf-8"))
    return data["flat"], data["tracks"]


class FakeYdl:
    def __init__(self, flat: dict | Exception | None = None, pages: dict | None = None, api: list | None = None,
                 api_error: Exception | None = None, locked: tuple[str, ...] = ()) -> None:
        self.flat = flat
        self.pages = pages or {}
        self.api = api or []
        self.api_error = api_error
        self.locked = locked
        self.reads: list[str] = []  # song links read by themselves, in the order asked
        self.requests: list[list[str]] = []  # the ids of each api-v2 tracks request
        self.sessions: list[dict] = []  # the options of every yt-dlp opened
        self._real = None
        self._lock = threading.Lock()

    def __call__(self, opts: dict) -> "_Session":
        self.sessions.append(opts)
        return _Session(self, opts)

    def soundcloud(self):
        """yt-dlp's own SoundCloud extractor, its requests answered here."""
        with self._lock:
            if self._real is None:
                self._real = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": core._Quiet()})
                ie = self._real.get_info_extractor("Soundcloud")
                ie._CLIENT_ID = "recorded"
                ie.initialize = lambda: None
                ie._call_api = self._call_api
            return self._real.get_info_extractor("Soundcloud")

    def _call_api(self, url: str, *args, query: dict | None = None, **kwargs):
        assert url == "https://api-v2.soundcloud.com/tracks", url
        ids = query["ids"].split(",")
        self.requests.append(ids)
        if self.api_error is not None:
            raise self.api_error
        return [copy.deepcopy(t) for t in self.api if str(t["id"]) in ids]


class _Session:
    def __init__(self, fake: FakeYdl, opts: dict) -> None:
        self.fake = fake
        self.params = dict(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def extract_info(self, url: str, download: bool = False, process: bool = True) -> dict:
        fake = self.fake
        if process:  # a list link, read flat
            if isinstance(fake.flat, Exception):
                raise fake.flat
            return copy.deepcopy(fake.flat)
        fake.reads.append(url)
        if url in fake.locked and not self.params.get("ignore_no_formats_error"):
            raise DownloadError(DRM)
        page = fake.pages.get(url)
        if page is None:
            raise DownloadError(f"ERROR: [generic] {url}: Unable to download webpage: HTTP Error 404: Not Found")
        if isinstance(page, Exception):
            raise page
        return copy.deepcopy(page)

    def get_info_extractor(self, key: str):
        assert key == "Soundcloud", key
        return self.fake.soundcloud()


def fetch(session: _Session, info: dict, cancel=None) -> dict:
    """core._fetch without a network: the song's audio is a few bytes in the download's work folder."""
    audio = Path(session.params["outtmpl"]).parent / "audio.opus"
    audio.write_bytes(b"OggS fake " + info.get("id", "").encode())
    return {**info, "requested_downloads": [{"filepath": str(audio)}]}


def song_page(song_id: str, title: str | None, uploader: str = "The Royal Concept", **extra) -> dict:
    """What yt-dlp reads from one SoundCloud song's own link (process=False), the fields core uses."""
    page = {"id": song_id, "title": title, "track": title, "uploader": uploader, "extractor_key": "Soundcloud",
            "webpage_url": f"https://soundcloud.com/band/{song_id}", "duration": 200.0,
            "thumbnails": [{"url": f"https://i1.sndcdn.com/artworks-{song_id}-original.jpg"}],
            "formats": [{"format_id": "hls_aac_160k", "url": "https://cf-hls-media.sndcdn.com/x.m3u8"}]}
    return {**page, **extra}
