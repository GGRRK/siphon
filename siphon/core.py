"""Siphon's engine: resolve a link into tracks, then download each one as a tagged audio file.

This module is the contract the window and the CLI code against; the Spotify reader lives in
spotify.py and the YouTube matcher in match.py.

yt-dlp is imported where a link is first resolved or downloaded, not with this module: a window only
playing music does without its 12 MB and the 0.05 s it takes to import, or 0.3 s from a downloaded
engine's wheel, for which Python keeps no bytecode (measured 2026-09-26, Python 3.14).
"""

import base64
import hashlib
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import match, names, paths

if TYPE_CHECKING:
    import yt_dlp
    from yt_dlp.utils import DownloadError


class SiphonError(Exception):
    """A failure whose message is one user-readable sentence."""


class Cancelled(SiphonError):
    def __init__(self, message: str = "Cancelled.") -> None:
        super().__init__(message)


FORMATS = ("opus", "m4a", "mp3", "flac", "best")  # first = the default: YouTube's own Opus, no re-encode
FORMAT_LABELS = {
    "mp3": "MP3",
    "m4a": "M4A (AAC)",
    "opus": "Opus",
    "flac": "FLAC",
    "best": "Original",
}
# What each format gets you, for the Format row, its list and `siphon get --help`: true whichever site a song comes
# from. Measured 2026-09-28 on 3 YouTube videos, a Spotify song (found on YouTube) and a SoundCloud song: YouTube
# sends Opus at 127-133 kbps (0.97-1.0 MB a minute) and AAC, the M4A kind, at 130-134 kbps (0.98-1.0 MB);
# SoundCloud sends AAC at 160 kbps (1.26 MB) and no Opus. A format the site sends is kept as it is (yt-dlp copies
# it), any other is converted: Opus from SoundCloud at 160 kbps (1.29 MB), MP3 at 244-272 kbps (1.83-2.04 MB, up
# to twice Original), FLAC 24-bit at 1.6-1.8 Mbps (12.0-13.4 MB, 10-14 times Original). FLAC is lossless, so its
# samples are Original's to -135 dB, bar the peaks a lossy decoder puts above full scale (0.05% of one song's).
FORMAT_NOTES = {
    "opus": "YouTube's own sound, untouched; small files; not for Apple Music",
    "m4a": "Small files that play almost anywhere, Apple Music included",
    "mp3": "Plays on anything, even old car stereos; files up to twice as big",
    "flac": "Sounds like Original, no better, in files 10-14 times bigger",
    "best": "Exactly what the site sends; the file type depends on the site",
}
# The quality meter: how close a format's sound is to the site's own, in blocks out of QUALITY_BLOCKS, and its word;
# never more than it is from any site. Original and FLAC keep the site's sound from every site. Opus, M4A and MP3
# are each converted from some site (Opus from SoundCloud, MP3 from YouTube, M4A from one that sends MP3), once and
# at a bitrate listening tests find nearly nobody can tell from its source; and M4A from YouTube is YouTube's other,
# AAC version of the song, not the Opus Original keeps. Excellent, then, but not the site's own.
QUALITY_BLOCKS = 4
FORMAT_QUALITY = {
    "opus": (3, "Excellent"),
    "m4a": (3, "Excellent"),
    "mp3": (3, "Excellent"),
    "flac": (4, "Best"),
    "best": (4, "Best"),
}


@dataclass
class Track:
    url: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float | None = None  # seconds
    cover_url: str = ""
    source: str = ""  # display name: "YouTube", "Spotify", "SoundCloud"...
    # Set for tracks known only by metadata (Spotify, other stores): the text searched on
    # YouTube. Their url is the store's page; the YouTube upload is found at download time.
    query: str = ""
    index: int | None = None
    track_no: int | None = None
    # Why this song can't be downloaded, when the list it came from already says so (a private or deleted song
    # of a SoundCloud set); download() fails with it at once. "" for every song that may download.
    error: str = ""


@dataclass
class Resolved:
    title: str
    kind: str  # "track" | "album" | "playlist"
    tracks: list[Track]
    folder: str | None  # subfolder for multi-track links, None for a single track
    note: str = ""
    cover_url: str = ""  # an album's or playlist's own picture, largest found; "" for a single track
    link: str = ""  # where a short link led (spotify.link), so a playlist is recognised however it was pasted


@dataclass
class Progress:
    stage: str  # "matching" | "downloading" | "converting" | "tagging" | "done"
    fraction: float | None
    detail: str = ""


ProgressFn = Callable[[Progress], None]

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
AUDIO_EXTS = ("opus", "m4a", "mp3", "ogg", "flac", "wav", "aac")
NAME_LIMIT = 180  # bytes, extension included
# Windows: characters a download's hidden work folder adds under the target, \.siphon-xxxxxxxx\audio.webm.part
_WORK_ROOM = 40

# format -> (yt-dlp format selector, FFmpegExtractAudio codec, quality). ExtractAudio copies
# the stream instead of re-encoding when the source already has the target codec.
_FORMAT_OPTS = {
    "mp3": ("bestaudio/best", "mp3", "0"),  # 0 = LAME VBR V0
    "m4a": ("bestaudio[ext=m4a]/bestaudio/best", "m4a", "192"),
    "opus": ("bestaudio[acodec=opus]/bestaudio/best", "opus", "160"),
    "flac": ("bestaudio/best", "flac", None),
    "best": ("bestaudio/best", "best", None),
}


# ---------------------------------------------------------------- small helpers


def is_supported(text: str) -> bool:
    """Cheap offline check for clipboard prefill: an http(s) URL or a spotify: URI."""
    text = text.strip()
    if re.fullmatch(r"spotify:[a-z]+:\S+", text):
        return True
    if not text or any(c.isspace() for c in text):
        return False
    parts = urllib.parse.urlsplit(text)
    return parts.scheme in ("http", "https") and "." in (parts.hostname or "")


def default_outdir() -> Path:
    return paths.music_dir()


def safe_name(text: str, limit: int = NAME_LIMIT) -> str:
    """A file or folder name: no slash or control characters, no leading dot, <= limit bytes.

    On Windows also none of the characters or device names it reserves (names.py)."""
    if paths.windows():
        text = names.windows_chars(text)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text.replace("/", "-"))
    text = " ".join(text.split()).lstrip(". ")
    if paths.windows():
        text = names.unreserved(text)
    text = text.encode()[:limit].decode("utf-8", "ignore").rstrip(" .")
    return text or "Untitled"


def file_stem(track: Track) -> str:
    name = f"{track.artist} - {track.title}" if track.artist else track.title
    return safe_name(name, NAME_LIMIT - len(".flac"))


def ytdlp_options(fmt: str, workdir: Path) -> dict:
    selector, codec, quality = _FORMAT_OPTS[fmt]
    extract = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
    if quality:
        extract["preferredquality"] = quality
    return {
        "format": selector,
        "postprocessors": [extract],
        "outtmpl": str(workdir / "audio.%(ext)s"),
        "noplaylist": True,
        # The library sorts by file time: a new download must be dated now, not by the server's Last-Modified.
        "updatetime": False,
    }


# yt-dlp's name for each runtime and its executable, in order of preference.
JS_RUNTIMES = (("node", "node"), ("deno", "deno"), ("quickjs", "qjs"))
NO_JS = "YouTube needs Node.js or Deno - install one, then try again."
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def js_runtime() -> tuple[str, str] | None:
    """(yt-dlp runtime name, executable) of the JavaScript runtime for YouTube's signature challenges:
    a packaged build's own one first (a Node.js on the system may be too old for yt-dlp), else the first
    on PATH; None when there is none."""
    bundle = paths.bundle_dir()
    for folder in ([str(bundle / "bin")] if bundle else []) + [None]:
        for name, exe in JS_RUNTIMES:
            if found := shutil.which(exe, path=folder):
                return name, found
    return None


def _is_youtube(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def _require_js() -> None:
    """Fail early and clearly when YouTube can't work: yt-dlp's own error says nothing useful."""
    if js_runtime() is None:
        raise SiphonError(NO_JS)


class _Quiet:
    """yt-dlp logger that prints nothing; failures still arrive as exceptions."""

    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


def _ydl(extra: dict) -> "yt_dlp.YoutubeDL":
    import yt_dlp

    _in_background(getattr(yt_dlp.utils, "Popen", None))
    runtime = js_runtime()
    return yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Quiet(),
        "js_runtimes": {runtime[0]: {"path": runtime[1]}} if runtime else {},
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        **extra,
    })


_background_lock = threading.Lock()


def _in_background(popen: type | None) -> None:
    """Every program yt-dlp runs - ffmpeg converting, ffprobe, the JavaScript runtime solving YouTube's challenges -
    runs below normal priority (paths.background). yt-dlp starts them all through its one Popen class, whose start
    this wraps, once; a yt-dlp without that class runs them as it would anyway."""
    with _background_lock:
        if popen is None or getattr(popen, "_siphon_background", False):
            return
        start = popen.__init__

        def start_in_background(self, *args, **kwargs) -> None:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | paths.background()
            start(self, *args, **kwargs)
            paths.lower_priority(self.pid)

        popen.__init__ = start_in_background
        popen._siphon_background = True


REFUSED = "YouTube refused the download - try Update engine."
# yt-dlp error text -> the sentence the user sees; first match wins.
_EXPLANATIONS = (
    (("sign in to confirm", "not a bot"),
     "YouTube asked for a sign-in to prove this isn't a bot - try again later."),
    (("private video",), "This video is private."),
    (("geo restriction", "from your location", "in your country"), "This isn't available in your country."),
    (("drm",), "That site locks its audio with DRM, so Siphon can't download it."),
    (("unsupported url",), "Siphon can't find any audio at this link."),
    (("age-restricted", "confirm your age", "age restricted"),
     "This video is age-restricted and can't be downloaded without signing in."),
    (("http error 404",), "That page doesn't exist any more."),
    (("http error 403", "forbidden", "signature", "nsig",
      "requested format is not available", "only images are available"),
     REFUSED),
    (("video unavailable", "not available", "has been removed", "account associated"),
     "This video isn't available."),
    (("unable to extract", "unable to parse"),
     "This site changed and yt-dlp can't read it yet - try Update engine."),
    (("ffmpeg", "ffprobe"), "ffmpeg is missing or failed - "
     + ("reinstall Siphon." if paths.windows() else "install it with: sudo pacman -S ffmpeg")),
    (("timed out", "unable to download", "urlopen error", "name resolution",
      "connection", "network is unreachable"),
     "Network problem - check your connection and try again."),
)


def explain(message: str) -> str:
    low = message.lower()
    for needles, sentence in _EXPLANATIONS:
        if any(n in low for n in needles):
            return sentence
    detail = message.strip().splitlines()[0] if message.strip() else "unknown error"
    detail = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]+:\s)?", "", detail)
    detail = re.sub(r"[;.]?\s*please report this issue.*$", "", detail, flags=re.I)
    return f"Download failed: {detail[:140]}"


def http_get(url: str, ua: str = BROWSER_UA, what: str = "that page") -> tuple[str, bytes]:
    """GET url following redirects; returns (final url, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise SiphonError(f"Couldn't find {what} - check the link.") from None
        host = urllib.parse.urlsplit(url).hostname
        raise SiphonError(f"{host} answered with error {e.code} - try again later.") from None
    except (urllib.error.URLError, OSError):
        raise SiphonError("Network problem - check your connection and try again.") from None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = dict(attrs)
            key, content = a.get("property") or a.get("name"), a.get("content")
            if key and content is not None:
                self.meta.setdefault(key.lower(), content.strip())


def page_meta(html_text: str) -> dict[str, str]:
    """<meta property|name=... content=...> pairs, first occurrence wins, entities decoded."""
    parser = _MetaParser()
    parser.feed(html_text)
    return parser.meta


def _iso_seconds(value: str) -> float | None:
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not m or not any(m.groups()):
        return None
    h, mins, s = (float(g or 0) for g in m.groups())
    return h * 3600 + mins * 60 + s


def track_from_meta(meta: dict[str, str], url: str) -> Track | None:
    """A song from a store page's Open Graph tags (Apple Music, Deezer, Tidal...), to be
    searched on YouTube. None when the page doesn't describe a song."""
    kind = meta.get("og:type", "")
    if not (kind.startswith("music.") or any(k.startswith("music:") for k in meta)):
        return None
    if kind.startswith("music.") and kind != "music.song":
        raise SiphonError("From this site Siphon can only fetch single songs - paste a song link.")
    og_title = meta.get("og:title", "")
    if not og_title:
        return None
    host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    site = meta.get("og:site_name") or host
    if m := re.fullmatch(r"(.+) by (.+?) on ([^,]+)", og_title):  # "Song by Artist on Apple Music"
        title, artist, site = m.groups()
    else:
        title, artist = og_title, ""
        parts = re.split(r"\s+[-·•|]\s+", meta.get("og:description", ""))
        if len(parts) > 1 and parts[0].casefold() not in {"song", "single", "album", "ep", "track"}:
            artist = parts[0]
        musician = meta.get("music:musician", "")
        if not artist and not musician.startswith("http"):
            artist = musician
    duration = _iso_seconds(meta.get("music:song:duration") or meta.get("music:duration") or "")
    return Track(url=url, title=title, artist=artist, duration=duration,
                 cover_url=meta.get("og:image", ""), source=" ".join(site.split()),
                 query=f"{artist} - {title}" if artist else title)


# ---------------------------------------------------------------- resolve


def resolve(url: str) -> Resolved:
    url = url.strip()
    if not is_supported(url):
        raise SiphonError("That isn't a link - paste a web address starting with https://.")
    from . import spotify  # spotify imports this module's types, so it loads lazily

    if spotify.handles(url):
        return spotify.resolve(url)
    return _resolve_ytdlp(url)


def _resolve_ytdlp(url: str) -> Resolved:
    from yt_dlp.utils import DownloadError

    if _is_youtube(url):
        _require_js()
    opts: dict = {"extract_flat": "in_playlist"}
    # watch?v=X&list=RD... is YouTube's endless auto-mix: the user means the one song.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    if "v" in query and query.get("list", [""])[0].startswith("RD"):
        opts["noplaylist"] = True
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries") if info else None
            if entries is not None:
                entries = [e for e in entries if e and e.get("ie_key") != "YoutubeTab"]
                hidden = sum(e.get("title") in _HIDDEN_VIDEOS for e in entries)
                entries = _complete([e for e in entries if e.get("title") not in _HIDDEN_VIDEOS], ydl)
    except DownloadError as e:
        reason = explain(str(e))
        if "drm" in str(e).lower() and not _is_youtube(url):
            return _resolve_locked(url, reason)
        if "unsupported url" in str(e).lower() or "drm" in str(e).lower():
            return _resolve_page(url, reason)
        raise SiphonError(reason) from None
    if not info:
        raise SiphonError("Siphon can't find any audio at this link.")

    if entries is None:
        track = _track_from_info(info)
        return Resolved(title=label(track), kind="track", tracks=[track], folder=None)

    tracks = []
    for entry, error in entries:
        track = _track_from_info(entry)
        track.error = error
        tracks.append(track)
    tracks = [t for t in tracks if t.url]
    if not tracks:
        raise SiphonError("Siphon found no tracks at this link - is the playlist private?")
    for i, t in enumerate(tracks):
        t.index = i
    title = info.get("title") or "Playlist"
    kind = "album" if "album" in (info.get("extractor_key") or "").lower() else "playlist"
    # A SoundCloud set that is an album or EP stays a playlist (its link makes one) but its songs are numbered.
    if kind == "album" or info.get("album_type") in _ALBUM_TYPES:
        for n, t in enumerate(tracks, 1):
            t.album, t.track_no = t.album or title, t.track_no or n
    # YouTube gives a playlist's own square picture (measured 2026-09-26: 240, 480 and 720 px); a playlist
    # without one takes its first video's picture.
    covers = _thumbnails(info)
    note = f"{hidden} private or deleted video{'s' if hidden != 1 else ''} left out." if hidden else ""
    return Resolved(title=title, kind=kind, tracks=tracks, folder=safe_name(title), note=note,
                    cover_url=covers[0] if covers else _video_picture(tracks[0]))


_HIDDEN_VIDEOS = ("[Private video]", "[Deleted video]")  # what a flat YouTube playlist lists them as
_ALBUM_TYPES = ("album", "ep", "single", "compilation")  # SoundCloud's set types, as yt-dlp passes them on


# ---------------------------------------------------------------- the songs of a list

# A list read flat names its songs only as far as the site's list page does. SoundCloud sets name none of them: each
# entry is a link (some only https://api-v2.soundcloud.com/tracks/<id>) with neither title nor artist, and SoundCloud
# user pages leave out the artist. Such songs are read in full before the list is shown, so each is listed, named
# and tagged as its own link would be - and, in download(), never taken for another song's file.
GONE = "This song is private or was deleted from SoundCloud."
_BATCH = 50  # songs per SoundCloud api-v2 tracks request: its limit
_READERS = 8  # songs read at once, one request each, where a site has no batch request


def _unnamed(entry: dict) -> bool:
    title, artist = match.describe(entry)
    return not (title and artist)


def _complete(entries: list[dict], ydl: "yt_dlp.YoutubeDL") -> list[tuple[dict, str]]:
    """Each entry of a flat list, in the list's order, with (where the list left the name out) the song's own
    metadata, and "" or why the song can't be downloaded. SoundCloud songs are read in batches; any other song
    without a title is read by itself, _READERS at a time; a song that still can't be read keeps its entry and
    download() reads it again, then says what is wrong."""
    done = [(entry, "") for entry in entries]
    soundcloud = [i for i, e in enumerate(entries) if e.get("ie_key") == "Soundcloud" and e.get("id") and _unnamed(e)]
    if soundcloud:
        try:
            found = _soundcloud_tracks(ydl, [str(entries[i]["id"]) for i in soundcloud])
        except Exception:  # SoundCloud's answer or yt-dlp's insides changed, or the network: one by one below
            found = None
        if found is not None:
            for i in soundcloud:
                song = found.get(str(entries[i]["id"]))
                done[i] = (_transparent(entries[i], song), "") if song else (entries[i], GONE)
    untitled = [i for i, (entry, error) in enumerate(done) if not error and not match.describe(entry)[0]]
    if untitled:
        with ThreadPoolExecutor(min(_READERS, len(untitled)), thread_name_prefix="siphon-read") as pool:
            read = list(pool.map(_read_one, [entries[i] for i in untitled]))
        for i, song in zip(untitled, read):
            if song:
                done[i] = (_transparent(entries[i], song), "")
    return done


def _transparent(entry: dict, song: dict) -> dict:
    """A song's own metadata, with what only its list entry knows (a set's album) added: the song names itself as
    its own link does, whatever title the list gave it."""
    listed = {k: v for k, v in entry.items() if k not in ("_type", "url", "ie_key")}
    return {**listed, **{k: v for k, v in song.items() if v is not None}}


def _read_one(entry: dict) -> dict | None:
    """One song's metadata, read from its own page; None when that fails (download() then says why)."""
    try:
        with _ydl({}) as ydl:  # one per song: yt-dlp objects are not shared between threads
            return ydl.extract_info(entry["url"], download=False, process=False)
    except Exception:
        return None


def _soundcloud_tracks(ydl: "yt_dlp.YoutubeDL", ids: list[str]) -> dict[str, dict]:
    """id -> the song's metadata, the fields a SoundCloud song's own link gives, for those of ids SoundCloud shares:
    a private or deleted song is left out. One api-v2 request per _BATCH songs, through yt-dlp's SoundCloud client
    (its client id, fetched again when SoundCloud turns it down): 6 songs in 0.12 s (measured 2026-09-28)."""
    ie = ydl.get_info_extractor("Soundcloud")
    ie.initialize()
    found = {}
    for start in range(0, len(ids), _BATCH):
        songs = ie._call_api(ie._API_V2_BASE + "tracks", None, "Downloading tracks",
                             query={"ids": ",".join(ids[start:start + _BATCH])}, headers=ie._HEADERS)
        if not isinstance(songs, list):  # not the answer this reads: read the songs one by one instead
            raise SiphonError("SoundCloud answered the tracks request in an unknown form.")
        for song in songs:
            if not isinstance(song, dict) or not song.get("id"):
                continue
            # yt-dlp checks each song's full-size artwork with a request of its own (0.1 s a song, measured
            # 2026-09-28), so the list takes the 500 px one, which is always there; the download reads the full size.
            # A song blocked in this country (policy BLOCK) is still named; its download says why it fails.
            user = song.get("user") if isinstance(song.get("user"), dict) else {}
            art = song.get("artwork_url") or user.get("avatar_url") or ""
            info = ie._extract_info_dict({**song, "policy": None, "artwork_url": None,
                                          "user": {**user, "avatar_url": None}}, extract_flat=True)
            found[str(song["id"])] = {
                **info, "extractor_key": "Soundcloud", "ie_key": "Soundcloud",
                "thumbnail": re.sub(r"-[0-9a-z]+\.(jpg|png)$", "-t500x500.jpg", art) if art else None}
    return found


def _video_picture(track: Track) -> str:
    """A YouTube video's largest thumbnail: a flat playlist entry lists at most 336x188, which squares to a soft
    188 px, while maxresdefault is 1280x720 (720 px squared, measured 2026-09-26). Other sites: the track's cover."""
    ids = urllib.parse.parse_qs(urllib.parse.urlsplit(track.url).query).get("v")
    if _is_youtube(track.url) and ids:
        return f"https://i.ytimg.com/vi/{ids[0]}/maxresdefault.jpg"
    return track.cover_url


def _resolve_locked(url: str, reason: str) -> Resolved:
    """A song whose site locks its audio with DRM (SoundCloud's paid songs): named by the site's own metadata, the
    way the same song in a list is, and downloaded from YouTube; the page's title tags when that metadata can't be
    read."""
    from yt_dlp.utils import DownloadError

    try:
        with _ydl({"ignore_no_formats_error": True}) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except DownloadError:
        info = None
    if not info or "entries" in info or info.get("_type") == "playlist":
        return _resolve_page(url, reason)
    track = _track_from_info(info)
    if not track.title:
        return _resolve_page(url, reason)
    track.query = _query(track)
    return Resolved(title=label(track), kind="track", tracks=[track], folder=None)


def _query(track: Track) -> str:
    """What to search YouTube for to find track."""
    return f"{track.artist.split(', ')[0]} - {track.title}" if track.artist else track.title


def _resolve_page(url: str, reason: str) -> Resolved:
    try:
        final, body = http_get(url)
    except SiphonError:
        raise SiphonError(reason) from None
    track = track_from_meta(page_meta(body.decode("utf-8", "replace")), final)
    if track is None:
        raise SiphonError(reason)
    return Resolved(title=label(track), kind="track", tracks=[track], folder=None)


def source_link(url: str) -> str:
    """One spelling of an album or playlist link, so the same playlist pasted in another form is recognised:
    Spotify's intl-xx/, embed/ and spotify: URI forms and YouTube's list= id on any of its hosts become one
    link; other links lose share-tracking parameters (si=, utm_*=), the fragment and www."""
    url = url.strip()
    from . import spotify

    if spotify.handles(url):
        try:
            kind, item = spotify.parse_link(url)
        except SiphonError:
            return url  # a spotify.link short link: only resolving it tells where it leads
        return f"https://open.spotify.com/{kind}/{item}"
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if _is_youtube(url) and (ids := [value for key, value in query if key == "list"]):
        return f"https://www.youtube.com/playlist?list={ids[0]}"
    query = [(key, value) for key, value in query if key != "si" and not key.startswith("utm_")]
    host = parts.netloc.lower().removeprefix("www.")
    return urllib.parse.urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"),
                                    urllib.parse.urlencode(query), ""))


def label(track: Track) -> str:
    """How a song is named where it is listed: "Artist - Title", its link while the name is unknown."""
    if not track.title:
        return track.url
    return f"{track.artist} - {track.title}" if track.artist else track.title


def _track_from_info(info: dict, index: int | None = None) -> Track:
    """The song a yt-dlp result describes; its title stays "" when the result has none (a flat list's entry):
    download() then names the song from its own page, never "Untitled", which every such song would share."""
    title, artist = match.describe(info)
    url = info.get("webpage_url") or info.get("url") or ""
    thumbnails = _thumbnails(info)
    return Track(
        url=url,
        title=title,
        artist=artist,
        # yt-dlp gives every song of a SoundCloud set the set's title as its album, a plain playlist's too
        album="" if info.get("album_type") == "playlist" else info.get("album") or "",
        duration=info.get("duration"),
        cover_url=thumbnails[0] if thumbnails else "",
        source=_source(info, url),
        index=index,
        track_no=info.get("track_number"),
    )


def _thumbnails(info: dict) -> list[str]:
    """Thumbnail URLs, best first (yt-dlp lists them worst first)."""
    urls = [info.get("thumbnail")] + [t.get("url") for t in reversed(info.get("thumbnails") or [])]
    return list(dict.fromkeys(u for u in urls if u))[:4]


def _source(info: dict, url: str) -> str:
    key = (info.get("extractor_key") or info.get("ie_key") or "").lower()
    host = urllib.parse.urlsplit(url).hostname or ""
    if host.startswith("music.youtube."):
        return "YouTube Music"
    if key.startswith("youtube"):
        return "YouTube"
    for name in ("SoundCloud", "Bandcamp", "Vimeo", "Mixcloud", "Audiomack"):
        if key.startswith(name.lower()):
            return name
    return host.removeprefix("www.") or "Web"


# ---------------------------------------------------------------- download


def download(track: Track, outdir: Path, fmt: str, progress: ProgressFn | None = None,
             cancel: threading.Event | None = None) -> Path:
    """Download one track into outdir as fmt and return the tagged file.

    Blocking and thread-safe; progress is called on the thread running this. Everything is
    written into a hidden work folder first, so a failure or cancel leaves nothing behind.
    """
    if fmt not in FORMATS:
        raise SiphonError(f"Unknown format {fmt!r}.")
    report = progress or (lambda p: None)

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    check()
    if track.error:
        raise SiphonError(track.error)
    if track.query and not track.title:
        raise SiphonError("The link doesn't give this song's name, so Siphon can't look for it.")
    if track.query or _is_youtube(track.url):  # store tracks (query set) are downloaded from YouTube
        _require_js()
    if track.source == "Spotify":
        from . import spotify

        track = spotify.enrich(track)
    outdir = Path(outdir)
    # A song the list could not fully name (no title, or no artist) is named once its own page is read, as its own
    # link would name it: until then its file can't be looked for, and a stand-in name would be every such song's.
    named = bool(track.query or (track.title and track.artist))
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        if named:
            stem = _fit(file_stem(track), outdir)
            if existing := _existing(outdir, stem, fmt):
                report(Progress("done", 1.0, "already downloaded"))
                return existing
        work = Path(tempfile.mkdtemp(prefix=".siphon-", dir=outdir))
    except OSError as e:
        raise SiphonError(f"Can't write to {outdir}: {e.strerror or e}.") from None

    from yt_dlp.utils import DownloadError

    try:
        with _ydl({**ytdlp_options(fmt, work), **_hooks(report, check),
                   "extract_flat": "in_playlist", "playlistend": 6}) as ydl:
            try:
                info = _source_info(ydl, track, report, check)
            except DownloadError as e:
                if track.query or not track.title or _is_youtube(track.url) or "drm" not in str(e).lower():
                    raise
                # A song of a list whose site locks its audio (SoundCloud's paid songs): found on YouTube like
                # the same song's own link (_resolve_locked), named and tagged from the list.
                _require_js()
                track = replace(track, query=_query(track))
                info = _source_info(ydl, track, report, check)
            if not named:
                if not track.query:
                    track = _named(track, info)
                stem = _fit(file_stem(track), outdir)
                if existing := _existing(outdir, stem, fmt):
                    report(Progress("done", 1.0, "already downloaded"))
                    return existing
            check()
            report(Progress("downloading", 0.0))
            info = _fetch(ydl, info, cancel)
        check()
        audio = _output_file(info, work)
        report(Progress("tagging", None))
        _tag(audio, track, info, _cover(_cover_urls(track, info), work))
        final = outdir / f"{stem}{audio.suffix}"
        os.replace(audio, final)
    except DownloadError as e:
        if _was_cancelled(e):
            raise Cancelled() from None
        raise SiphonError(explain(str(e))) from None
    except mutagen.MutagenError as e:
        raise SiphonError(f"Couldn't write the tags: {e}.") from None
    except OSError as e:
        raise SiphonError(f"Couldn't save the file: {e.strerror or e}.") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report(Progress("done", 1.0, final.name))
    return final


def _named(track: Track, info: dict) -> Track:
    """track named from its own page (info): the title and artist its link alone gets; a page without a title gives
    a name made of the site and the song's id, the same each time and no other song's."""
    own = _track_from_info(info)
    title = own.title or track.title
    if not title:
        song_id = str(info.get("id") or "") or hashlib.sha1(track.url.encode()).hexdigest()[:10]
        title = f"{track.source or own.source} {song_id}".strip()
    return replace(track, title=title, artist=own.artist or track.artist, album=track.album or own.album,
                   duration=track.duration or own.duration, cover_url=track.cover_url or own.cover_url,
                   track_no=track.track_no or own.track_no)


def _fit(stem: str, outdir: Path) -> str:
    """On Windows, stem shortened so the file's whole path stays under MAX_PATH."""
    if not paths.windows():
        return stem
    room = names.room(outdir)
    if room < _WORK_ROOM:
        raise SiphonError(f"The path of {outdir} is too long for Windows - choose a shorter folder.")
    return names.shorten(stem, room - len(".flac"))


def _fetch(ydl: "yt_dlp.YoutubeDL", info: dict, cancel: threading.Event | None) -> dict:
    """Download (and convert) an extracted result.

    YouTube refuses roughly one stream URL in six with HTTP 403, in bursts that can outlast a few
    seconds of plain retries (measured 2026-09-25: 1 of 6 downloads refused on all 3 default-client
    tries). So the retries wait longer and alternate with the embedded player, the only other
    yt-dlp client that still gets audio formats without a PO token (default and web_embedded
    worked; tv, web_safari, mweb, android_vr and ios did not).
    """
    from yt_dlp.utils import DownloadError

    clients = (None, None, ["web_embedded"], None, ["web_embedded"])
    for attempt, client in enumerate(clients):
        if attempt:
            if cancel is None:
                time.sleep(2 * attempt)
            elif cancel.wait(2 * attempt):
                raise Cancelled()
            ydl.params["extractor_args"] = {"youtube": {"player_client": client}} if client else {}
        try:
            if attempt == 0:
                return ydl.process_ie_result(info, download=True)
            return ydl.extract_info(info["webpage_url"], download=True)
        except DownloadError as e:
            if attempt == len(clients) - 1 or _was_cancelled(e) or explain(str(e)) != REFUSED:
                raise
    raise AssertionError("unreachable")


def _was_cancelled(e: "DownloadError") -> bool:
    return bool(e.exc_info) and isinstance(e.exc_info[1], Cancelled)


def _existing(outdir: Path, stem: str, fmt: str) -> Path | None:
    exts = AUDIO_EXTS if fmt == "best" else (fmt,)
    return next((p for e in exts if (p := outdir / f"{stem}.{e}").is_file()), None)


def _hooks(report: ProgressFn, check: Callable[[], None]) -> dict:
    last = [None]  # percent (or MB when the size is unknown) last reported: no UI flooding

    def on_download(d: dict) -> None:
        check()
        if d["status"] != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        fraction = min(done / total, 1.0) if total else None
        step = int(fraction * 100) if fraction is not None else -int(done / 1e6)
        if step != last[0]:
            last[0] = step
            size = f"{done / 1e6:.1f} of {total / 1e6:.1f} MB" if total else f"{done / 1e6:.1f} MB"
            report(Progress("downloading", fraction, size))

    def on_postprocess(d: dict) -> None:
        check()
        if d["status"] == "started" and d.get("postprocessor") == "ExtractAudio":
            report(Progress("converting", None))

    return {"progress_hooks": [on_download], "postprocessor_hooks": [on_postprocess]}


def _source_info(ydl: "yt_dlp.YoutubeDL", track: Track, report: ProgressFn,
                 check: Callable[[], None]) -> dict:
    """The unprocessed extractor result to download: the track itself, or its YouTube match."""
    if not track.query:
        info = ydl.extract_info(track.url, download=False, process=False)
        if not info or "entries" in info or info.get("_type") == "playlist":
            raise SiphonError("This link is a list, not a single track.")
        if _preview_only(info):
            raise SiphonError(PREVIEW)
        return info
    report(Progress("matching", None, f"Searching YouTube for {track.query}"))
    primary_artist = track.artist.split(", ")[0]
    found = match.find(ydl, track.query, track.title, primary_artist, track.duration, check)
    if found is None:
        raise SiphonError(f"No good match on YouTube for {label(track)}.")
    report(Progress("matching", 1.0, f"Matched {found.title}"))
    return found.info or ydl.extract_info(found.url, download=False, process=False)


PREVIEW = "SoundCloud only gives a 30-second preview of this song (the full song needs SoundCloud Go+)."


def _preview_only(info: dict) -> bool:
    """Whether a site offers only a preview clip of the song: yt-dlp still downloads the clip when nothing else is
    there, and it would be saved as the song (SoundCloud Go+ songs: 30 s)."""
    formats = info.get("formats") or []
    return bool(formats) and all("preview" in str(f.get("format_id") or "") for f in formats)


def _output_file(info: dict, work: Path) -> Path:
    for d in info.get("requested_downloads") or []:
        if d.get("filepath") and Path(d["filepath"]).is_file():
            return Path(d["filepath"])
    files = [p for p in work.iterdir() if p.suffix.lstrip(".") in AUDIO_EXTS]
    if len(files) != 1:
        raise SiphonError("The download finished but no audio file came out of it.")
    return files[0]


def _cover_urls(track: Track, info: dict) -> list[str]:
    # Store-sourced tracks keep the store's artwork; YouTube's is only a fallback.
    if track.query:
        return [track.cover_url] if track.cover_url else _thumbnails(info)
    return _thumbnails(info) + [track.cover_url]


def cover_image(urls: list[str]) -> bytes | None:
    """The first of urls that downloads, as a JPEG cropped to a centred square like a track's cover;
    None when none does. Blocking: the window calls it from a worker thread."""
    try:
        work = Path(tempfile.mkdtemp(prefix="siphon-cover-"))
    except OSError:
        return None
    try:
        return _cover(urls, work)
    except OSError:  # no ffmpeg, or no room for the work files: no picture, never a failure
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _cover(urls: list[str], work: Path) -> bytes | None:
    """The first cover that downloads, as a JPEG cropped to a centred square."""
    for url in filter(None, urls):
        try:
            _, data = http_get(url)
        except SiphonError:
            continue
        src, dst = work / "cover-source", work / "cover.jpg"
        src.write_bytes(data)
        with subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src),
             "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-frames:v", "1", "-q:v", "2", str(dst)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=paths.no_window() | paths.background(),
        ) as ffmpeg:
            paths.lower_priority(ffmpeg.pid)
        if ffmpeg.returncode == 0 and dst.is_file():
            return dst.read_bytes()
    return None


def _tag(path: Path, track: Track, info: dict, cover: bytes | None) -> None:
    album = track.album or ("" if track.query else info.get("album") or "")
    number = track.track_no or (None if track.query else info.get("track_number"))
    fields = {"title": track.title, "artist": track.artist, "album": album,
              "tracknumber": str(number) if number else ""}
    audio = mutagen.File(path)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio, MP3):
        frames = {"title": TIT2, "artist": TPE1, "album": TALB, "tracknumber": TRCK}
        for key, value in fields.items():
            if value:
                audio.tags.setall(frames[key].__name__, [frames[key](encoding=3, text=value)])
        if cover:
            audio.tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3,
                                            desc="Cover", data=cover)])
        audio.save(v2_version=3)
    elif isinstance(audio, MP4):
        atoms = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb"}
        for key, atom in atoms.items():
            if fields[key]:
                audio.tags[atom] = [fields[key]]
        if number:
            audio.tags["trkn"] = [(number, 0)]
        if cover:
            audio.tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif isinstance(audio, (FLAC, OggOpus, OggVorbis)):
        for key, value in fields.items():
            if value:
                audio[key] = [value]
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                audio.add_picture(pic)
            else:
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()


# ---------------------------------------------------------------- engine


def engine_version() -> str:
    """The yt-dlp version this process uses, read from its version module without importing yt-dlp itself where
    that module can be found the way importing would find it; else from yt-dlp, imported."""
    if "yt_dlp" not in sys.modules:
        try:
            package = importlib.util.find_spec("yt_dlp")  # a top-level name: finding it imports nothing
            spec = importlib.machinery.PathFinder.find_spec("yt_dlp.version", package.submodule_search_locations)
            found: dict = {}
            exec(spec.loader.get_code(spec.name), found)  # plain assignments, written by yt-dlp's release script
            if isinstance(found.get("__version__"), str):
                return found["__version__"]
        except Exception:  # anything unusual about where it lives: ask yt-dlp itself
            pass
    import yt_dlp

    return yt_dlp.version.__version__


def update_engine() -> str:
    """Fetch the newest yt-dlp and return the version the next launch will use.

    The running process keeps the yt-dlp it already imported: engine.py downloads verified wheels, which
    the next start loads (every build, see engine.py for why not pip).
    """
    from . import engine

    try:
        return engine.update(engine_version())
    except engine.UpdateError as e:
        raise SiphonError(str(e)) from None
