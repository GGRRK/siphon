"""The Settings dialog's logic that needs no display: the equalizer page's words and lists, the Updates page's lines,
Close to Tray's subtitle; and the main window's three-dot menu."""

from types import SimpleNamespace

import pytest

from siphon import eq
from siphon.ui import equalizer, settings_dialog
from siphon.ui.window import main_menu


@pytest.mark.parametrize("hz, name", [(31, "31 Hz"), (500, "500 Hz"), (1000, "1 kHz"), (16000, "16 kHz")])
def test_band_names_for_screen_readers(hz, name):
    assert equalizer.band_name(hz) == name


def test_every_band_has_a_name_and_a_caption():
    assert [equalizer.band_name(hz) for hz in eq.FREQUENCIES] == [
        "31 Hz", "62 Hz", "125 Hz", "250 Hz", "500 Hz", "1 kHz", "2 kHz", "4 kHz", "8 kHz", "16 kHz"]
    assert len(eq.LABELS) == len(eq.FREQUENCIES)


@pytest.mark.parametrize("db, text", [(0.0, "0 dB"), (-0.0, "0 dB"), (3.5, "+3.5 dB"), (-12.0, "-12 dB"),
                                      (12.0, "+12 dB"), (-0.5, "-0.5 dB"), (-24.0, "-24 dB"), (23.5, "+23.5 dB")])
def test_decibels(db, text):
    assert equalizer.decibels(db) == text


def test_preset_names_built_in_first_then_yours():
    equal = eq.Equalizer()
    equal.apply("Rock")
    equal.save("Mine")
    assert equalizer.preset_names(equal) == [*eq.BUILT_IN, "Mine"]


def test_custom_is_listed_last_only_while_the_curve_is_no_preset():
    equal = eq.Equalizer()
    equal.save("Mine")
    equal.set_band(0, 7.5)
    assert equal.preset == eq.CUSTOM
    assert equalizer.preset_names(equal) == [*eq.BUILT_IN, "Mine", eq.CUSTOM]
    equal.set_band(0, 0.0)  # back on Flat's curve
    assert equalizer.preset_names(equal) == [*eq.BUILT_IN, "Mine"]


def test_the_current_preset_is_always_in_the_list():
    equal = eq.Equalizer()
    for step in (lambda: equal.apply("Jazz"), lambda: equal.set_band(3, -4.0), lambda: equal.save("Late"),
                 lambda: equal.delete("Late"), lambda: equal.apply("Flat")):
        step()
        assert equal.preset in equalizer.preset_names(equal)


@pytest.mark.parametrize("name, problem", [
    ("Warm", ""),
    ("rock", "“rock” is a built-in name; choose another."),
    ("custom", "“custom” is a built-in name; choose another."),
    ("x" * 41, "A preset name can have at most 40 characters."),
])
def test_name_problems(name, problem):
    assert equalizer.name_problem(name) == problem


def _updates(**fields) -> SimpleNamespace:
    base = dict(state="idle", message="", engine_state="idle", engine_message="", engine_pending="")
    return SimpleNamespace(**(base | fields))


def test_status_line_before_any_check():
    assert settings_dialog.status_line(_updates()) == "Not checked since Siphon started."


@pytest.mark.parametrize("state", ["checking", "downloading", "ready", "up-to-date", "available", "unavailable",
                                   "error"])
def test_status_line_is_the_updates_own_sentence(state):
    assert settings_dialog.status_line(_updates(state=state, message="One sentence.")) == "One sentence."


@pytest.mark.parametrize("state, action", [
    ("idle", "check"), ("checking", "busy"), ("downloading", "progress"), ("ready", "restart"),
    ("up-to-date", "check"), ("available", "download"), ("unavailable", "check"), ("error", "check"),
])
def test_status_action_per_state(state, action):
    assert settings_dialog.status_action(state) == action


def test_engine_line():
    assert settings_dialog.engine_line(_updates()) == "Not checked since Siphon started."
    ready = _updates(engine_state="ready", engine_pending="2026.09.25",
                     engine_message="yt-dlp 2026.09.25 will be used from the next start.")
    assert settings_dialog.engine_line(ready) == "yt-dlp 2026.09.25 will be used from the next start."
    current = _updates(engine_state="up-to-date", engine_message="The engine is up to date (yt-dlp 2026.08.19).")
    assert settings_dialog.engine_line(current) == "The engine is up to date (yt-dlp 2026.08.19)."


def test_engine_line_keeps_a_fetched_engine_after_a_failed_check():
    failed = _updates(engine_state="error", engine_pending="2026.09.25", engine_message="Couldn't reach PyPI.")
    assert settings_dialog.engine_line(failed) == (
        "Couldn't reach PyPI. yt-dlp 2026.09.25 will be used from the next start.")
    assert settings_dialog.engine_line(_updates(engine_state="error", engine_message="Couldn't reach PyPI.")) == (
        "Couldn't reach PyPI.")


def test_close_to_tray_says_why_it_is_off():
    assert settings_dialog.tray_line(True) == "Closing the window keeps Siphon playing and downloading in the tray"
    assert settings_dialog.tray_line(False) == "No system tray was found, so closing the window quits Siphon"
    assert settings_dialog.tray_line(True, background=True) == settings_dialog.tray_line(True)
    assert settings_dialog.tray_line(False, background=True) == (
        "No system tray here: closing the window keeps Siphon running in the background while it plays or "
        "downloads, and quits it otherwise")


def test_the_three_dot_menu_has_quit():
    menu = main_menu()
    items = []
    for s in range(menu.get_n_items()):
        section = menu.get_item_link(s, "section")
        items += [(section.get_item_attribute_value(i, "label").get_string(),
                   section.get_item_attribute_value(i, "action").get_string()) for i in range(section.get_n_items())]
    assert items == [("Open Music Folder", "win.open-folder"), ("Refresh Library", "win.refresh-library"),
                     ("Update Engine", "app.update-engine"), ("About Siphon", "app.about"), ("Quit", "app.quit")]
