"""The Settings dialog's logic that needs no display: the Updates page's lines."""

from types import SimpleNamespace

import pytest

from siphon.ui import settings_dialog


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
