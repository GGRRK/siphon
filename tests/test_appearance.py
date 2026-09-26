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
def test_named_accent_replaces_the_accent_background_and_keeps_white_text_on_it(accent):
    # --accent-color derives from the background in libadwaita's own stylesheet; the text on it is set to
    # libadwaita's white because a desktop theme's gtk.css may have picked a colour for its own accent
    assert appearance.accent_css(accent) == (
        f":root {{ --accent-bg-color: var(--accent-{accent}); --accent-fg-color: white; }}\n")


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


ADWAITA_LIKE = """
/* @define-color commented_color red; */
@define-color window_bg_color #fafafb;
@define-color view_fg_color rgb(0 0 6 / 80%);
@define-color theme_bg_color @window_bg_color;
:root { --window-bg-color: @window_bg_color; }
@media (prefers-color-scheme: dark) {
  @define-color window_bg_color #222226;
  :root { --standalone-color-oklab: max(l, 0.85) a b; }
  @define-color view_fg_color #ffffff;
}
@media (prefers-contrast: more) {
  @define-color borders rgb(0 0 6 / 50%);
}
.osd { color: white; }
"""


def test_the_light_palette_is_the_top_level_colours():
    assert appearance.adwaita_palette(ADWAITA_LIKE, dark=False) == {
        "window_bg_color": "#fafafb", "view_fg_color": "rgb(0 0 6 / 80%)", "theme_bg_color": "@window_bg_color"}


def test_the_dark_palette_overrides_them_with_the_dark_block_and_skips_high_contrast():
    assert appearance.adwaita_palette(ADWAITA_LIKE, dark=True) == {
        "window_bg_color": "#222226", "view_fg_color": "#ffffff", "theme_bg_color": "@window_bg_color"}


def test_system_style_restores_no_palette():
    assert appearance.palette_css("system", ADWAITA_LIKE) == ""
    assert appearance.palette_css("light", ADWAITA_LIKE).startswith("@define-color window_bg_color #fafafb;\n")


def test_the_real_libadwaita_palettes_cover_what_a_theme_overrides():
    css = Gio.resources_lookup_data("/org/gnome/Adwaita/styles/gtk.css", Gio.ResourceLookupFlags.NONE)
    css = css.get_data().decode()
    light, dark = appearance.adwaita_palette(css, False), appearance.adwaita_palette(css, True)
    for name in ("window_bg_color", "window_fg_color", "view_bg_color", "headerbar_bg_color", "card_bg_color",
                 "popover_bg_color", "dialog_bg_color", "sidebar_bg_color"):
        assert light[name] != dark[name], name
    for style in ("light", "dark"):  # GTK reads the whole restored palette back without an error
        errors = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda _provider, _section, error: errors.append(error.message))
        provider.load_from_string(appearance.palette_css(style, css) + appearance.accent_css("green"))
        assert errors == [], style


def test_every_swatch_has_its_colour_in_the_stylesheet():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for accent in NAMED:
        assert f".appearance .accents .{accent} {{ background-color: var(--accent-{accent}); }}" in css
    for style in settings.STYLES:
        assert f".appearance .styles .{style} {{" in css


PALETTE_LIKE = "@define-color window_bg_color #222226;\n@define-color theme_bg_color @window_bg_color;\n"


@pytest.mark.parametrize("css", [appearance.accent_css("purple"), STYLE_CSS.read_text(encoding="utf-8"), PALETTE_LIKE])
def test_stylesheets_parse_without_errors(css):
    errors = []
    provider = Gtk.CssProvider()
    provider.connect("parsing-error", lambda _provider, _section, error: errors.append(error.message))
    provider.load_from_string(css)
    assert errors == []
