"""Title clean-up for uploads, and picking the YouTube upload that is the same recording as a
track known only by its metadata (a Spotify or store link)."""

import re
import unicodedata
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

YTM_SEARCH = "https://music.youtube.com/search?q={}#songs"
THRESHOLD = 0.6  # lowest score accepted as a match
CONFIDENT = 1.0  # a YouTube Music song this good ends the search early
MAX_DRIFT = 20  # seconds; longer differences are rejected while a closer candidate exists

_DECOR_WORDS = (
    r"official|officiel|oficial|music|lyrics?|video|videoclip|audio|visuali[sz]er|clip"
    r"|hd|hq|4k|uhd|full|1080p|720p|with|only|m/v|mv"
)
_DECORATION = re.compile(rf"(?:(?:{_DECOR_WORDS})\b[\s/-]*)+", re.I)
_BRACKETED = re.compile(r"\s*[(\[【]([^)\]】]*)[)\]】]")
_TRAILER = re.compile(r"\s*\|\s*([^|]*)$")
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_VERSIONS = re.compile(
    r"\b(live|cover|remix|sped[\s-]*up|slowed|8d|karaoke|instrumental|nightcore)\b", re.I
)
# "(Re-Mastered)", "(2022 Remaster)", " - Remastered 2009": the same recording, a note store and SoundCloud titles
# carry and most uploads leave out; it is not asked of an upload (one that has it still scores a little lower).
_REMASTER = re.compile(
    r"\s*(?:[(\[][^)\]]*\bre-?master(?:ed)?\b[^)\]]*[)\]]|\s[-–—]\s[^-–—]*\bre-?master(?:ed)?\b[^-–—]*$)", re.I
)
_NOISE = {
    "the", "a", "an", "and", "feat", "ft", "featuring", "with", "x", "official", "video",
    "audio", "lyrics", "lyric", "topic", "vevo", "hd", "music", "prod", "by",
}


def clean_title(title: str) -> str:
    """Drop upload decorations such as "(Official Video)", "[Lyrics]" or "| HD"."""
    text = _BRACKETED.sub(
        lambda m: "" if _DECORATION.fullmatch(m.group(1).strip()) else m.group(0), title
    )
    while (m := _TRAILER.search(text)) and _DECORATION.fullmatch(m.group(1).strip()):
        text = text[: m.start()]
    return " ".join(text.split()).strip(" -–—|")


def split_artist_title(title: str) -> tuple[str, str]:
    """"Artist - Title" -> (artist, title); ("", title) when there is no separator."""
    parts = _SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", title.strip()


def channel_artist(info: dict) -> str:
    """The uploader as an artist name: "Artist - Topic" and "ArtistVEVO" become "Artist"."""
    name = re.sub(r"\s+-\s+Topic$", "", info.get("channel") or info.get("uploader") or "")
    if len(name) > 4 and name.upper().endswith("VEVO"):
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name[:-4]).strip()
    return name


def describe(info: dict) -> tuple[str, str]:
    """(title, artist) for a yt-dlp result: music metadata when the site has it (YouTube
    Music, SoundCloud, Bandcamp), else "Artist - Title" from the upload title, else the
    uploader."""
    artist = ", ".join(info.get("artists") or []) or info.get("artist") or ""
    track = info.get("track")
    # SoundCloud copies the upload title into "track"; that is not music metadata.
    copied = track == info.get("title") and split_artist_title(track or "")[0]
    if track and not copied:
        return track, artist or channel_artist(info)
    raw = clean_title(info.get("title") or "")
    uploader = channel_artist(info)
    first, second = split_artist_title(raw)
    if not first:
        return raw, artist or uploader
    # Some uploads read "Title - Artist": trust the order unless the uploader says otherwise.
    if tokens(uploader) & tokens(second) and not tokens(uploader) & tokens(first):
        first, second = second, first
    return second, artist or first


def tokens(text: str) -> set[str]:
    """Comparable words: case- and accent-folded, filler words dropped (unless that is all)."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("&", " and "))
    words = set(re.findall(r"\w+", "".join(c for c in text if not unicodedata.combining(c))))
    return (words - _NOISE) or words


def versions(text: str) -> set[str]:
    """Alternate-version markers in a title: live, remix, sped up..."""
    return {re.sub(r"[\s-]+", " ", v.lower()) for v in _VERSIONS.findall(text)}


@dataclass
class Candidate:
    url: str
    title: str
    artist: str = ""  # artist metadata or channel name
    duration: float | None = None
    official: bool = False  # a YouTube Music song, a "- Topic"/VEVO channel or the artist's own
    info: dict | None = None  # the full extractor result, once fetched


def score(c: Candidate, title: str, artist: str, duration: float | None) -> float | None:
    """How likely c is the recording (title, artist, duration); None when it clearly isn't."""
    want_title, want_artist = tokens(_REMASTER.sub("", title) or title), tokens(artist) if artist else set()
    text = tokens(clean_title(c.title))
    title_sim = len(want_title & text) / len(want_title) if want_title else 0.0
    if title_sim < 0.5:
        return None
    artist_sim = 1.0
    if want_artist:
        artist_sim = len(want_artist & (text | tokens(c.artist))) / len(want_artist)
    if artist_sim < 0.5:
        return None
    extra = len(text - want_title - want_artist) / max(len(text), 1)
    if duration and c.duration:
        closeness = max(0.0, 1 - abs(c.duration - duration) / MAX_DRIFT)
    else:
        closeness = 0.5
    s = 0.45 * title_sim + 0.25 * artist_sim + 0.3 * closeness - 0.15 * extra
    if c.official:
        s += 0.1
    if versions(c.title) - versions(title):
        s -= 0.6
    return s


def close(c: Candidate, duration: float | None) -> bool:
    """Within MAX_DRIFT of the wanted length, or either length unknown."""
    return not duration or not c.duration or abs(c.duration - duration) <= MAX_DRIFT


def choose(cands: list[Candidate], title: str, artist: str,
           duration: float | None) -> Candidate | None:
    if any(close(c, duration) and c.duration for c in cands):
        cands = [c for c in cands if close(c, duration)]
    scored = [(s, c) for c in cands if (s := score(c, title, artist, duration)) is not None]
    best = max(scored, key=lambda sc: sc[0], default=None)
    return best[1] if best and best[0] >= THRESHOLD else None


def is_official(channel: str, artist: str) -> bool:
    if channel.endswith(" - Topic") or channel.upper().endswith("VEVO"):
        return True
    return bool(artist) and tokens(channel) == tokens(artist)


def find(ydl, query: str, title: str, artist: str, duration: float | None,
         check: Callable[[], None]) -> Candidate | None:
    """Search YouTube Music songs, then plain YouTube, for the recording.

    ydl must be created with extract_flat="in_playlist". YouTube Music search results carry
    only a title, so the most promising few are fetched in full to learn artist and length.
    """
    from yt_dlp.utils import DownloadError  # here, not at the top: see core.py

    cands: list[Candidate] = []
    songs = _search(ydl, YTM_SEARCH.format(urllib.parse.quote(query)), artist, from_ytm=True)
    ranked = sorted(
        ((s, c) for c in songs if (s := score(c, title, "", None)) is not None),
        key=lambda sc: -sc[0],
    )
    for _, c in ranked[:3]:
        check()
        try:
            full = ydl.extract_info(c.url, download=False, process=False)
        except DownloadError:
            continue
        c.title = full.get("track") or full.get("title") or c.title
        c.artist = describe(full)[1]
        c.duration, c.info = full.get("duration"), full
        cands.append(c)
        if (s := score(c, title, artist, duration)) is not None and s >= CONFIDENT:
            return c
    best = choose(cands, title, artist, duration)
    if best and close(best, duration):
        return best
    check()
    cands += _search(ydl, f"ytsearch6:{query}", artist, from_ytm=False)
    return choose(cands, title, artist, duration)


def _search(ydl, url: str, artist: str, from_ytm: bool) -> list[Candidate]:
    from yt_dlp.utils import DownloadError

    try:
        result = ydl.extract_info(url, download=False)
    except DownloadError:
        return []
    cands = []
    for e in list((result or {}).get("entries") or [])[:6]:
        if not e or not e.get("url"):
            continue
        channel = e.get("channel") or e.get("uploader") or ""
        cands.append(Candidate(
            url=e["url"], title=e.get("title") or "", artist=channel,
            duration=e.get("duration"), official=from_ytm or is_official(channel, artist),
        ))
    return cands
