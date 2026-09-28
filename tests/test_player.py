"""The libmpv player, driven for real with short generated tones through mpv's null audio output, or into a WAV file
where the equalizer is measured (silent either way)."""

import array
import locale
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from gi.repository import GLib

from siphon import eq
from siphon import player as player_module
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


# -- the equalizer ------------------------------------------------------------------------------------------------

ROCK = eq.BUILT_IN["Rock"]
MINE = (1.0, 2.0, 3.0, 0.0, -1.5, 0.0, 0.5, 0.0, -6.0, 9.0)
LEVEL = 0.02  # each of the ten tones in the test chord: all ten together stay far from full scale


def kept_time(seen: Recorder) -> bool:
    """The reported position kept pace with the clock: no stall, no jump back to the start."""
    (t0, p0), (t1, p1) = seen.positions[0], seen.positions[-1]
    moved = [p for _, p in seen.positions]
    return len(moved) >= 3 and moved == sorted(moved) and abs((p1 - p0) - (t1 - t0)) < 0.15


def filters(player: Player) -> dict[str, str]:
    """The playing song's filters, as mpv built them: label -> gain (the preamp's volume)."""
    built = {}
    for f in player._mpv.af:  # each an ffmpeg graph of one filter: "equalizer=f=31:t=o:w=1:g=5"
        params = dict(p.split("=", 1) for p in f["params"]["graph"].split("=", 1)[1].split(":"))
        built[f["label"]] = params.get("g", params.get("volume"))
    return built


def test_no_equalizer_or_a_flat_one_runs_no_filter(player, tones):
    player.set_equalizer(eq.FLAT)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.3)
    assert player._mpv.af == []
    player.set_equalizer(None)
    assert player._mpv.af == []


def test_every_song_starts_with_the_equalizer_set_before_it(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["t1"], tones["t2"]])
    run_until(lambda: player.position > 0.2)
    first = filters(player)
    assert tuple(first) == eq.FILTERS
    assert (first["preamp"], first["eq0"], first["eq4"]) == ("-7.8dB", "5", "-2")
    run_until(lambda: player.current == tones["t2"] and player.position > 0.2)
    assert filters(player) == first  # the gapless follow-on song too


def test_moving_a_band_while_playing_neither_rebuilds_nor_restarts(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 1.0)
    seen, built = Recorder(player), player._mpv.af
    for db in (6, 7, 8, 9):
        player.set_equalizer(ROCK[:9] + (db,))
        run_for(0.15)
    run_for(0.8)
    assert player._mpv.af == built  # af-command changed the running filters; `af` was never rewritten
    assert (player.state, seen.seeks, seen.errors) == ("playing", [], [])
    assert kept_time(seen)


def test_a_seek_after_live_changes_keeps_them(player, tones):
    """mpv rebuilds the filters from `af` on a seek, so `af` must carry the live gains by then."""
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    player.set_equalizer(MINE)
    assert filters(player)["eq9"] == "5"  # changed live only
    player.seek(4.0)
    run_until(lambda: player.position >= 4.0)
    assert (filters(player)["eq9"], filters(player)["preamp"]) == ("9", f"{eq.headroom(MINE):g}dB")


def test_switching_off_mid_song_zeroes_the_filters_and_the_next_seek_drops_them(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    seen, built = Recorder(player), player._mpv.af
    player.set_equalizer(None)
    run_until(lambda: player._live_eq == eq.FLAT)  # 0 dB by commands: taking them out would click
    player.set_equalizer(MINE)
    run_until(lambda: player._live_eq == MINE)
    player.set_equalizer(None)
    run_until(lambda: player._live_eq == eq.FLAT)
    assert player._mpv.af == built
    run_for(0.6)
    assert (player.state, seen.seeks, player.current) == ("playing", [], tones["long"])
    assert kept_time(seen)
    player.seek(5.0)
    run_until(lambda: player.position >= 5.0)
    assert player._mpv.af == []


def test_mpvs_own_on_screen_tools_are_off(player, tones):
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.3)
    assert [player._mpv[tool] for tool in player_module._TOOLS] == [False] * len(player_module._TOOLS)
    if os.path.isdir("/proc/self/task"):  # their threads are gone too
        names = [Path(f"/proc/self/task/{task}/comm").read_text().strip() for task in os.listdir("/proc/self/task")]
        assert not [name for name in names if name.startswith("lua/")]


@pytest.mark.linux
def test_the_equalizer_starts_no_threads(player, tones):
    """ffmpeg would give each filter's graph a pool of threads, one per core up to 16; each is held to one."""
    def threads_while_playing(gains) -> int:
        player.set_equalizer(gains)
        player.play_songs([tones["long"]])
        run_until(lambda: player.position > 0.5)
        return len(os.listdir("/proc/self/task"))

    plain = threads_while_playing(None)
    assert threads_while_playing(ROCK) <= plain + 2


def test_switching_on_mid_song_puts_the_filters_in_at_0_db_then_moves_them(player, tones):
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    seen = Recorder(player)
    player.set_equalizer(MINE)
    assert set(filters(player).values()) == {None, "0", "0dB"}  # the rate filter, the bands, the preamp
    run_until(lambda: player._live_eq == MINE)
    run_for(0.8)
    assert (player.state, seen.seeks) == ("playing", [])
    assert kept_time(seen)
    player.seek(5.0)
    run_until(lambda: player.position >= 5.0)
    assert filters(player)["eq9"] == "9"


def test_a_big_preamp_move_is_made_in_half_decibel_steps(player, tones):
    """The preamp is a plain gain: in one jump from Rock to off it clicks, in 0.5 dB steps it does not."""
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    walked = [player._live_eq]
    player.set_equalizer(None)
    while walked[-1] != eq.FLAT:
        run_until(lambda: player._live_eq != walked[-1])
        walked.append(player._live_eq)
    preamps = [eq.headroom(gains) for gains in walked]
    assert preamps[0] == -7.8 and preamps[-1] == 0
    assert max(abs(b - a) for a, b in zip(preamps, preamps[1:])) <= 0.6  # 0.5, and the rounding to 0.1 dB


def test_changes_made_while_paused_wait_for_the_resume(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    run_until(lambda: player.position > 0.5)
    player.pause()
    player.set_equalizer(None)
    run_for(0.3)
    assert player._live_eq == ROCK  # stepping in a pause would land every step at once when it resumes
    player.play()
    run_until(lambda: player._live_eq == eq.FLAT)


def test_the_next_song_starts_with_gains_changed_during_this_one(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["t1"], tones["t2"]])
    run_until(lambda: player.position > 0.2)
    player.set_equalizer(MINE)
    run_until(lambda: player.current == tones["t2"] and player.position > 0.2)
    assert filters(player)["eq9"] == "9"


def test_gains_changed_while_a_song_opens_reach_it(player, tones):
    player.set_equalizer(ROCK)
    player.play_songs([tones["long"]])
    player.set_equalizer(MINE)  # before mpv has made the song's filters: applied once it has opened
    run_until(lambda: player.position > 0.5)
    assert player._live_eq == MINE
    player.seek(2.0)
    run_until(lambda: player.position >= 2.0)
    assert filters(player)["eq9"] == "9"


# measured: the chord's ten tones through the real player, written by mpv's pcm output into a WAV file

@pytest.fixture(scope="module")
def chord(tmp_path_factory) -> Song:
    path = tmp_path_factory.mktemp("chord") / "chord.wav"
    tones = "+".join(f"{LEVEL}*sin(2*PI*{hz}*t)" for hz in eq.FREQUENCIES)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc={tones}:s=48000:d=3",
                    "-c:a", "pcm_s16le", str(path)], check=True)
    return Song(path, "Chord", "", "", 0.0, None, 0.0, path.stat().st_size)


def record(song: Song, gains, tmp_path: Path, monkeypatch) -> array.array:
    """Play the song to its end into a WAV file; its first channel, as floats."""
    monkeypatch.setenv("SIPHON_AO", "pcm")
    out = tmp_path / "out.wav"
    player = Player()
    try:
        player._mpv["ao-pcm-file"] = str(out)
        player.set_equalizer(gains)
        player.play_songs([song])
        run_until(lambda: player.state == "stopped", 10)
    finally:
        player.shutdown()  # the WAV header is finished when the output closes
    return first_channel(out)


def first_channel(path: Path) -> array.array:
    """A WAV file's first channel as floats, read here rather than by ffmpeg: the Windows build's ffmpeg encodes
    only the formats Siphon writes. mpv's pcm output is 16-bit when no filter runs, else 32-bit float."""
    data = path.read_bytes()
    pos, fmt, body = 12, b"", b""
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if data[pos:pos + 4] == b"fmt ":
            fmt = data[pos + 8:pos + 8 + size]
        elif data[pos:pos + 4] == b"data":
            body = data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    tag, channels, bits = (int.from_bytes(fmt[a:b], "little") for a, b in ((0, 2), (2, 4), (14, 16)))
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the subformat GUID starts with the format tag
        tag = int.from_bytes(fmt[24:26], "little")
    assert (tag, bits) in ((1, 16), (3, 32)), (tag, bits)
    samples = array.array("h" if tag == 1 else "f")
    samples.frombytes(body[:len(body) - len(body) % (channels * bits // 8)])
    scale = 1 / 32768 if tag == 1 else 1.0
    return array.array("d", (x * scale for x in samples[::channels]))


def level_db(samples, hz: float, rate: int = 48000) -> float:
    """Goertzel: the tone's level against LEVEL; exact over whole cycles, which one second of an integer Hz is."""
    c = 2 * math.cos(2 * math.pi * hz / rate)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + c * s1 - s2, s1
    return 20 * math.log10(2 * math.sqrt(s1 * s1 + s2 * s2 - c * s1 * s2) / len(samples) / LEVEL)


@pytest.mark.parametrize("gains", [None, eq.BUILT_IN["Bass"], ROCK, MINE], ids=["flat", "bass", "rock", "custom"])
def test_measured_gain_at_every_band_is_the_curve_less_the_preamp(chord, gains, tmp_path, monkeypatch):
    samples = record(chord, gains, tmp_path, monkeypatch)
    second = samples[48000:96000]  # the second second: the filters have settled
    for hz in eq.FREQUENCIES:
        expected = eq.response(gains, hz, 48000) + eq.headroom(gains) if gains else 0.0
        assert level_db(second, hz) == pytest.approx(expected, abs=0.1), hz


@pytest.mark.parametrize("loud", [(12.0,) * 10, (24.0,) * 10, (24.0, 24.0) + (0.0,) * 8],
                         ids=["all +12", "all +24", "31 and 62 Hz +24"])
def test_the_preamp_keeps_a_full_scale_tone_at_the_curves_peak_from_clipping(loud, tmp_path, monkeypatch):
    """A preamp of minus the highest slider would leave every band at +12 7.6 dB (2.4x) over full scale, at +24
    20.5 dB (10.6x). At 44.1 kHz these peak highest: 19.64 dB at 7988 Hz, 44.50 dB at 4003 Hz, 30.55 dB at 31 Hz."""
    rate = 44100
    hz = max(range(20, 20000), key=lambda f: eq.response(loud, f, rate))
    path = tmp_path / "full.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc=sin(2*PI*{hz}*t):s={rate}:d=2", "-c:a", "pcm_s24le", str(path)], check=True)
    samples = record(Song(path, "Full", "", "", 0.0, None, 0.0, 1), loud, tmp_path, monkeypatch)
    peak = max(map(abs, samples[rate:]))
    assert 0.98 < peak < 1.0  # close to full scale, not over it: e.g. a preamp of 19.7 dB for a 19.64 dB peak


def test_the_bands_hand_the_sound_on_as_floats(chord, tmp_path, monkeypatch):
    """They work in double; given doubles, mpv chose 16-bit samples for its output (ao=pcm, mpv 0.41)."""
    record(chord, ROCK, tmp_path, monkeypatch)
    fmt = (tmp_path / "out.wav").read_bytes()[12:60]
    assert fmt[:4] == b"fmt " and int.from_bytes(fmt[22:24], "little") == 32


@pytest.mark.parametrize("rate", [8000, 16000, 32000])
def test_a_band_at_half_the_sample_rate_does_not_turn_the_song_into_nan(rate, tmp_path, monkeypatch):
    """ffmpeg's equalizer gives NaN for a band exactly at the Nyquist frequency; the chain resamples such files."""
    path = tmp_path / f"low{rate}.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate={rate}:duration=1", "-c:a", "pcm_s16le", str(path)], check=True)
    samples = record(Song(path, "Low", "", "", 0.0, None, 0.0, 1), ROCK, tmp_path, monkeypatch)
    assert len(samples) > 40000 and not any(map(math.isnan, samples)) and 0.01 < max(map(abs, samples)) < 1.0
