import re
from pathlib import Path

import pytest

from siphon import settings
from siphon.ui import appearance
from gi.repository import Adw, Gio, Gtk  # noqa: E402  siphon.ui pins the versions first

STYLE_CSS = Path(appearance.__file__).with_name("style.css")
NAMED = [accent for accent in settings.ACCENTS if accent != "system"]


@pytest.mark.parametrize("style, scheme", [("system", Adw.ColorScheme.DEFAULT),
                                           ("light", Adw.ColorScheme.FORCE_LIGHT),
                                           ("dark", Adw.ColorScheme.FORCE_DARK)])
def test_each_style_forces_its_scheme_and_system_follows_the_os(style, scheme):
    assert appearance.color_scheme(style) == scheme


def test_every_style_has_a_scheme():
    assert set(appearance._SCHEMES) == set(settings.STYLES)


@pytest.mark.parametrize("style", ["Dark", "auto", ""])
def test_unknown_style_follows_the_os(style):
    assert appearance.color_scheme(style) == Adw.ColorScheme.DEFAULT


@pytest.mark.parametrize("accent", NAMED)
def test_named_accent_replaces_the_accent_background_only(accent):
    # --accent-color and --accent-fg-color derive from it in libadwaita's own stylesheet.
    assert appearance.accent_css(accent) == f":root {{ --accent-bg-color: var(--accent-{accent}); }}\n"


@pytest.mark.parametrize("accent", ["system", "magenta", "Purple", "", "purple; } * { color: red"])
def test_system_or_unknown_accent_leaves_the_system_one(accent):
    assert appearance.accent_css(accent) == ""


@pytest.mark.parametrize("version, supported", [((1, 5), False), ((1, 6), True), ((1, 10), True), ((2, 0), True)])
def test_accents_need_libadwaita_1_6(version, supported):
    assert appearance.accents_supported(version) is supported


def test_accent_names():
    assert appearance.accent_name("system") == "System Accent Colour"
    assert appearance.accent_name("slate") == "Slate"


@pytest.mark.skipif(not appearance.accents_supported(), reason="libadwaita before 1.6 has no named accents")
def test_every_named_accent_is_a_libadwaita_variable():
    css = Gio.resources_lookup_data("/org/gnome/Adwaita/styles/gtk.css", Gio.ResourceLookupFlags.NONE)
    defined = set(re.findall(r"--accent-([a-z]+):", css.get_data().decode()))
    assert set(NAMED) <= defined


def test_every_swatch_has_its_colour_in_the_stylesheet():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for accent in NAMED:
        assert f".appearance .accents .{accent} {{ background-color: var(--accent-{accent}); }}" in css
    for style in settings.STYLES:
        assert f".appearance .styles .{style} {{" in css


@pytest.mark.parametrize("css", [appearance.accent_css("purple"), STYLE_CSS.read_text(encoding="utf-8")])
def test_stylesheets_parse_without_errors(css):
    errors = []
    provider = Gtk.CssProvider()
    provider.connect("parsing-error", lambda _provider, _section, error: errors.append(error.message))
    provider.load_from_string(css)
    assert errors == []
