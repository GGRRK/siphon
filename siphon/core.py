"""Siphon's engine: resolve a link into tracks, then download each one as a tagged audio file.

This module is the contract the window and the CLI code against; the Spotify reader lives in
spotify.py and the YouTube matcher in match.py.
"""

import base64
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
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import mutagen
import yt_dlp
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from yt_dlp.utils import DownloadError

from . import match, names, paths


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
    "best": "Original (no re-encode)",
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


@dataclass
class Resolved:
    title: str
    kind: str  # "track" | "album" | "playlist"
    tracks: list[Track]
    folder: str | None  # subfolder for multi-track links, None for a single track
    note: str = ""
    cover_url: str = ""  # an album's or playlist's own picture, largest found; "" for a single track


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


def _ydl(extra: dict) -> yt_dlp.YoutubeDL:
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


REFUSED = "YouTube refused the download - try Update engine."
# yt-dlp error text -> the sentence the user sees; first match wins.
_EXPLANATIONS = (
    (("sign in to confirm", "not a bot"),
     "YouTube asked for a sign-in to prove this isn't a bot - try again later."),
    (("private video",), "This video is private."),
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
    except DownloadError as e:
        reason = explain(str(e))
        if "unsupported url" in str(e).lower() or "drm" in str(e).lower():
            return _resolve_page(url, reason)
        raise SiphonError(reason) from None
    if not info:
        raise SiphonError("Siphon can't find any audio at this link.")

    entries = info.get("entries")
    if entries is None:
        track = _track_from_info(info)
        return Resolved(title=_label(track), kind="track", tracks=[track], folder=None)

    tracks = [
        _track_from_info(e, index=i)
        for i, e in enumerate(list(entries))
        if e and e.get("title") not in ("[Private video]", "[Deleted video]")
        and e.get("ie_key") != "YoutubeTab"
    ]
    tracks = [t for t in tracks if t.url]
    if not tracks:
        raise SiphonError("Siphon found no tracks at this link - is the playlist private?")
    for i, t in enumerate(tracks):
        t.index = i
    title = info.get("title") or "Playlist"
    kind = "album" if "album" in (info.get("extractor_key") or "").lower() else "playlist"
    if kind == "album":
        for n, t in enumerate(tracks, 1):
            t.album, t.track_no = t.album or title, t.track_no or n
    # YouTube gives a playlist's own square picture (measured 2026-09-26: 240, 480 and 720 px), or for a
    # playlist without one its first video's thumbnail (up to 336x188, squared to 188).
    covers = _thumbnails(info)
    return Resolved(title=title, kind=kind, tracks=tracks, folder=safe_name(title),
                    cover_url=covers[0] if covers else tracks[0].cover_url)


def _resolve_page(url: str, reason: str) -> Resolved:
    try:
        final, body = http_get(url)
    except SiphonError:
        raise SiphonError(reason) from None
    track = track_from_meta(page_meta(body.decode("utf-8", "replace")), final)
    if track is None:
        raise SiphonError(reason)
    return Resolved(title=_label(track), kind="track", tracks=[track], folder=None)


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


def _label(track: Track) -> str:
    return f"{track.artist} - {track.title}" if track.artist else track.title


def _track_from_info(info: dict, index: int | None = None) -> Track:
    title, artist = match.describe(info)
    url = info.get("webpage_url") or info.get("url") or ""
    thumbnails = _thumbnails(info)
    return Track(
        url=url,
        title=title or "Untitled",
        artist=artist,
        album=info.get("album") or "",
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
    if track.query or _is_youtube(track.url):  # store tracks (query set) are downloaded from YouTube
        _require_js()
    if track.source == "Spotify":
        from . import spotify

        track = spotify.enrich(track)
    outdir = Path(outdir)
    stem = _fit(file_stem(track), outdir)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        existing = _existing(outdir, stem, fmt)
        if existing:
            report(Progress("done", 1.0, "already downloaded"))
            return existing
        work = Path(tempfile.mkdtemp(prefix=".siphon-", dir=outdir))
    except OSError as e:
        raise SiphonError(f"Can't write to {outdir}: {e.strerror or e}.") from None

    try:
        with _ydl({**ytdlp_options(fmt, work), **_hooks(report, check),
                   "extract_flat": "in_playlist", "playlistend": 6}) as ydl:
            info = _source_info(ydl, track, report, check)
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


def _fit(stem: str, outdir: Path) -> str:
    """On Windows, stem shortened so the file's whole path stays under MAX_PATH."""
    if not paths.windows():
        return stem
    room = names.room(outdir)
    if room < _WORK_ROOM:
        raise SiphonError(f"The path of {outdir} is too long for Windows - choose a shorter folder.")
    return names.shorten(stem, room - len(".flac"))


def _fetch(ydl: yt_dlp.YoutubeDL, info: dict, cancel: threading.Event | None) -> dict:
    """Download (and convert) an extracted result.

    YouTube refuses roughly one stream URL in six with HTTP 403, in bursts that can outlast a few
    seconds of plain retries (measured 2026-09-25: 1 of 6 downloads refused on all 3 default-client
    tries). So the retries wait longer and alternate with the embedded player, the only other
    yt-dlp client that still gets audio formats without a PO token (default and web_embedded
    worked; tv, web_safari, mweb, android_vr and ios did not).
    """
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


def _was_cancelled(e: DownloadError) -> bool:
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


def _source_info(ydl: yt_dlp.YoutubeDL, track: Track, report: ProgressFn,
                 check: Callable[[], None]) -> dict:
    """The unprocessed extractor result to download: the track itself, or its YouTube match."""
    if not track.query:
        info = ydl.extract_info(track.url, download=False, process=False)
        if not info or "entries" in info or info.get("_type") == "playlist":
            raise SiphonError("This link is a list, not a single track.")
        return info
    report(Progress("matching", None, f"Searching YouTube for {track.query}"))
    primary_artist = track.artist.split(", ")[0]
    found = match.find(ydl, track.query, track.title, primary_artist, track.duration, check)
    if found is None:
        raise SiphonError(f"No good match on YouTube for {_label(track)}.")
    report(Progress("matching", 1.0, f"Matched {found.title}"))
    return found.info or ydl.extract_info(found.url, download=False, process=False)


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
        done = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src),
             "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-frames:v", "1", "-q:v", "2", str(dst)],
            capture_output=True, creationflags=paths.no_window(),
        )
        if done.returncode == 0 and dst.is_file():
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
    """The yt-dlp version loaded in this process."""
    return yt_dlp.version.__version__


def update_engine() -> str:
    """Fetch the newest yt-dlp and return the version the next launch will use.

    The running process keeps the yt-dlp it already imported. A packaged build has no pip, so it
    downloads verified wheels from PyPI (engine.py); a venv install upgrades with pip.
    """
    if paths.bundle_dir() is not None:
        from . import engine

        try:
            return engine.update(engine_version())
        except engine.UpdateError as e:
            raise SiphonError(str(e)) from None
    if sys.prefix == sys.base_prefix:
        raise SiphonError("Siphon isn't running from its own environment - run install.sh update.")
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "--upgrade", "yt-dlp[default]"],
            capture_output=True, text=True, timeout=600, creationflags=paths.no_window(),
        )
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines() or ["pip failed"]
            raise SiphonError(f"Updating yt-dlp failed: {lines[-1][:160]}")
        version = subprocess.run(
            [sys.executable, "-c", "from yt_dlp.version import __version__; print(__version__)"],
            capture_output=True, text=True, timeout=60, creationflags=paths.no_window(),
        )
    except subprocess.TimeoutExpired:
        raise SiphonError("Updating yt-dlp took too long - check your connection.") from None
    return version.stdout.strip()
