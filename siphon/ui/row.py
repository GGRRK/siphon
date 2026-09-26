"""One line of the queue: a track (or a link still being read) and its state."""

from collections.abc import Callable
from enum import Enum

from gi.repository import GLib, Gtk, Pango


class State(Enum):
    READING = "Reading link…"
    QUEUED = "Queued"
    MATCHING = "Finding the song…"
    DOWNLOADING = "Downloading…"
    CONVERTING = "Converting…"
    TAGGING = "Tagging…"
    CANCELLING = "Cancelling…"
    DONE = "Done"
    ALREADY = "Already downloaded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


ACTIVE = {State.READING, State.QUEUED, State.MATCHING, State.DOWNLOADING,
          State.CONVERTING, State.TAGGING, State.CANCELLING}
FINISHED = {State.DONE, State.ALREADY, State.FAILED, State.CANCELLED}
_WITH_BAR = ACTIVE - {State.QUEUED}
_STATE_STYLE = {State.DONE: "success", State.FAILED: "error"}
_BUTTON = {  # state group -> (icon, tooltip)
    "cancel": ("window-close-symbolic", "Cancel"),
    "retry": ("view-refresh-symbolic", "Retry"),
    "show": ("folder-open-symbolic", "Show in Folder"),
}


def button_role(state: State) -> str:
    if state in ACTIVE:
        return "cancel"
    return "show" if state in (State.DONE, State.ALREADY) else "retry"


class TrackRow(Gtk.ListBoxRow):
    def __init__(self, title: str, on_action: Callable[["TrackRow"], None], on_changed: Callable[[], None]) -> None:
        super().__init__(activatable=False, selectable=False)
        self.state = State.QUEUED
        self._on_changed = on_changed
        self._subtitle_text = ""
        self._pulse_source = 0

        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._subtitle = Gtk.Label(xalign=0, hexpand=True,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                                   css_classes=["caption", "dim-label"])
        self._state_label = Gtk.Label(xalign=1, valign=Gtk.Align.START, css_classes=["caption", "numeric"])
        self._bar = Gtk.ProgressBar(margin_top=6)
        # Style classes are added, not passed to the constructor, which would drop GTK's own ones.
        self._button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._button.add_css_class("flat")
        self._button.add_css_class("circular")
        self._button.connect("clicked", lambda _button: on_action(self))

        line = Gtk.Box(spacing=12)
        line.append(self._subtitle)
        line.append(self._state_label)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(self._title)
        text.append(line)
        text.append(self._bar)
        box = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=10, margin_bottom=10)
        box.append(text)
        box.append(self._button)
        self.set_child(box)

        self.set_title(title)
        self._state_label.set_label(self.state.value)
        self._restyle()

    def set_title(self, title: str) -> None:
        self._title.set_label(title)
        self._title.set_tooltip_text(title)

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle_text = subtitle
        if self.state is not State.FAILED:
            self._subtitle.set_label(subtitle)

    def fail(self, message: str) -> None:
        self.set_state(State.FAILED)
        # An error gets two lines; the usual "artist · source" gets one.
        self._subtitle.set_wrap(True)
        self._subtitle.set_lines(2)
        self._subtitle.set_label(message)
        self._subtitle.set_tooltip_text(message)

    def set_state(self, state: State, fraction: float | None = None) -> None:
        """Progress repeats the same state many times a second; only a new state restyles the row."""
        changed = state is not self.state
        self.state = state
        if state is State.DOWNLOADING and fraction is not None:
            self._state_label.set_label(f"Downloading {round(fraction * 100)}%")
        else:
            self._state_label.set_label(state.value)
        if state in _WITH_BAR and fraction is None:
            self._start_pulse()
        else:
            self._stop_pulse()
            self._bar.set_fraction(fraction or 0.0)
        if changed:
            self._restyle()
            self._on_changed()

    def _restyle(self) -> None:
        self._state_label.set_css_classes(["caption", "numeric", _STATE_STYLE.get(self.state, "dim-label")])
        self._subtitle.set_label(self._subtitle_text)
        self._subtitle.set_tooltip_text(None)
        self._subtitle.set_wrap(False)
        self._subtitle.set_lines(-1)
        self._bar.set_visible(self.state in _WITH_BAR)
        icon, tooltip = _BUTTON[button_role(self.state)]
        self._button.set_icon_name(icon)
        self._button.set_tooltip_text(tooltip)
        self._button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        self._button.set_sensitive(self.state is not State.CANCELLING)

    def _start_pulse(self) -> None:
        if not self._pulse_source:
            self._bar.pulse()
            self._pulse_source = GLib.timeout_add(120, self._pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def _pulse(self) -> bool:
        if self.get_parent() is None:  # removed from the queue while pulsing
            self._pulse_source = 0
            return GLib.SOURCE_REMOVE
        self._bar.pulse()
        return GLib.SOURCE_CONTINUE
