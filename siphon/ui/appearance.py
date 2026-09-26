"""Light or dark style and accent colour chosen in Siphon itself, from the settings cog; "system" follows the OS.

libadwaita reads the OS's choice through the settings portal on Linux and from the Windows app theme and
accent colour on Windows (its Win32 backend: dark style since 1.3, accent colour since 1.6).
"""

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import settings

_SCHEMES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK}
_STYLE_NAMES = {"system": "Follow System Style", "light": "Light Style", "dark": "Dark Style"}
_ACCENT_ROW = 5  # two rows of five dots: 180 px wide, near the three style swatches' 156 px


def color_scheme(style: str) -> Adw.ColorScheme:
    return _SCHEMES.get(style, Adw.ColorScheme.DEFAULT)


def accents_supported(version: tuple[int, int] | None = None) -> bool:
    """libadwaita 1.6 brought accent colours and the --accent-<name> variables; older ones have neither."""
    return (version or (Adw.get_major_version(), Adw.get_minor_version())) >= (1, 6)


def accent_css(accent: str) -> str:
    """The stylesheet that replaces the system accent, or "" to keep it.

    libadwaita derives everything else from --accent-bg-color: --accent-color (text, links, focus rings) is
    that colour with its lightness capped at 0.5 in light style and raised to at least 0.85 in dark, and
    --accent-fg-color stays white, as for the system's own accents.
    """
    if accent == "system" or accent not in settings.ACCENTS:
        return ""
    return f":root {{ --accent-bg-color: var(--accent-{accent}); }}\n"


def accent_name(accent: str) -> str:
    return "System Accent Colour" if accent == "system" else accent.capitalize()


class Appearance:
    """Applies the chosen style and accent to every window, dialog and popover of the display."""

    def __init__(self, display: Gdk.Display) -> None:
        self._accent = Gtk.CssProvider()
        # Above libadwaita's own accent (theme priority), level with Siphon's style.css.
        Gtk.StyleContext.add_provider_for_display(display, self._accent, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def set_style(self, style: str) -> None:
        Adw.StyleManager.get_default().set_color_scheme(color_scheme(style))

    def set_accent(self, accent: str) -> None:
        self._accent.load_from_string(accent_css(accent) if accents_supported() else "")


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
