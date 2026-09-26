"""Spotify links -> track lists, read from Spotify's public embed pages (no API key needed).

Spotify only provides metadata; the audio is matched on YouTube at download time.
"""

import json
import re
import urllib.parse
from dataclasses import replace

from .core import Resolved, SiphonError, Track, http_get, page_meta, safe_name

KINDS = ("track", "album", "playlist")
EMBED_CAP = 100  # the embed page lists at most this many tracks
_WEB = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(?:user/[^/]+/)?"
    r"([a-z]+)/([A-Za-z0-9]{22})(?![A-Za-z0-9])"
)
_URI = re.compile(r"spotify:(?:user:[^:]+:)?([a-z]+):([A-Za-z0-9]{22})")
_SHORT_HOSTS = ("spotify.link", "spotify.app.link")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
# Spotify gives link-preview bots plain HTML with og:/music: tags (album name, track number,
# cover) and short links a direct redirect; browsers get a script-only shell instead.
_PREVIEW_UA = "Siphon/0.1 (link preview)"
_ONLY = "paste a track, album or playlist link"


def handles(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return url.startswith("spotify:") or host == "open.spotify.com" or host in _SHORT_HOSTS


def parse_link(url: str) -> tuple[str, str]:
    """(kind, id) for a Spotify web link or URI; SiphonError for anything else."""
    m = _URI.fullmatch(url.strip()) or _WEB.match(url.strip())
    if not m:
        raise SiphonError(f"Siphon doesn't recognise this Spotify link - {_ONLY}.")
    kind, item = m.groups()
    if kind not in KINDS:
        raise SiphonError(f"Spotify {kind} links can't be downloaded - {_ONLY}.")
    return kind, item


def resolve(url: str) -> Resolved:
    if urllib.parse.urlsplit(url).hostname in _SHORT_HOSTS:
        url = _expand(url)
    kind, item = parse_link(url)
    _, body = http_get(f"https://open.spotify.com/embed/{kind}/{item}", what=f"that Spotify {kind}")
    result = from_entity(parse_embed(body.decode("utf-8", "replace")))
    if kind == "track":
        result.tracks[0] = enrich(result.tracks[0])
    result.link = f"https://open.spotify.com/{kind}/{item}"
    return result


def _expand(url: str) -> str:
    final, body = http_get(url, ua=_PREVIEW_UA, what="that Spotify link")
    if _WEB.match(final):
        return final
    if m := _WEB.search(body.decode("utf-8", "replace")):
        return m.group(0)
    raise SiphonError(f"This Spotify short link leads nowhere Siphon can use - {_ONLY}.")


def parse_embed(html_text: str) -> dict:
    """The entity (track, album or playlist) inside an embed page's Next.js data."""
    try:
        data = json.loads(_NEXT_DATA.search(html_text).group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SiphonError(
            "Siphon couldn't read Spotify's page - Spotify may have changed it."
        ) from None
    if not isinstance(entity, dict):
        raise SiphonError("Spotify returned nothing for this link - check it.")
    return entity


def from_entity(entity: dict) -> Resolved:
    kind = entity.get("type")
    name = entity.get("name") or entity.get("title") or ""
    cover = _largest_image(entity)
    if kind == "track":
        artists = ", ".join(a["name"] for a in entity.get("artists") or [] if a.get("name"))
        track = _track(entity.get("uri") or "", name, artists, entity.get("duration"), cover)
        return Resolved(title=f"{artists} - {name}" if artists else name, kind="track",
                        tracks=[track], folder=None)
    if kind not in ("album", "playlist"):
        raise SiphonError(f"Spotify {kind} links can't be downloaded - {_ONLY}.")

    items = [t for t in entity.get("trackList") or [] if t.get("entityType", "track") == "track"]
    tracks = []
    for i, item in enumerate(items):
        track = _track(item.get("uri") or "", item.get("title") or "", item.get("subtitle") or "",
                       item.get("duration"), cover if kind == "album" else "")
        track.index = i
        if kind == "album":
            track.album, track.track_no = name, i + 1
        tracks.append(track)
    if not tracks:
        raise SiphonError(f"This Spotify {kind} has no tracks Siphon can see.")
    owner = entity.get("subtitle") or ""
    folder = f"{owner} - {name}" if kind == "album" and owner else name
    note = ""
    if len(entity.get("trackList") or []) >= EMBED_CAP:
        note = f"Spotify only shows the first {EMBED_CAP} tracks of this {kind}."
    return Resolved(title=name, kind=kind, tracks=tracks, folder=safe_name(folder), note=note, cover_url=cover)


def _track(uri: str, title: str, artists: str, duration_ms: int | None, cover: str) -> Track:
    track_id = uri.removeprefix("spotify:track:") if uri.startswith("spotify:track:") else ""
    return Track(
        url=f"https://open.spotify.com/track/{track_id}" if track_id else "",
        title=title,
        artist=artists,
        duration=duration_ms / 1000 if duration_ms else None,
        cover_url=cover,
        source="Spotify",
        query=f"{artists.split(', ')[0]} - {title}" if artists else title,
    )


def _largest_image(entity: dict) -> str:
    images = (entity.get("visualIdentity") or {}).get("image") or []
    images = images or (entity.get("coverArt") or {}).get("sources") or []
    best = max(images, key=lambda i: i.get("maxWidth") or i.get("width") or 0, default=None)
    return best.get("url", "") if best else ""


def album_from_meta(meta: dict[str, str]) -> tuple[str, int | None]:
    """(album, track number) from a track page's tags; og:description reads
    "Artist · Album · Song · Year"."""
    parts = meta.get("og:description", "").split(" · ")
    album = " · ".join(parts[1:-2]) if len(parts) >= 4 else ""
    number = meta.get("music:album:track", "")
    return album, int(number) if number.isdigit() else None


def enrich(track: Track) -> Track:
    """Fill a Spotify track's missing album, track number and cover from its page.

    Best effort: playlist entries arrive without them; failures leave the track as it is.
    """
    if track.source != "Spotify" or not track.url or (track.album and track.cover_url):
        return track
    try:
        _, body = http_get(track.url, ua=_PREVIEW_UA)
    except SiphonError:
        return track
    meta = page_meta(body.decode("utf-8", "replace"))
    album, number = album_from_meta(meta)
    return replace(track, album=track.album or album, track_no=track.track_no or number,
                   cover_url=track.cover_url or meta.get("og:image", ""))
