import json
from pathlib import Path

import pytest

from siphon import settings
from siphon.settings import Settings

FORMATS = ("mp3", "m4a", "opus", "flac", "best")


@pytest.fixture
def defaults(tmp_path: Path) -> Settings:
    return Settings("mp3", tmp_path / "Music" / "Siphon")


@pytest.mark.linux
def test_config_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings.config_path() == tmp_path / "siphon" / "settings.json"


@pytest.mark.linux
@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_config_path_ignores_unset_or_relative_xdg(monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert settings.config_path() == Path.home() / ".config" / "siphon" / "settings.json"


def test_missing_file_gives_defaults(tmp_path, defaults):
    assert settings.load(defaults, FORMATS, tmp_path / "nope.json") == defaults


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2]", '"text"', "\xff\xfe"])
def test_corrupt_file_gives_defaults(tmp_path, defaults, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="latin-1")
    assert settings.load(defaults, FORMATS, path) == defaults


def test_bad_values_fall_back_one_by_one(tmp_path, defaults):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "wav", "folder": "/srv/audio"}))
    assert settings.load(defaults, FORMATS, path) == Settings("mp3", Path("/srv/audio"))
    path.write_text(json.dumps({"format": "flac", "folder": 42}))
    assert settings.load(defaults, FORMATS, path) == Settings("flac", defaults.folder)
    path.write_text(json.dumps({"format": ["mp3"], "folder": "  "}))
    assert settings.load(defaults, FORMATS, path) == defaults


def test_tilde_folder_expands(tmp_path, defaults):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "opus", "folder": "~/Songs"}))
    assert settings.load(defaults, FORMATS, path).folder == Path.home() / "Songs"


def test_round_trip_creates_dirs_and_leaves_no_temp_files(tmp_path, defaults):
    path = tmp_path / "cfg" / "siphon" / "settings.json"
    chosen = Settings("flac", tmp_path / "Out & About")
    settings.save(chosen, path)
    assert settings.load(defaults, FORMATS, path) == chosen
    settings.save(Settings("best", tmp_path), path)
    assert settings.load(defaults, FORMATS, path) == Settings("best", tmp_path)
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


@pytest.mark.linux
def test_save_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings.save(Settings("m4a", tmp_path / "x"))
    assert json.loads((tmp_path / "siphon" / "settings.json").read_text()) == {
        "format": "m4a", "folder": str(tmp_path / "x"), "volume": 0.8, "shuffle": False,
        "repeat": "off", "sort": "added", "page": "download", "style": "system", "accent": "system",
        "equalizer": {"enabled": True, "bands": [0.0] * 10, "preset": "Flat", "presets": {}}}


def test_failed_write_keeps_old_file_and_cleans_up(monkeypatch, tmp_path, defaults):
    path = tmp_path / "settings.json"
    settings.save(Settings("opus", tmp_path), path)

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(settings.os, "replace", boom)
    with pytest.raises(OSError):
        settings.save(Settings("flac", tmp_path), path)
    assert settings.load(defaults, FORMATS, path).format == "opus"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_file_from_before_the_player_loads_with_new_defaults(tmp_path, defaults):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "opus", "folder": "/srv/audio"}))
    loaded = settings.load(defaults, FORMATS, path)
    assert loaded == Settings("opus", Path("/srv/audio"))
    assert (loaded.volume, loaded.shuffle, loaded.repeat, loaded.sort, loaded.page) == (0.8, False, "off", "added", "download")
    assert (loaded.style, loaded.accent) == ("system", "system")


def test_player_and_view_state_round_trip(tmp_path, defaults):
    path = tmp_path / "settings.json"
    chosen = Settings("mp3", tmp_path, volume=0.35, shuffle=True, repeat="one", sort="artist", page="playlists",
                      style="dark", accent="purple")
    settings.save(chosen, path)
    assert settings.load(defaults, FORMATS, path) == chosen


@pytest.mark.parametrize("key, value", [
    ("volume", "loud"), ("volume", True), ("volume", None), ("volume", [0.5]),
    ("shuffle", "yes"), ("shuffle", 1),
    ("repeat", "twice"), ("repeat", True),
    ("sort", "genre"), ("sort", 3),
    ("page", "settings"), ("page", None),
    ("style", "Dark"), ("style", "auto"), ("style", 1), ("style", None), ("style", ["dark"]),
    ("accent", "Purple"), ("accent", "#9141ac"), ("accent", "magenta"), ("accent", 0), ("accent", False),
])
def test_bad_new_values_fall_back_alone(tmp_path, defaults, key, value):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "flac", "folder": "/srv/audio", key: value}))
    loaded = settings.load(defaults, FORMATS, path)
    assert getattr(loaded, key) == getattr(defaults, key)
    assert (loaded.format, loaded.folder) == ("flac", Path("/srv/audio"))


@pytest.mark.parametrize("raw, expected", [(1.7, 1.0), (-0.2, 0.0), (0, 0.0), (1, 1.0), (0.5, 0.5)])
def test_volume_is_clamped(tmp_path, defaults, raw, expected):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"volume": raw}))
    assert settings.load(defaults, FORMATS, path).volume == expected


def test_non_finite_volume_falls_back(tmp_path, defaults):
    path = tmp_path / "settings.json"
    path.write_text('{"volume": NaN}')  # Python's json reads and writes NaN
    assert settings.load(defaults, FORMATS, path).volume == defaults.volume


@pytest.mark.parametrize("style", settings.STYLES)
@pytest.mark.parametrize("accent", settings.ACCENTS)
def test_every_style_and_accent_round_trips(tmp_path, defaults, style, accent):
    path = tmp_path / "settings.json"
    settings.save(Settings("opus", tmp_path, style=style, accent=accent), path)
    loaded = settings.load(defaults, FORMATS, path)
    assert (loaded.style, loaded.accent) == (style, accent)
