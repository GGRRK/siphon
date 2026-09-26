"""Light or dark style and accent colour chosen in Siphon itself, from the settings cog; "system" follows the OS.

libadwaita reads the OS's choice through the settings portal on Linux and from the Windows app theme and
accent colour on Windows (its Win32 backend: dark style since 1.3, accent colour since 1.6).

A desktop theme can recolour libadwaita apps through ~/.config/gtk-4.0/gtk.css, which GTK loads at user
priority, above anything an app loads at application priority. That file may fix every surface colour: a
dark theme's file made "Light" draw dark surfaces and left a dark accent text on any accent chosen here
(measured 2026-09-26 with rrk-shell's gtk.css). So a style or accent chosen here is loaded just above user
priority: "Light" and "Dark" restore libadwaita's own palette for that style, read from libadwaita's
stylesheet so it matches the running version, and "System" leaves the desktop's colours alone.
"""

import re

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import settings

_SCHEMES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK}
_STYLE_NAMES = {"system": "Follow System Style", "light": "Light Style", "dark": "Dark Style"}
_ACCENT_ROW = 5  # two rows of five dots: 180 px wide, near the three style swatches' 156 px
_ADWAITA_CSS = "/org/gnome/Adwaita/styles/gtk.css"
_DARK = "(prefers-color-scheme: dark)"
_TOKENS = re.compile(r"/\*.*?\*/|@define-color\s+(\w+)\s+([^;]+);|@media([^{]*)\{|\{|\}", re.S)
PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1


def color_scheme(style: str) -> Adw.ColorScheme:
    return _SCHEMES.get(style, Adw.ColorScheme.DEFAULT)


def accents_supported(version: tuple[int, int] | None = None) -> bool:
    """libadwaita 1.6 brought accent colours and the --accent-<name> variables; older ones have neither."""
    return (version or (Adw.get_major_version(), Adw.get_minor_version())) >= (1, 6)


def accent_css(accent: str) -> str:
    """The stylesheet that replaces the system accent, or "" to keep it.

    libadwaita derives --accent-color (text, links, focus rings) from --accent-bg-color: its lightness capped
    at 0.5 in light style and raised to at least 0.85 in dark. --accent-fg-color is set to libadwaita's white
    because a desktop theme's gtk.css may have made it a colour chosen for its own accent.
    """
    if accent == "system" or accent not in settings.ACCENTS:
        return ""
    return f":root {{ --accent-bg-color: var(--accent-{accent}); --accent-fg-color: white; }}\n"


def adwaita_palette(css: str, dark: bool) -> dict[str, str]:
    """libadwaita's named colours for one style: those at the top level of its stylesheet, overridden in dark
    style by those in its (prefers-color-scheme: dark) block. Other media blocks (high contrast) are skipped."""
    top: dict[str, str] = {}
    night: dict[str, str] = {}
    media: list[str] = []
    for match in _TOKENS.finditer(css):
        token = match.group(0)
        if token.startswith("/*"):
            continue
        if match.group(1):
            where = [m for m in media if m]
            if not where:
                top[match.group(1)] = match.group(2).strip()
            elif where == [_DARK]:
                night[match.group(1)] = match.group(2).strip()
        elif match.group(3) is not None:
            media.append(" ".join(match.group(3).split()))
        elif token == "{":
            media.append("")
        elif media:
            media.pop()
    return {**top, **night} if dark else top


def palette_css(style: str, adwaita_css: str) -> str:
    """For "light" or "dark", libadwaita's own palette as @define-color rules; "" for "system"."""
    if style not in ("light", "dark"):
        return ""
    palette = adwaita_palette(adwaita_css, style == "dark")
    return "".join(f"@define-color {name} {value};\n" for name, value in palette.items())


def accent_name(accent: str) -> str:
    return "System Accent Colour" if accent == "system" else accent.capitalize()


class Appearance:
    """Applies the chosen style and accent to every window, dialog and popover of the display."""

    def __init__(self, display: Gdk.Display) -> None:
        self._style = "system"
        self._accent = "system"
        self._provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self._provider, PRIORITY)
        try:
            self._adwaita = Gio.resources_lookup_data(_ADWAITA_CSS, Gio.ResourceLookupFlags.NONE).get_data().decode()
        except GLib.Error:  # never seen; without it a desktop theme's gtk.css keeps its surfaces
            self._adwaita = ""

    def set_style(self, style: str) -> None:
        self._style = style
        Adw.StyleManager.get_default().set_color_scheme(color_scheme(style))
        self._load()

    def set_accent(self, accent: str) -> None:
        self._accent = accent if accents_supported() else "system"
        self._load()

    def _load(self) -> None:
        # one stylesheet, the accent last: libadwaita's palette also names accent_color, which the accent overrides
        self._provider.load_from_string(palette_css(self._style, self._adwaita) + accent_css(self._accent))


def _swatch(action: str, value: str, name: str, classes: list[str], check_align: Gtk.Align) -> Gtk.CheckButton:
    """A round radio choice bound to a stateful app action; a check mark shows on the chosen one."""
    check = Gtk.Image(icon_name="object-select-symbolic", halign=check_align, valign=check_align,
                      hexpand=True, css_classes=["check-mark"])
    button = Gtk.CheckButton(child=check, action_name=action, tooltip_text=name, focus_on_click=False,
                             css_classes=["swatch", *classes])
    button.set_action_target_value(GLib.Variant.new_string(value))
    button.update_property([Gtk.AccessibleProperty.LABEL], [name])
    return button


def settings_button() -> Gtk.MenuButton:
    """The header bar's cog: a popover with the style swatches and, where libadwaita has them, accent dots."""
    styles = Gtk.Box(spacing=12, homogeneous=True, halign=Gtk.Align.CENTER, css_classes=["styles"])
    for style in settings.STYLES:
        styles.append(_swatch("app.style", style, _STYLE_NAMES[style], [style], Gtk.Align.END))
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["appearance"])
    content.append(styles)
    if accents_supported():
        accents = Gtk.Grid(row_spacing=10, column_spacing=10, halign=Gtk.Align.CENTER, css_classes=["accents"])
        for index, accent in enumerate(settings.ACCENTS):
            accents.attach(_swatch("app.accent", accent, accent_name(accent), [accent], Gtk.Align.CENTER),
                           index % _ACCENT_ROW, index // _ACCENT_ROW, 1, 1)
        content.append(Gtk.Separator())
        content.append(accents)
    return Gtk.MenuButton(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                          popover=Gtk.Popover(child=content))
