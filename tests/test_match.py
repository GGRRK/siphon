import urllib.parse

import pytest

from siphon.match import (
    YTM_SEARCH, Candidate, channel_artist, choose, clean_title, describe, find, score,
    split_artist_title, tokens,
)


@pytest.mark.parametrize("raw, clean", [
    ("Never Gonna Give You Up (Official Video)", "Never Gonna Give You Up"),
    ("Song [Official Audio]", "Song"),
    ("Song (Lyrics)", "Song"),
    ("Song (Official Lyric Video)", "Song"),
    ("Song (Visualizer)", "Song"),
    ("Song (Official Music Video) | HD", "Song"),
    ("Song | Official Video", "Song"),
    ("Song (Official Video) (4K Remaster)", "Song (4K Remaster)"),
    ("Song (feat. Someone) [Official Video]", "Song (feat. Someone)"),
    ("Song (Live at Wembley)", "Song (Live at Wembley)"),
    ("Song (Remix)", "Song (Remix)"),
])
def test_clean_title(raw, clean):
    assert clean_title(raw) == clean


def test_split_artist_title():
    assert split_artist_title("Rick Astley - Never Gonna Give You Up") == (
        "Rick Astley", "Never Gonna Give You Up")
    assert split_artist_title("Artist – Title – Part 2") == ("Artist", "Title – Part 2")
    assert split_artist_title("Me at the zoo") == ("", "Me at the zoo")
    assert split_artist_title("Up-Tempo") == ("", "Up-Tempo")


@pytest.mark.parametrize("info, expected", [
    ({"track": "Song", "artists": ["A", "B"], "title": "whatever"}, ("Song", "A, B")),
    ({"track": "Song", "channel": "Band - Topic"}, ("Song", "Band")),
    ({"title": "Artist - Song (Official Video)", "channel": "Label"}, ("Song", "Artist")),
    ({"title": "Song [Official Audio]", "channel": "Artist - Topic"}, ("Song", "Artist")),
    ({"title": "Song (Official Video)", "channel": "RickAstleyVEVO"}, ("Song", "Rick Astley")),
    ({"title": "Me at the zoo", "uploader": "jawed"}, ("Me at the zoo", "jawed")),
    ({"title": "BELLAKEO (Video Oficial) - Peso Pluma, Anitta", "channel": "Peso Pluma"},
     ("BELLAKEO", "Peso Pluma, Anitta")),
    ({"title": "Artist - Song", "channel": "Artist"}, ("Song", "Artist")),
    ({"title": "Artist - Song", "track": "Artist - Song", "uploader": "Label"}, ("Song", "Artist")),
    ({"title": "Song", "track": "Song", "uploader": "Band"}, ("Song", "Band")),
])
def test_describe(info, expected):
    assert describe(info) == expected


def test_channel_artist_keeps_plain_names():
    assert channel_artist({"channel": "VEVO"}) == "VEVO"
    assert channel_artist({"uploader": "Some Label"}) == "Some Label"


def test_tokens_fold_case_accents_and_filler():
    assert tokens("Beyoncé & JAY-Z (feat. Someone)") == {"beyonce", "jay", "z", "someone"}
    assert tokens("The Music") == {"the", "music"}  # all filler: keep it rather than nothing


TITLE, ARTIST, LENGTH = "Never Gonna Give You Up", "Rick Astley", 213.6


def c(title, artist="", duration=213.0, official=False):
    return Candidate(url=f"https://youtu.be/{title}", title=title, artist=artist,
                     duration=duration, official=official)


def best(*cands):
    return choose(list(cands), TITLE, ARTIST, LENGTH)


def test_official_song_beats_remaster_lyrics_and_live():
    song = c("Never Gonna Give You Up", "Rick Astley", 214, official=True)
    assert best(
        c("Never Gonna Give You Up (2022 Remaster)", "Rick Astley", 214, official=True),
        c("Rick Astley - Never Gonna Give You Up", "Amazing Lyrics", 214),
        c("Rick Astley - Never Gonna Give You Up | Glastonbury 2023", "BBC Music", 560),
        song,
    ) is song


@pytest.mark.parametrize("version", [
    "Remix", "Live", "Cover", "Sped Up", "sped-up", "Slowed + Reverb", "8D Audio", "Karaoke",
    "Instrumental", "Nightcore",
])
def test_alternate_versions_are_penalised(version):
    plain = c("Rick Astley - Never Gonna Give You Up", "Some Channel", 213)
    alt = c(f"Never Gonna Give You Up ({version})", "Rick Astley", 213, official=True)
    assert score(alt, TITLE, ARTIST, LENGTH) < score(plain, TITLE, ARTIST, LENGTH)
    assert best(alt, plain) is plain
    assert best(alt) is None


@pytest.mark.parametrize("wanted", ["Goldrushed (Re-Mastered)", "Goldrushed - Remastered 2009",
                                    "Goldrushed [2022 Remaster]", "Goldrushed"])
def test_a_remaster_note_in_the_source_title_does_not_hide_the_song(wanted):
    upload = c("The Royal Concept - Goldrushed", "Fernet Nero 53", 228)
    assert choose([upload], wanted, "The Royal Concept", 227.1) is upload
    remaster = c("The Royal Concept - Goldrushed (Remastered)", "Fernet Nero 53", 228)
    assert score(remaster, wanted, "The Royal Concept", 227.1) < score(upload, wanted, "The Royal Concept", 227.1)


def test_a_title_that_is_only_a_remaster_note_is_still_matched_on():
    song = c("Band - Remastered", "Band", 200)
    assert choose([song, c("Band - Other Song", "Band", 200)], "Remastered", "Band", 200) is song


def test_version_word_in_the_source_title_is_not_penalised():
    live = c("Song (Live)", "Band", 200, official=True)
    assert choose([live], "Song (Live)", "Band", 200) is live


def test_duration_filter_only_applies_when_a_close_candidate_exists():
    close = c("Rick Astley - Never Gonna Give You Up", "Channel", 220)
    far = c("Never Gonna Give You Up", "Rick Astley", 260, official=True)
    assert best(close, far) is close
    assert best(far) is far  # nothing within 20 s: the long one may still win


def test_wrong_song_or_artist_is_rejected():
    assert best(c("Together Forever", "Rick Astley", 213, official=True)) is None
    assert best(c("Never Gonna Give You Up", "Some Cover Band", 213, official=True)) is None


def test_unknown_duration_is_neutral():
    song = c("Never Gonna Give You Up", "Rick Astley", None, official=True)
    assert choose([song], TITLE, ARTIST, None) is song


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: canned search pages and videos, keyed by URL."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def extract_info(self, url, download=False, process=True):
        from yt_dlp.utils import DownloadError

        self.calls.append(url)
        if url not in self.pages:
            raise DownloadError(f"ERROR: no page {url}")
        return self.pages[url]


QUERY = "Rick Astley - Never Gonna Give You Up"
YTM = YTM_SEARCH.format(urllib.parse.quote(QUERY))
YTS = f"ytsearch6:{QUERY}"


def song(vid, title):
    return {"url": f"https://music.youtube.com/watch?v={vid}", "title": title}


def full(title, duration, artist="Rick Astley"):
    return {"title": title, "track": title, "artists": [artist], "duration": duration}


def search(ydl):
    return find(ydl, QUERY, TITLE, ARTIST, LENGTH, lambda: None)


def test_find_stops_at_a_confident_youtube_music_song():
    ydl = FakeYDL({
        YTM: {"entries": [song("tf", "Together Forever"), song("ng", "Never Gonna Give You Up")]},
        "https://music.youtube.com/watch?v=ng": full("Never Gonna Give You Up", 214),
    })
    found = search(ydl)
    assert found.url.endswith("=ng") and found.info["duration"] == 214
    assert ydl.calls == [YTM, "https://music.youtube.com/watch?v=ng"]  # no plain YouTube search


def test_find_falls_back_to_youtube_search():
    ydl = FakeYDL({
        YTM: {"entries": [song("ext", "Never Gonna Give You Up")]},
        "https://music.youtube.com/watch?v=ext": full("Never Gonna Give You Up", 400),
        YTS: {"entries": [{"url": "https://www.youtube.com/watch?v=ok", "duration": 213,
                           "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
                           "channel": "Rick Astley"}]},
    })
    assert search(ydl).url == "https://www.youtube.com/watch?v=ok"  # the 400 s one is too long


def test_find_survives_a_failed_youtube_music_search_and_gives_up_cleanly():
    ydl = FakeYDL({YTS: {"entries": [{"url": "u", "title": "Rick Astley - Together Forever",
                                      "duration": 213, "channel": "Rick Astley"}]}})
    assert search(ydl) is None
    assert ydl.calls == [YTM, YTS]


def test_find_checks_for_cancel_between_fetches():
    class Stop(Exception):
        pass

    def cancelled():
        raise Stop

    ydl = FakeYDL({YTM: {"entries": [song("ng", "Never Gonna Give You Up")]}})
    with pytest.raises(Stop):
        find(ydl, QUERY, TITLE, ARTIST, LENGTH, cancelled)
