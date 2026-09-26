"""The libmpv player, driven for real with short generated tones through mpv's null audio output (silent)."""

import locale
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from gi.repository import GLib

from siphon.library import Song
from siphon.player import Player

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make test tones"),
    # PyGObject's MainContext.iteration() asks asyncio for its (deprecated) loop policy on every call
    pytest.mark.filterwarnings("ignore::DeprecationWarning:gi.events"),
]


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    monkeypatch.setenv("SIPHON_AO", "null")


def _tone(folder: Path, name: str, title: str, seconds: float, hz: int) -> Song:
    path = folder / name
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
                    "-metadata", f"title={title}", "-c:a", "libmp3lame", str(path)], check=True)
    return Song(path, title, "Tester", "Tones", 0.0, None, path.stat().st_mtime, path.stat().st_size)


@pytest.fixture(scope="module")
def tones(tmp_path_factory) -> dict[str, Song]:
    folder = tmp_path_factory.mktemp("tones")
    songs = {f"t{i}": _tone(folder, f"t{i}.mp3", f"Tone {i}", 1, 300 + 100 * i) for i in range(1, 5)}
    songs["long"] = _tone(folder, "long.mp3", "Long", 8, 440)
    (folder / "bad.mp3").write_text("not audio")
    songs["bad"] = Song(folder / "bad.mp3", "Broken", "", "", 0.0, None, 0.0, 9)
    songs["missing"] = Song(folder / "gone.mp3", "Gone", "", "", 0.0, None, 0.0, 0)
    return songs


@pytest.fixture
def player():
    player = Player()
    yield player
    player.shutdown()


class Recorder:
    """Collects every signal the player emits."""

    def __init__(self, player: Player) -> None:
        self.titles: list[str] = []  # the current song's title each time it changes
        self.errors: list[str] = []
        self.seeks: list[float] = []
        self.positions: list[tuple[float, float]] = []  # (monotonic time, position)
        player.connect("changed", self._changed)
        player.connect("error", lambda _p, message: self.errors.append(message))
        player.connect("seeked", lambda _p, seconds: self.seeks.append(seconds))
        player.connect("position", lambda _p, seconds: self.positions.append((time.monotonic(), seconds)))

    def _changed(self, player: Player) -> None:
        if player.current and (not self.titles or self.titles[-1] != player.current.title):
            self.titles.append(player.current.title)


def run_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting on the player"
        context.iteration(False)
        time.sleep(0.005)


def run_for(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    run_until(lambda: time.monotonic() >= deadline, seconds + 1)


def test_the_output_in_tests_is_mpvs_null_driver(player, tones):
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0)
    assert player._mpv.current_ao == "null"


def test_starts_after_gtk_has_set_the_users_number_format():
    """GTK's start-up applies the user's locale; libmpv refuses to start unless LC_NUMERIC is "C"."""
    for name in ("de_DE.UTF-8", "en_US.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8"):
        try:
            locale.setlocale(locale.LC_NUMERIC, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no non-C locale is installed")
    try:
        player = Player()
        player.shutdown()
    finally:
        locale.setlocale(locale.LC_NUMERIC, "C")


def test_playing_reports_duration_and_position_about_four_times_a_second(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["long"]])
    assert (player.state, player.current, player.index) == ("playing", tones["long"], 0)
    run_until(lambda: player.duration > 0)
    assert player.duration == pytest.approx(8, abs=0.1)  # the tags said 0; mpv measured the file
    run_until(lambda: player.position >= 2.0)
    window = [t for t, _ in seen.positions if t >= seen.positions[-1][0] - 1.0]
    assert 3 <= len(window) <= 6
    assert [p for _, p in seen.positions] == sorted(p for _, p in seen.positions)


def test_songs_play_through_in_order_then_rewind_stopped(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["t1"], tones["t2"], tones["t3"]])
    run_until(lambda: player.state == "stopped", timeout=8)
    assert seen.titles == ["Tone 1", "Tone 2", "Tone 3", "Tone 1"]
    assert (player.index, player.position, seen.errors) == (0, 0.0, [])


def test_repeat_one_plays_the_same_song_again(player, tones):
    seen = Recorder(player)
    player.set_repeat("one")
    player.play_songs([tones["t1"], tones["t2"]])
    run_until(lambda: len(seen.seeks) >= 2, timeout=8)
    assert seen.seeks[:2] == [0.0, 0.0]  # each restart is a jump back to the start
    assert (player.state, player.index, seen.titles) == ("playing", 0, ["Tone 1"])


def test_repeat_all_wraps_to_the_first_song(player, tones):
    seen = Recorder(player)
    player.set_repeat("all")
    player.play_songs([tones["t1"], tones["t2"]])
    run_until(lambda: len(seen.titles) >= 3, timeout=8)
    assert seen.titles[:3] == ["Tone 1", "Tone 2", "Tone 1"]
    assert player.state == "playing"


def test_repeat_off_ends_next_at_the_last_song(player, tones):
    player.play_songs([tones["t1"], tones["t2"]], start=1)
    assert not player.has_next()
    player.next()
    assert player.index == 1
    player.set_repeat("all")
    assert player.has_next()
    with pytest.raises(ValueError):
        player.set_repeat("sometimes")


def test_shuffle_keeps_the_current_song_and_visits_every_other_once(player, tones):
    songs = [tones["t1"], tones["t2"], tones["t3"], tones["t4"]]
    player.play_songs(songs, start=2)
    player.set_shuffle(True)
    assert (player.index, player.shuffle) == (2, True)
    visited = [player.index]
    while player.has_next():
        player.next()
        visited.append(player.index)
    assert sorted(visited) == [0, 1, 2, 3] and visited[0] == 2


def test_unshuffle_returns_to_list_order_at_the_current_song(player, tones):
    player.play_songs([tones["t1"], tones["t2"], tones["t3"], tones["t4"]])
    player.set_shuffle(True)
    player.jump(1)
    player.set_shuffle(False)
    assert player.current == tones["t2"]
    player.next()
    assert player.current == tones["t3"]


def test_enqueue_and_play_next(player, tones):
    player.play_songs([tones["t1"], tones["t2"]])
    player.enqueue([tones["t4"]])
    player.play_next([tones["t3"]])
    assert [s.title for s in player.queue] == ["Tone 1", "Tone 3", "Tone 2", "Tone 4"]
    assert player.index == 0
    titles = []
    while player.has_next():
        player.next()
        titles.append(player.current.title)
    assert titles == ["Tone 3", "Tone 2", "Tone 4"]


def test_adding_to_an_empty_queue_readies_the_first_song_without_playing(player, tones):
    player.enqueue([tones["t1"], tones["t2"]])
    assert (player.state, player.current, player.index) == ("stopped", tones["t1"], 0)
    player.toggle()
    assert player.state == "playing"


def test_seek_pause_and_toggle(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["long"]])
    run_until(lambda: player.duration > 0)
    player.seek(4.0)
    assert seen.seeks == [4.0]
    run_until(lambda: player.position >= 4.3)
    player.pause()
    assert player.state == "paused"
    run_for(0.3)
    held, count = player.position, len(seen.positions)
    run_for(0.6)
    assert (player.position, len(seen.positions)) == (held, count)  # nothing moves while paused
    player.toggle()
    assert player.state == "playing"
    run_until(lambda: player.position > held + 0.3)
    player.toggle()
    assert player.state == "paused"


def test_a_seek_while_the_file_opens_lands_once_it_is_open(player, tones):
    player.play_songs([tones["long"]])
    player.seek(5.0)
    assert player.position == 5.0
    run_until(lambda: player.position > 5.2)
    assert player.position < 6.5


def test_previous_restarts_after_three_seconds_otherwise_goes_back(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["t1"], tones["long"]], start=1)
    run_until(lambda: player.duration > 0)
    player.seek(3.5)
    run_until(lambda: player.position > 3.5)
    player.previous()
    assert (player.index, seen.seeks[-1]) == (1, 0.0)
    player.previous()
    assert player.current == tones["t1"]


def test_stop_keeps_the_song_and_play_starts_it_again(player, tones):
    player.play_songs([tones["t1"], tones["long"]], start=1)
    run_until(lambda: player.position > 0.3)
    player.stop()
    assert (player.state, player.current, player.position) == ("stopped", tones["long"], 0.0)
    run_for(0.3)
    assert player.state == "stopped"
    player.play()
    run_until(lambda: player.position > 0.3)
    assert (player.state, player.current) == ("playing", tones["long"])


def test_songs_that_fail_to_load_are_reported_and_skipped_quietly(player, tones, capfd):
    seen = Recorder(player)
    player.play_songs([tones["bad"], tones["missing"], tones["t1"]])
    run_until(lambda: player.current == tones["t1"])
    assert seen.errors == ["Couldn't play “Broken”: unrecognized file format.",
                           "Couldn't play “Gone”: the file is missing."]
    run_until(lambda: player.state == "stopped")
    assert capfd.readouterr() == ("", "")  # mpv writes nothing to the terminal


def test_a_long_run_of_broken_songs_still_reaches_the_good_one(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["bad"], tones["missing"]] * 4 + [tones["t2"]])
    run_until(lambda: player.current == tones["t2"] and player.state == "playing")
    assert len(seen.errors) == 8


@pytest.mark.linux
def test_a_file_name_that_is_not_utf8_plays(player, tones):
    odd = tones["t1"].path.with_name(os.fsdecode(b"caf\xe9.mp3"))
    shutil.copy(tones["t1"].path, odd)
    seen = Recorder(player)
    player.play_songs([Song(odd, "Odd", "", "", 0.0, None, 0.0, 0)])
    run_until(lambda: player.position > 0.2)
    assert seen.errors == []


def test_a_queue_where_nothing_loads_stops_even_on_repeat_all(player, tones):
    seen = Recorder(player)
    player.set_repeat("all")
    player.play_songs([tones["bad"], tones["missing"]])
    run_until(lambda: player.state == "stopped")
    run_for(0.5)
    assert (len(seen.errors), player.state) == (2, "stopped")


def test_volume_maps_to_mpv_and_clamps(player, tones):
    player.set_volume(0.5)
    assert (player.volume, player._mpv.volume) == (0.5, 50)
    player.set_volume(3)
    assert (player.volume, player._mpv.volume) == (1.0, 100)


def test_shutdown_is_prompt_and_final(player, tones):
    seen = Recorder(player)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    started = time.monotonic()
    player.shutdown()
    assert time.monotonic() - started < 1.0
    count = len(seen.positions)
    player.toggle()
    player.next()
    player.seek(2)
    player.shutdown()
    run_for(0.4)
    assert len(seen.positions) == count
