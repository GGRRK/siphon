import json
import math
import random

import pytest

from siphon import eq, settings
from siphon.eq import BUILT_IN, CUSTOM, FLAT, Equalizer

FORMATS = ("mp3", "opus")
ROCK = BUILT_IN["Rock"]
MINE = (1.0, 2.0, 3.0, 0.0, -1.5, 0.0, 0.5, 0.0, -12.0, 12.0)


# -- the model ------------------------------------------------------------------------------------------------------

def test_bands_and_built_in_presets():
    assert eq.FREQUENCIES == (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
    assert len(eq.LABELS) == len(eq.FREQUENCIES) and eq.LABELS[5] == "1k"
    assert list(BUILT_IN) == ["Flat", "Bass", "Treble", "Vocal", "Pop", "Rock", "Jazz", "Classic"]
    assert BUILT_IN["Rock"] == (5, 4, 2, -1, -2, -1, 2, 4, 5, 5)
    assert BUILT_IN["Vocal"] == (-2, -1, 0, 2, 4, 4, 3, 1, 0, -1)
    assert all(len(curve) == 10 and all(eq.MIN_DB <= db <= eq.MAX_DB for db in curve) for curve in BUILT_IN.values())


def test_a_new_equalizer_is_on_and_flat_so_nothing_plays_until_a_curve_is_picked():
    fresh = Equalizer()
    assert (fresh.enabled, fresh.bands, fresh.preset, fresh.presets) == (True, FLAT, "Flat", {})
    assert fresh.gains() is None
    fresh.apply("Bass")
    assert fresh.gains() == BUILT_IN["Bass"]
    fresh.enabled = False
    assert fresh.gains() is None


@pytest.mark.parametrize("db, snapped", [(3.2, 3.0), (3.3, 3.5), (-0.2, 0.0), (99, 12.0), (-99, -12.0), (-12.4, -12.0)])
def test_a_band_snaps_to_half_decibels_inside_the_range(db, snapped):
    model = Equalizer()
    model.set_band(4, db)
    assert model.bands[4] == snapped
    assert math.copysign(1, model.bands[4]) == math.copysign(1, snapped)  # never -0.0, which would print as "-0"


def test_editing_a_band_says_custom_until_the_bands_match_a_preset_again():
    model = Equalizer()
    model.apply("Rock")
    model.set_band(0, 6)
    assert model.preset == CUSTOM
    model.set_band(0, 5)
    assert model.preset == "Rock"
    for i, db in enumerate(BUILT_IN["Jazz"]):
        model.set_band(i, db)
    assert model.preset == "Jazz"


def test_unknown_preset_is_a_key_error():
    with pytest.raises(KeyError):
        Equalizer().apply("Nope")


def test_saving_adds_a_preset_that_is_then_the_current_one():
    model = Equalizer()
    for i, db in enumerate(MINE):
        model.set_band(i, db)
    assert model.preset == CUSTOM
    assert model.save("  Late \n Night  ") == "Late Night"
    assert (model.preset, model.presets) == ("Late Night", {"Late Night": MINE})
    assert model.names() == [*BUILT_IN, "Late Night"]
    model.apply("Flat")
    model.apply("Late Night")
    assert (model.bands, model.preset) == (MINE, "Late Night")


def test_saving_over_a_name_in_any_case_replaces_that_preset_in_its_place():
    model = Equalizer(presets={"One": ROCK, "Two": FLAT, "Three": ROCK})
    model.apply("Bass")
    assert model.find(" two ") == "Two"  # the page asks before replacing
    assert model.find("Four") is None
    assert model.save("TWO") == "TWO"
    assert list(model.presets.items()) == [("One", ROCK), ("TWO", BUILT_IN["Bass"]), ("Three", ROCK)]


@pytest.mark.parametrize("name, message", [
    ("   ", "Give the preset a name."),
    ("x" * 41, "at most 40 characters"),
    ("bad\x00name", "control characters"),
    ("rock", "“rock” is a built-in name"),
    ("CUSTOM", "“CUSTOM” is a built-in name"),
])
def test_unusable_names_are_refused_with_a_reason(name, message):
    model = Equalizer()
    with pytest.raises(ValueError, match=message):
        model.save(name)
    assert model.presets == {}


def test_a_long_name_at_the_limit_and_other_scripts_are_fine():
    model = Equalizer()
    for name in ("x" * 40, "Nuit d’été", "Ночь", "🔥 Bass"):
        assert model.save(name) == name


def test_rename_keeps_the_place_and_follows_the_current_preset():
    model = Equalizer(presets={"A": ROCK, "B": MINE})
    model.apply("B")
    assert model.rename("B", "  Bee ") == "Bee"
    assert list(model.presets.items()) == [("A", ROCK), ("Bee", MINE)]
    assert model.preset == "Bee"
    assert model.rename("A", "a") == "a"  # only the case changes
    assert list(model.presets) == ["a", "Bee"]


def test_rename_refuses_a_taken_or_built_in_name_and_an_unknown_preset():
    model = Equalizer(presets={"A": ROCK, "B": MINE})
    with pytest.raises(ValueError, match="already a preset called “A”"):
        model.rename("B", "a")
    with pytest.raises(ValueError, match="built-in"):
        model.rename("B", "Pop")
    with pytest.raises(KeyError):
        model.rename("Rock", "Stone")  # built-in presets are not the user's to rename
    assert list(model.presets) == ["A", "B"]


def test_delete_keeps_the_sound_and_names_what_it_now_is():
    model = Equalizer(presets={"Mine": MINE, "Also Rock": ROCK})
    model.apply("Mine")
    model.delete("Mine")
    assert (model.bands, model.preset, list(model.presets)) == (MINE, CUSTOM, ["Also Rock"])
    model.apply("Also Rock")
    model.delete("Also Rock")
    assert (model.bands, model.preset) == (ROCK, "Rock")
    with pytest.raises(KeyError):
        model.delete("Flat")


# -- settings.json --------------------------------------------------------------------------------------------------

@pytest.fixture
def defaults(tmp_path) -> settings.Settings:
    return settings.Settings("mp3", tmp_path / "Music")


def test_round_trip_through_settings_json(tmp_path, defaults):
    path = tmp_path / "settings.json"
    chosen = settings.Settings("opus", tmp_path, equalizer=Equalizer(False, MINE, "Mine", {"Mine": MINE, "Loud": ROCK}))
    settings.save(chosen, path)
    stored = json.loads(path.read_text())["equalizer"]
    assert stored == {"enabled": False, "bands": list(MINE), "preset": "Mine",
                      "presets": {"Mine": list(MINE), "Loud": list(ROCK)}}
    assert settings.load(defaults, FORMATS, path) == chosen


def test_an_older_file_without_an_equalizer_gets_the_default(tmp_path, defaults):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "opus", "volume": 0.5}))
    assert settings.load(defaults, FORMATS, path).equalizer == Equalizer()


@pytest.mark.parametrize("data", [None, [], "Rock", 3, {"enabled": "yes", "bands": "loud", "presets": []}])
def test_a_damaged_equalizer_falls_back_to_the_default(data):
    assert eq.from_json(data, Equalizer()) == Equalizer()


@pytest.mark.parametrize("bands", [
    [1] * 9, [1] * 11, [1, 2, 3, 4, 5, 6, 7, 8, 9, "10"], [True] + [0] * 9, [float("nan")] + [0] * 9,
    [float("inf")] + [0] * 9, [[1]] * 10, {"0": 1},
])
def test_bands_of_the_wrong_length_or_type_are_refused(bands):
    assert eq.from_json({"enabled": False, "bands": bands}, Equalizer()) == Equalizer(enabled=False)


def test_each_missing_or_damaged_value_falls_back_to_its_default():
    defaults = Equalizer(False, ROCK, "Rock", {"Mine": MINE})
    assert eq.from_json({"presets": "none"}, defaults) == defaults
    assert eq.from_json({"enabled": True, "presets": {}}, defaults) == Equalizer(True, ROCK, "Rock", {})


def test_saved_values_are_snapped_and_clamped():
    loaded = eq.from_json({"bands": [30, -30, 1.26, 0, 0, 0, 0, 0, 0, 0]}, Equalizer())
    assert loaded.bands == (12.0, -12.0, 1.5, 0, 0, 0, 0, 0, 0, 0)
    assert loaded.preset == CUSTOM


def test_bad_presets_are_dropped_one_by_one():
    data = {"presets": {"Good": list(MINE), "  Spaced   Out ": list(ROCK), "": list(ROCK), "rock": list(ROCK),
                        "Short": [1, 2], "Words": "loud", "GOOD": list(ROCK), "x" * 41: list(ROCK)}}
    assert eq.from_json(data, Equalizer()).presets == {"Good": MINE, "Spaced Out": ROCK}


def test_the_saved_preset_name_is_kept_only_while_it_still_names_the_bands():
    presets = {"Mine": list(ROCK)}
    # two presets share Rock's curve: the one picked is remembered
    assert eq.from_json({"bands": list(ROCK), "preset": "Mine", "presets": presets}, Equalizer()).preset == "Mine"
    assert eq.from_json({"bands": list(ROCK), "preset": "Rock", "presets": presets}, Equalizer()).preset == "Rock"
    # a stale or unknown name gives way to what the bands are
    assert eq.from_json({"bands": list(ROCK), "preset": "Jazz"}, Equalizer()).preset == "Rock"
    assert eq.from_json({"bands": list(MINE), "preset": "Gone"}, Equalizer()).preset == CUSTOM
    assert eq.from_json({"bands": list(MINE), "preset": 7}, Equalizer()).preset == CUSTOM


# -- mpv's filter chain ---------------------------------------------------------------------------------------------

def test_off_or_flat_is_no_filter_at_all():
    assert eq.chain(None) == ""
    assert eq.chain(FLAT) == ""
    assert eq.chain([0] * 10) == ""


def test_the_chain_is_a_preamp_then_ten_octave_wide_peaking_filters():
    assert eq.chain(ROCK) == (
        "@rate:aformat=sample_rates=44100|48000|88200|96000|176400|192000|352800|384000,"
        "@preamp:volume=volume=-7.8dB,"
        "@eq0:equalizer=f=31:t=o:w=1:g=5,@eq1:equalizer=f=62:t=o:w=1:g=4,@eq2:equalizer=f=125:t=o:w=1:g=2,"
        "@eq3:equalizer=f=250:t=o:w=1:g=-1,@eq4:equalizer=f=500:t=o:w=1:g=-2,@eq5:equalizer=f=1000:t=o:w=1:g=-1,"
        "@eq6:equalizer=f=2000:t=o:w=1:g=2,@eq7:equalizer=f=4000:t=o:w=1:g=4,@eq8:equalizer=f=8000:t=o:w=1:g=5,"
        "@eq9:equalizer=f=16000:t=o:w=1:g=5")


def test_a_single_band_keeps_all_ten_filters_so_any_slider_can_move_live():
    gains = (0, 0, 0, 0, 0, 0, 0, 0, 0, -3.5)
    assert eq.chain(gains).split(",", 1)[1] == ("@preamp:volume=volume=0dB," + ",".join(
        f"@eq{i}:equalizer=f={hz}:t=o:w=1:g=0" for i, hz in enumerate(eq.FREQUENCIES[:9]))
        + ",@eq9:equalizer=f=16000:t=o:w=1:g=-3.5")


def test_commands_change_only_what_moved_and_turn_down_before_boosting():
    up = (5, 4, 2, -1, -2, -1, 2, 4, 5, 9)
    assert eq.commands(ROCK, up) == [("preamp", "volume", "-9.7dB", "volume"), ("eq9", "gain", "9", "equalizer")]
    assert eq.commands(up, ROCK) == [("eq9", "gain", "5", "equalizer"), ("preamp", "volume", "-7.8dB", "volume")]
    cut = (5, 4, 2, -1, -2, -1.5, 2, 4, 5, 5)  # a cut away from the peak leaves the preamp alone
    assert eq.commands(ROCK, cut) == [("eq5", "gain", "-1.5", "equalizer")]
    assert eq.commands(ROCK, ROCK) == []


@pytest.mark.parametrize("gains, preamp", [
    (FLAT, 0.0), ((-12.0,) * 10, 0.0), (BUILT_IN["Bass"], -8.1), (BUILT_IN["Treble"], -9.4), (ROCK, -7.8),
    ((0, 0, 0, 0, 0, 12.0, 0, 0, 0, 0), -12.1), ((12.0,) * 10, -19.7),
])
def test_headroom_is_the_curves_highest_point_and_a_margin_not_the_highest_slider(gains, preamp):
    """Bass peaks at 7.96, Treble at 9.28 (44.1 kHz), Rock at 7.67, every band at +12 at 19.64 dB; 0.05 dB more for
    ffmpeg's float arithmetic, rounded up to the next tenth."""
    assert eq.headroom(gains) == preamp


def _dense_peak(gains) -> float:
    return max(eq.response(gains, 20 * 2 ** (k / 96), rate) for rate in (44100, 48000) for k in range(961))


def test_headroom_covers_the_true_peak_of_any_curve_with_the_margin():
    rng = random.Random(7)
    for _ in range(12):
        gains = tuple(eq.snap(rng.uniform(eq.MIN_DB, eq.MAX_DB)) for _ in eq.FREQUENCIES)
        peak = _dense_peak(gains)
        # the peak search may miss the top by a few thousandths of a dB, which the 0.05 dB margin absorbs
        assert -peak - 0.15 <= eq.headroom(gains) <= -peak - 0.05 + 0.003 or peak <= 0


@pytest.mark.parametrize("start, end", [(ROCK, FLAT), (FLAT, ROCK), (ROCK, MINE), (FLAT, (12.0,) * 10),
                                        (BUILT_IN["Bass"], BUILT_IN["Treble"])])
def test_steps_move_the_preamp_at_most_half_a_decibel_and_arrive(start, end):
    walked = [start]
    while walked[-1] != end:
        walked.append(eq.step(walked[-1], end))
        assert len(walked) < 100
    preamps = [eq.headroom(gains) for gains in walked]
    assert max(abs(b - a) for a, b in zip(preamps, preamps[1:])) <= 0.6  # 0.5, and the rounding to 0.1 dB


def test_a_step_that_moves_the_preamp_half_a_decibel_or_less_goes_all_the_way():
    assert eq.step(ROCK, ROCK[:2] + (6.0,) + ROCK[3:]) == ROCK[:2] + (6.0,) + ROCK[3:]
    slider = ROCK[:8] + (5.5,) + ROCK[9:]  # preamp -7.8 -> -8.3
    assert (eq.headroom(slider), eq.step(ROCK, slider)) == (-8.3, slider)


def test_the_model_matches_known_values_of_the_rbj_peaking_filter():
    alone = (0, 0, 0, 0, 0, 6.0, 0, 0, 0, 0)
    assert eq.response(alone, 1000, 48000) == pytest.approx(6.0, abs=1e-9)  # exact at the centre
    # bandwidth in octaves is measured between the half-gain (in dB) points: half an octave either side
    assert eq.response(alone, 1000 * 2 ** 0.5, 48000) == pytest.approx(3.0, abs=0.05)
    assert eq.response(alone, 1000 / 2 ** 0.5, 48000) == pytest.approx(3.0, abs=0.05)
    assert eq.response(FLAT, 440, 44100) == 0
