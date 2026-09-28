"""The development stand-ins (SIPHON_FAKE_*) must offer everything the window and MPRIS use of the real modules."""

import dataclasses
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest
from gi.repository import GObject

from siphon import core, covers, library, playlists
from siphon.player import Player

TESTS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"siphon_fakes_{name}", TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fake_player = _load("fake_player")
fake_library = _load("fake_library")
fake_core = _load("fake_core")


def _public(cls: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls, callable) if not name.startswith("_")}


def test_the_fake_player_has_every_signal_of_the_real_one():
    assert set(GObject.signal_list_names(Player)) <= set(GObject.signal_list_names(fake_player.Player))


def test_the_fake_player_has_every_public_method_of_the_real_one():
    assert _public(Player) - set(dir(GObject.Object)) <= _public(fake_player.Player)


@pytest.mark.parametrize("real, fake", [(library.Library, fake_library.Library),
                                        (playlists.Playlists, fake_library.Playlists)])
def test_the_fake_library_has_every_public_method_of_the_real_one(real, fake):
    assert _public(real) <= _public(fake)


def test_the_fake_songs_and_playlists_have_the_real_fields():
    for real, fake in ((library.Song, fake_library.Song), (playlists.Playlist, fake_library.Playlist)):
        assert {f.name for f in dataclasses.fields(real)} <= {f.name for f in dataclasses.fields(fake)}
    assert {"display_artist", "key"} <= set(dir(fake_library.Song))


def test_the_fake_covers_match_the_real_signature():
    assert inspect.signature(covers.cover_file).parameters.keys() == \
        inspect.signature(fake_library.cover_file).parameters.keys()


def test_the_fake_core_has_the_real_fields_and_what_the_window_calls():
    for real, fake in ((core.Track, fake_core.Track), (core.Resolved, fake_core.Resolved)):
        assert {f.name for f in dataclasses.fields(real)} <= {f.name for f in dataclasses.fields(fake)}
    for name in ("resolve", "download", "is_supported", "source_link", "cover_image"):
        assert inspect.signature(getattr(core, name)).parameters.keys() == \
            inspect.signature(getattr(fake_core, name)).parameters.keys()


def test_the_fake_core_words_the_formats_as_the_real_one():
    for name in ("FORMATS", "FORMAT_LABELS", "FORMAT_NOTES", "FORMAT_QUALITY", "QUALITY_BLOCKS"):
        assert getattr(fake_core, name) == getattr(core, name), name
