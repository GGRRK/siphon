import pytest

from siphon import spotify
from siphon.core import SiphonError, page_meta

ID = "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize("link, expected", [
    (f"https://open.spotify.com/track/{ID}", ("track", ID)),
    (f"https://open.spotify.com/track/{ID}?si=f00ba4", ("track", ID)),
    (f"https://open.spotify.com/intl-de/track/{ID}?si=x", ("track", ID)),
    (f"https://open.spotify.com/intl-pt-BR/album/{ID}", ("album", ID)),
    (f"https://open.spotify.com/embed/playlist/{ID}", ("playlist", ID)),
    (f"https://open.spotify.com/user/someone/playlist/{ID}", ("playlist", ID)),
    (f"spotify:track:{ID}", ("track", ID)),
    (f"spotify:user:someone:playlist:{ID}", ("playlist", ID)),
])
def test_parse_link(link, expected):
    assert spotify.parse_link(link) == expected


@pytest.mark.parametrize("kind", ["artist", "show", "episode"])
def test_unsupported_kinds_name_what_works(kind):
    with pytest.raises(SiphonError, match=f"Spotify {kind} links .* track, album or playlist"):
        spotify.parse_link(f"https://open.spotify.com/{kind}/{ID}")


@pytest.mark.parametrize("link", [
    "https://open.spotify.com/", f"https://open.spotify.com/track/{ID}x", "spotify:track:short",
])
def test_garbage_links(link):
    with pytest.raises(SiphonError):
        spotify.parse_link(link)


def test_handles():
    assert spotify.handles(f"spotify:track:{ID}")
    assert spotify.handles("https://spotify.link/AbCdEf")
    assert spotify.handles(f"https://open.spotify.com/album/{ID}")
    assert not spotify.handles("https://www.youtube.com/watch?v=jNQXAC9IVRw")


def test_track_embed(fixture_text):
    result = spotify.from_entity(spotify.parse_embed(fixture_text("spotify_embed_track.html")))
    assert result.kind == "track" and result.folder is None
    (t,) = result.tracks
    assert (t.title, t.artist, t.source) == ("Never Gonna Give You Up", "Rick Astley", "Spotify")
    assert t.duration == pytest.approx(213.573)
    assert t.url == f"https://open.spotify.com/track/{ID}"
    assert t.query == "Rick Astley - Never Gonna Give You Up"
    assert t.cover_url.endswith("ab67616d0000b273baf89eb11ec7c657805d2da0")  # the 640 px one


def test_album_embed(fixture_text):
    result = spotify.from_entity(spotify.parse_embed(fixture_text("spotify_embed_album.html")))
    assert (result.kind, result.title, result.folder) == (
        "album", "The Dark Side of the Moon", "Pink Floyd - The Dark Side of the Moon")
    assert len(result.tracks) == 10 and result.note == ""
    first, last = result.tracks[0], result.tracks[-1]
    assert (first.title, first.track_no, first.index) == ("Speak to Me", 1, 0)
    assert last.track_no == 10 and last.album == "The Dark Side of the Moon"
    assert all(t.cover_url and t.artist == "Pink Floyd" for t in result.tracks)


def test_playlist_embed(fixture_text):
    entity = spotify.parse_embed(fixture_text("spotify_embed_playlist.html"))
    result = spotify.from_entity(entity)
    assert (result.kind, result.folder) == ("playlist", "Today’s Top Hits")
    assert [t.index for t in result.tracks] == [0, 1, 2]
    assert all(t.album == "" and t.cover_url == "" and t.url for t in result.tracks)
    assert result.note == ""

    entity["trackList"] = entity["trackList"] * 34  # 102 entries: the embed's cap was hit
    assert "first 100 tracks" in spotify.from_entity(entity).note


def test_multi_artist_query_uses_the_first_artist():
    entity = {"type": "playlist", "name": "Mix", "trackList": [
        {"uri": f"spotify:track:{ID}", "title": "Song", "subtitle": "A, B & C", "duration": 1000}]}
    (t,) = spotify.from_entity(entity).tracks
    assert (t.artist, t.query, t.duration) == ("A, B & C", "A - Song", 1.0)


def test_embed_without_data():
    with pytest.raises(SiphonError, match="couldn't read Spotify"):
        spotify.parse_embed("<html>nothing here</html>")


def test_track_page_meta(fixture_text):
    meta = page_meta(fixture_text("spotify_track_page.html"))
    assert spotify.album_from_meta(meta) == ("Whenever You Need Somebody", 1)
    assert spotify.album_from_meta({"og:description": "Artist · Song · 2020"}) == ("", None)
