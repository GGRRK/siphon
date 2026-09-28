"""A list link's songs: each listed by its own name, in the list's order, and downloaded to a file of its own.

yt-dlp reads lists flat, and a flat SoundCloud set names none of its songs (no title, no artist, one of them only an
api-v2 address): Siphon 0.1.2 listed every one as "Untitled" and saved only the first, as "Untitled.opus", taking
each later song for "already downloaded". Everything here runs on recorded yt-dlp results (tests/fake_ytdlp.py)."""

import threading
import time

import pytest
from fake_ytdlp import DRM, FakeYdl, fetch, song_page, soundcloud_set
from yt_dlp.utils import DownloadError, ExtractorError

from siphon import core, match
from siphon.core import SiphonError, Track

SET = "https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep"
EP = [  # the set's own order, which SoundCloud's api-v2 answer does not keep
    ("75206121", "World On Fire (Re-Mastered)", "https://soundcloud.com/the-concept-band/world-on-fire-1"),
    ("47127625", "Gimme Twice", "https://soundcloud.com/the-concept-band/gimme-twice-mastered"),
    ("47127627", "Goldrushed (Re-Mastered)", "https://soundcloud.com/the-concept-band/goldrushed-mastered"),
    ("30510138", "D-D-Dance", "https://soundcloud.com/the-concept-band/the-concept-d-d-dance"),
    ("47127629", "In The End", "https://soundcloud.com/the-concept-band/in-the-end-mastered"),
    ("47127631", "Knocked Up", "https://soundcloud.com/the-concept-band/knocked-up-mastered"),
]


def resolve(monkeypatch, fake: FakeYdl, url: str = SET) -> core.Resolved:
    monkeypatch.setattr(core, "_ydl", fake)
    return core.resolve(url)


# ---------------------------------------------------------------- listing


def test_a_soundcloud_set_lists_every_song_by_its_own_name_in_order(monkeypatch):
    flat, api = soundcloud_set()
    assert not any(e.get("title") for e in flat["entries"])  # what the bug came from
    fake = FakeYdl(flat=flat, api=api)
    found = resolve(monkeypatch, fake)
    assert [(t.title, t.url) for t in found.tracks] == [(title, url) for _id, title, url in EP]
    assert {t.artist for t in found.tracks} == {"The Royal Concept"}
    assert [t.index for t in found.tracks] == list(range(6))
    assert all(not t.error and t.source == "SoundCloud" for t in found.tracks)
    # the set is an EP: its songs carry it as their album, numbered in its order; its link still makes a playlist
    assert (found.kind, found.title) == ("playlist", "The Royal Concept EP")
    assert [(t.album, t.track_no) for t in found.tracks] == [("The Royal Concept EP", n) for n in range(1, 7)]
    assert found.cover_url == "https://i1.sndcdn.com/artworks-000030896212-o16m9v-original.jpg"
    assert found.tracks[1].duration == pytest.approx(205.193)
    assert found.tracks[1].cover_url == "https://i1.sndcdn.com/artworks-000043574502-266jbv-t500x500.jpg"
    # one api-v2 request for the whole set, no song read by itself
    assert fake.requests == [[song_id for song_id, _title, _url in EP]] and fake.reads == []
    assert len({core.file_stem(t) for t in found.tracks}) == 6


def test_a_song_soundcloud_does_not_share_is_listed_in_its_place_with_why(monkeypatch):
    flat, api = soundcloud_set()
    found = resolve(monkeypatch, FakeYdl(flat=flat, api=[t for t in api if t["id"] != 47127627]))
    assert [t.title for t in found.tracks] == [title if song_id != "47127627" else "" for song_id, title, _ in EP]
    gone = found.tracks[2]
    assert (gone.error, core.label(gone)) == (core.GONE, EP[2][2])
    assert [t.error for t in found.tracks].count("") == 5


def test_a_song_listed_with_why_fails_at_once_with_it(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_ydl", lambda opts: pytest.fail("a song known to be gone is not read"))
    with pytest.raises(SiphonError, match="private or was deleted"):
        core.download(Track(url=EP[2][2], title="", source="SoundCloud", error=core.GONE), tmp_path, "opus")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [ExtractorError("HTTP Error 429: Too Many Requests"), KeyError("collection")])
def test_without_the_batch_each_song_is_read_by_itself_keeping_the_order(monkeypatch, error):
    flat, _api = soundcloud_set()
    links = [e["url"] for e in flat["entries"]]  # the last is an api-v2 address: read as it is
    pages = {link: song_page(song_id, f"The Royal Concept - {title}", webpage_url=url)
             for link, (song_id, title, url) in zip(links, EP)}
    pages[links[4]] = DownloadError("ERROR: [soundcloud] 47127629: This video is DRM protected")
    fake = FakeYdl(flat=flat, pages=pages, api_error=error)
    # later songs answer first: the list still comes out in the set's order
    reading = FakeYdl.__call__

    def slow_first(self, opts):
        session = reading(self, opts)
        read = session.extract_info

        def extract_info(url, download=False, process=True):
            if not process:
                time.sleep(0.05 * (len(links) - links.index(url)))
            return read(url, download, process)

        session.extract_info = extract_info
        return session

    monkeypatch.setattr(FakeYdl, "__call__", slow_first)
    found = resolve(monkeypatch, fake)
    assert len(fake.requests) == 1  # tried once, then song by song
    assert [t.title for t in found.tracks] == [title if n != 4 else "" for n, (_i, title, _u) in enumerate(EP)]
    assert [t.artist for t in found.tracks] == ["The Royal Concept"] * 4 + [""] + ["The Royal Concept"]
    assert found.tracks[5].url == EP[5][2]  # its own address, not the api-v2 one
    # the one that could not be read keeps its link; its download reads it again and says what is wrong
    assert (found.tracks[4].url, found.tracks[4].error) == (EP[4][2], "")
    assert sorted(fake.reads) == sorted(links)


def test_big_sets_are_read_50_songs_a_request(monkeypatch):
    entries = [{"_type": "url_transparent", "ie_key": "Soundcloud", "id": str(n),
                "url": f"https://api-v2.soundcloud.com/tracks/{n}"} for n in range(1, 121)]
    api = [{"id": n, "title": f"Song {n}", "permalink_url": f"https://soundcloud.com/dj/song-{n}", "duration": 1000 * n,
            "user": {"id": 7, "username": "DJ", "permalink_url": "https://soundcloud.com/dj"}}
           for n in reversed(range(1, 121))]
    fake = FakeYdl(flat={"title": "Long mix", "extractor_key": "SoundcloudSet", "entries": entries}, api=api)
    found = resolve(monkeypatch, fake)
    assert [len(ids) for ids in fake.requests] == [50, 50, 20]
    assert [core.label(t) for t in found.tracks] == [f"DJ - Song {n}" for n in range(1, 121)]
    assert found.tracks[0].cover_url == "" and found.cover_url == ""  # no artwork anywhere: no picture, no request


def test_soundcloud_lists_without_artists_get_theirs(monkeypatch):
    # a user's track page names its songs but not who made them
    entries = [{"_type": "url", "ie_key": "Soundcloud", "id": "47127625", "title": "Gimme Twice",
                "url": "https://soundcloud.com/the-concept-band/gimme-twice-mastered"}]
    _flat, api = soundcloud_set()
    found = resolve(monkeypatch, FakeYdl(flat={"title": "The Royal Concept (Tracks)", "extractor_key": "SoundcloudUser",
                                               "entries": entries}, api=api))
    assert core.label(found.tracks[0]) == "The Royal Concept - Gimme Twice"


def test_lists_that_name_their_songs_are_not_read_again(monkeypatch):
    monkeypatch.setattr(core, "_require_js", lambda: None)
    entries = [{"_type": "url", "ie_key": "Youtube", "title": f"Song {n}", "channel": "Band - Topic",
                "url": f"https://www.youtube.com/watch?v=abcdefghij{n}"} for n in range(3)]
    fake = FakeYdl(flat={"title": "Mix", "extractor_key": "YoutubeTab", "entries": entries})
    found = resolve(monkeypatch, fake, "https://www.youtube.com/playlist?list=PLx")
    assert [core.label(t) for t in found.tracks] == ["Band - Song 0", "Band - Song 1", "Band - Song 2"]
    assert fake.reads == [] and fake.requests == [] and len(fake.sessions) == 1


def test_any_sites_songs_listed_without_a_title_are_read(monkeypatch):
    entries = [{"_type": "url", "ie_key": "Bandcamp", "url": "https://band.bandcamp.com/track/one"},
               {"_type": "url", "ie_key": "Bandcamp", "url": "https://band.bandcamp.com/track/two", "title": "Two"}]
    page = {"id": "1", "title": "One", "artist": "Band", "extractor_key": "Bandcamp",
            "webpage_url": "https://band.bandcamp.com/track/one"}
    fake = FakeYdl(flat={"title": "Album", "extractor_key": "BandcampAlbum", "entries": entries},
                   pages={"https://band.bandcamp.com/track/one": page})
    found = resolve(monkeypatch, fake, "https://band.bandcamp.com/album/x")
    assert [(t.title, t.artist) for t in found.tracks] == [("One", "Band"), ("Two", "")]
    # only the song without a title is read at once; "Two" is named in full when it downloads
    assert fake.reads == ["https://band.bandcamp.com/track/one"]


def test_a_plain_playlists_title_is_not_its_songs_album(monkeypatch):
    flat, api = soundcloud_set()
    flat = {**flat, "album_type": "playlist", "title": "My Mix",
            "entries": [{**e, "album": "My Mix", "album_type": "playlist"} for e in flat["entries"]]}
    found = resolve(monkeypatch, FakeYdl(flat=flat, api=api))
    assert [(t.album, t.track_no) for t in found.tracks] == [("", None)] * 6


def test_youtube_says_how_many_videos_it_left_out(monkeypatch):
    monkeypatch.setattr(core, "_require_js", lambda: None)
    entries = [{"title": "[Private video]", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "ie_key": "Youtube"},
               {"title": "Song", "channel": "Band", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
                "ie_key": "Youtube"},
               {"title": "[Deleted video]", "url": "https://www.youtube.com/watch?v=ccccccccccc", "ie_key": "Youtube"}]
    fake = FakeYdl(flat={"title": "Mix", "extractor_key": "YoutubeTab", "entries": entries})
    found = resolve(monkeypatch, fake, "https://www.youtube.com/playlist?list=PLx")
    assert [t.title for t in found.tracks] == ["Song"]
    assert found.note == "2 private or deleted videos left out."
    assert fake.reads == []


def test_a_locked_soundcloud_link_alone_is_named_by_soundcloud_and_found_on_youtube(monkeypatch):
    url = EP[0][2]
    page = song_page("75206121", "World On Fire (Re-Mastered)", webpage_url=url, formats=[], duration=199.889)
    found = resolve(monkeypatch, FakeYdl(flat=DownloadError(DRM), pages={url: page}, locked=(url,)), url)
    track = found.tracks[0]
    assert (found.kind, found.title) == ("track", "The Royal Concept - World On Fire (Re-Mastered)")
    assert (track.query, track.duration, track.url) == ("The Royal Concept - World On Fire (Re-Mastered)", 199.889, url)


# ---------------------------------------------------------------- downloading


@pytest.fixture
def downloads(monkeypatch):
    """core.download against a FakeYdl (set .pages etc. on it): the files written, the songs they were tagged as."""
    fake = FakeYdl()
    tagged: list[Track] = []
    monkeypatch.setattr(core, "_ydl", fake)
    monkeypatch.setattr(core, "_fetch", fetch)
    monkeypatch.setattr(core, "_cover", lambda urls, work: None)
    monkeypatch.setattr(core, "_tag", lambda path, track, info, cover: tagged.append(track))
    fake.tagged = tagged
    return fake


def test_songs_a_list_did_not_name_get_files_of_their_own(downloads, tmp_path):
    (tmp_path / "Untitled.opus").write_bytes(b"another song")  # what 0.1.2 left: never taken for these
    downloads.pages = {url: song_page(song_id, title, webpage_url=url) for song_id, title, url in EP[:2]}
    songs = [Track(url=url, title="", source="SoundCloud") for _i, _t, url in EP[:2]]
    seen = []
    files = [core.download(t, tmp_path, "opus", seen.append) for t in songs]
    assert [f.name for f in files] == ["The Royal Concept - World On Fire (Re-Mastered).opus",
                                       "The Royal Concept - Gimme Twice.opus"]
    assert [(t.title, t.artist) for t in downloads.tagged] == [
        ("World On Fire (Re-Mastered)", "The Royal Concept"), ("Gimme Twice", "The Royal Concept")]
    assert not any(p.detail == "already downloaded" for p in seen)
    assert (tmp_path / "Untitled.opus").read_bytes() == b"another song"
    # the same song again is found under its own name, once its page is read
    seen.clear()
    assert core.download(songs[1], tmp_path, "opus", seen.append) == files[1]
    assert seen[-1].detail == "already downloaded" and len(downloads.tagged) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([f.name for f in files] + ["Untitled.opus"])


def test_a_song_named_nowhere_is_named_by_its_site_and_id(downloads, tmp_path):
    urls = ["https://api-v2.soundcloud.com/tracks/1", "https://api-v2.soundcloud.com/tracks/2"]
    downloads.pages = {url: song_page(url[-1], None, uploader="") for url in urls}
    files = [core.download(Track(url=url, title="", source="SoundCloud"), tmp_path, "opus") for url in urls]
    assert [f.name for f in files] == ["SoundCloud 1.opus", "SoundCloud 2.opus"]


def test_a_song_listed_without_its_artist_is_named_in_full(downloads, tmp_path):
    (tmp_path / "Gimme Twice.opus").write_bytes(b"someone else's Gimme Twice")
    url = EP[1][2]
    downloads.pages = {url: song_page("47127625", "The Royal Concept - Gimme Twice", webpage_url=url)}
    path = core.download(Track(url=url, title="Gimme Twice", source="SoundCloud"), tmp_path, "opus")
    assert path.name == "The Royal Concept - Gimme Twice.opus"


def test_a_locked_song_of_a_list_is_found_on_youtube(downloads, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_require_js", lambda: None)
    url = EP[0][2]
    video = {"id": "vid", "title": "The Royal Concept - World On Fire", "webpage_url": "https://youtu.be/vid"}
    searched = []

    def find(ydl, query, title, artist, duration, check):
        searched.append((query, title, artist, duration))
        return match.Candidate(url="https://youtu.be/vid", title=video["title"], info=video)

    monkeypatch.setattr(match, "find", find)
    downloads.locked = (url,)
    song = Track(url=url, title="World On Fire (Re-Mastered)", artist="The Royal Concept", duration=199.889,
                 cover_url="https://i1.sndcdn.com/artworks-acRKqXJcJGVN-0-t500x500.jpg", source="SoundCloud",
                 album="The Royal Concept EP", track_no=1)
    path = core.download(song, tmp_path, "opus")
    assert path.name == "The Royal Concept - World On Fire (Re-Mastered).opus"
    assert searched == [("The Royal Concept - World On Fire (Re-Mastered)", "World On Fire (Re-Mastered)",
                         "The Royal Concept", 199.889)]
    tagged = downloads.tagged[0]
    assert (tagged.title, tagged.artist, tagged.album, tagged.track_no) == (
        "World On Fire (Re-Mastered)", "The Royal Concept", "The Royal Concept EP", 1)
    assert core._cover_urls(tagged, video) == [song.cover_url]  # SoundCloud's artwork, not the video's


def test_a_song_offered_only_as_a_preview_is_not_saved(downloads, tmp_path):
    url = "https://soundcloud.com/the-concept-band/shadows-in-the-night"
    downloads.pages = {url: song_page("1", "Shadows in the Night", webpage_url=url, formats=[
        {"format_id": "hls_aac_160k_preview"}, {"format_id": "http_mp3_128_preview"}])}
    with pytest.raises(SiphonError, match="30-second preview"):
        core.download(Track(url=url, title="Shadows in the Night", artist="The Royal Concept"), tmp_path, "opus")
    assert list(tmp_path.iterdir()) == []


def test_a_store_song_without_a_name_is_not_searched_for(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_require_js", lambda: None)
    monkeypatch.setattr(core, "_ydl", lambda opts: pytest.fail("nothing to search for"))
    (tmp_path / "Untitled.opus").write_bytes(b"x")
    with pytest.raises(SiphonError, match="doesn't give this song's name"):
        core.download(Track(url="", title="", artist="Band", query="Band - ", source="Spotify"), tmp_path, "opus")


def test_the_whole_set_downloads_to_six_files(downloads, tmp_path, monkeypatch):
    flat, api = soundcloud_set()
    downloads.flat, downloads.api = flat, api
    downloads.pages = {url: song_page(song_id, title, webpage_url=url) for song_id, title, url in EP}
    found = core.resolve(SET)
    files = [core.download(t, tmp_path, "opus") for t in found.tracks]
    assert len(set(files)) == 6 and all(f.is_file() for f in files)
    assert [f.name for f in files] == [f"The Royal Concept - {title}.opus" for _i, title, _u in EP]
    assert [(t.title, t.track_no) for t in downloads.tagged] == [(title, n) for n, (_i, title, _u) in enumerate(EP, 1)]


def test_reading_songs_one_by_one_runs_a_few_at_a_time(monkeypatch):
    entries = [{"_type": "url", "ie_key": "Generic", "url": f"https://example.org/{n}.mp3"} for n in range(20)]
    running, most = [0], [0]
    lock = threading.Lock()

    def page(url):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1
        return {"id": url, "title": url.rsplit("/", 1)[1], "webpage_url": url}

    fake = FakeYdl(flat={"title": "Files", "extractor_key": "Generic", "entries": entries})
    reading = FakeYdl.__call__

    def counted(self, opts):
        session = reading(self, opts)
        session.extract_info = lambda url, download=False, process=True: (
            page(url) if not process else reading(self, opts).extract_info(url, download, process))
        return session

    monkeypatch.setattr(FakeYdl, "__call__", counted)
    found = resolve(monkeypatch, fake, "https://example.org/files")
    assert [t.title for t in found.tracks] == [f"{n}.mp3" for n in range(20)]
    assert 1 < most[0] <= core._READERS
