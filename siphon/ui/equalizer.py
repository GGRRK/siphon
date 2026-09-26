"""The Settings dialog's Equalizer page: on or off, the presets (built-in and your own) and ten band sliders.

Every change plays at once and is saved through SiphonApp.apply_equalizer(); the page never calls the player.
"""

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import eq
from .dialogs import ask_name, confirm
from .songmenu import quoted

_SLIDER_HEIGHT = 168  # 144 px of trough inside Adwaita's padding (measured): 3 px for each of the 48 half-dB steps
_PAGE_DB = 3.0  # Page Up/Down; the arrows move one STEP_DB
_VALUE_SHOWN_MS = 1000  # the readout stays this long after the last change, unless a slider is still held


def band_name(hz: int) -> str:
    """What a screen reader calls a slider: "31 Hz", "1 kHz"."""
    return f"{hz // 1000} kHz" if hz >= 1000 else f"{hz} Hz"


def decibels(db: float) -> str:
    """A band's gain as the readout shows it: "+3.5 dB", "-12 dB", "0 dB"."""
    return f"{db:+g} dB" if db else "0 dB"


def preset_names(equalizer: eq.Equalizer) -> list[str]:
    """The preset chooser's entries: every preset, and Custom last while the curve is none of them."""
    names = equalizer.names()
    return [*names, eq.CUSTOM] if equalizer.preset == eq.CUSTOM else names


def name_problem(name: str) -> str:
    """Why a preset can't have this name, or ""."""
    try:
        eq.clean_name(name)
    except ValueError as exc:
        return str(exc)
    return ""


class EqualizerPage(Adw.PreferencesPage):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(title="Equalizer", icon_name="audio-speakers-symbolic", name="equalizer")
        self._app = app
        self._syncing = False  # set while the page moves its own controls to match the model
        self._held: Gtk.Scale | None = None
        self._hide_source = 0

        self._switch = Adw.SwitchRow(title="Use Equalizer", active=self._eq.enabled)
        self._switch.connect("notify::active", self._on_switch)
        self._names: list[str] = []
        self._model = Gtk.StringList()
        self._preset = Adw.ComboRow(title="Preset", model=self._model)
        self._preset.connect("notify::selected", self._on_preset)
        top = Adw.PreferencesGroup()
        top.add(self._switch)
        top.add(self._preset)
        self.add(top)

        self._scales: list[Gtk.Scale] = []
        bank = Gtk.Box(homogeneous=True)
        for index, (hz, label) in enumerate(zip(eq.FREQUENCIES, eq.LABELS)):
            bank.append(self._band(index, hz, label))
        # The readout floats over the band being moved: a label per band would not fit ten across a narrow dialog.
        # An overlay, whose overlaid children never widen it: a Gtk.Fixed grows to reach its children, and kept
        # the page 592 px wide in a 360 px window once the readout had been over the 16k band (measured).
        self._value = self._readout()
        self._value.set_halign(Gtk.Align.START)
        self._strip = Gtk.Overlay(child=self._readout())  # the same label unseen, for the strip's height
        self._strip.add_overlay(self._value)
        self._bands = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "equalizer"])
        self._bands.append(self._strip)
        self._bands.append(bank)

        # Reset sits in the header: in one row with the two preset buttons it needed 360 px (measured), more than
        # a 360 px window leaves the page
        self._reset = Gtk.Button(label="Reset", tooltip_text="Set Every Band to 0 dB", valign=Gtk.Align.CENTER,
                                 css_classes=["flat"])
        self._reset.connect("clicked", self._on_reset)
        self._delete = Gtk.Button(label="Delete Preset")
        self._delete.connect("clicked", self._on_delete)
        self._save = Gtk.Button(label="Save Preset")
        self._save.connect("clicked", self._on_save)
        buttons = Gtk.Box(spacing=12, margin_top=12, halign=Gtk.Align.END)
        buttons.append(self._delete)
        buttons.append(self._save)
        curve = Adw.PreferencesGroup(title="Bands", header_suffix=self._reset)
        curve.add(self._bands)
        curve.add(buttons)
        self.add(curve)

        self._sync_bands()
        self._sync_preset()
        self._sync_sensitive()

    @staticmethod
    def _readout() -> Gtk.Label:
        return Gtk.Label(label=decibels(0.0), opacity=0, accessible_role=Gtk.AccessibleRole.PRESENTATION,
                         css_classes=["band-value", "caption", "numeric"])

    @property
    def _eq(self) -> eq.Equalizer:
        return self._app.prefs.equalizer

    def _band(self, index: int, hz: int, label: str) -> Gtk.Box:
        adjustment = Gtk.Adjustment(lower=eq.MIN_DB, upper=eq.MAX_DB, step_increment=eq.STEP_DB,
                                    page_increment=_PAGE_DB)
        # inverted: +12 dB at the top, and the Up arrow raises the gain
        scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=adjustment, inverted=True,
                          draw_value=False, digits=1, has_origin=False, height_request=_SLIDER_HEIGHT,
                          vexpand=True, halign=Gtk.Align.CENTER)
        for side in (Gtk.PositionType.LEFT, Gtk.PositionType.RIGHT):  # 0 dB: a line across all ten (style.css)
            scale.add_mark(0.0, side, None)
        scale.update_property([Gtk.AccessibleProperty.LABEL], [band_name(hz)])
        scale.connect("value-changed", self._on_band, index)
        held = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        held.connect("event", self._on_scale_event, scale)
        scale.add_controller(held)
        self._scales.append(scale)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.append(scale)
        column.append(Gtk.Label(label=label, css_classes=["caption", "dim-label", "numeric"]))
        return column

    # -- the model into the controls

    def _sync_bands(self) -> None:
        self._syncing = True
        for scale, db in zip(self._scales, self._eq.bands):
            scale.set_value(db)
            scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [decibels(db)])
        self._syncing = False

    def _sync_preset(self) -> None:
        names = preset_names(self._eq)
        self._syncing = True
        if names != self._names:
            self._model.splice(0, len(self._names), names)
            self._names = names
        self._preset.set_selected(names.index(self._eq.preset))
        self._syncing = False

    def _sync_sensitive(self) -> None:
        on = self._eq.enabled
        for widget in (self._preset, self._bands, self._save):
            widget.set_sensitive(on)
        self._reset.set_sensitive(on and self._eq.bands != eq.FLAT)
        self._delete.set_sensitive(on and self._eq.preset in self._eq.presets)

    def _changed(self) -> None:
        """After any change to the model: play and save it, then show what it now is."""
        self._app.apply_equalizer()
        self._sync_preset()
        self._sync_sensitive()

    # -- the controls into the model

    def _on_switch(self, row: Adw.SwitchRow, _pspec) -> None:
        self._eq.enabled = row.get_active()
        self._changed()

    def _on_preset(self, row: Adw.ComboRow, _pspec) -> None:
        if self._syncing or row.get_selected() >= len(self._names):
            return
        name = self._names[row.get_selected()]
        if name in (eq.CUSTOM, self._eq.preset):
            return
        self._eq.apply(name)
        self._sync_bands()
        # Custom leaves the list: not from inside the list's own selection change
        GLib.idle_add(self._after_preset)

    def _after_preset(self) -> bool:
        self._changed()
        return GLib.SOURCE_REMOVE

    def _on_band(self, scale: Gtk.Scale, index: int) -> None:
        if self._syncing:
            return
        db = eq.snap(scale.get_value())
        if db != scale.get_value():  # a drag lands between steps; the slider shows the step that plays
            self._syncing = True
            scale.set_value(db)
            self._syncing = False
        scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [decibels(db)])
        self._show_value(index, db)
        if db != self._eq.bands[index]:
            self._eq.set_band(index, db)
            self._changed()

    def _on_reset(self, _button: Gtk.Button) -> None:
        self._eq.apply("Flat")
        self._sync_bands()
        self._changed()

    def _on_save(self, _button: Gtk.Button) -> None:
        current = self._eq.preset
        ask_name(self, "Save Preset", "Save", self._save_as, initial=current if current in self._eq.presets else "",
                 label="Preset name", problem=name_problem)

    def _save_as(self, name: str) -> None:
        existing = self._eq.find(name)
        if existing is None:
            self._keep(name)
            return
        confirm(self, f"Replace {quoted(existing)}?", "The preset takes the equalizer's current curve.", "Replace",
                lambda: self._keep(name))

    def _keep(self, name: str) -> None:
        self._eq.save(name)  # no ValueError: ask_name let only a name through that name_problem passed
        self._changed()

    def _on_delete(self, _button: Gtk.Button) -> None:
        name = self._eq.preset  # one of yours: _sync_sensitive() leaves the button off for any other

        def delete() -> None:
            self._eq.delete(name)
            self._changed()

        confirm(self, f"Delete {quoted(name)}?", "The equalizer keeps its current curve.", "Delete", delete)

    # -- the readout

    def _on_scale_event(self, _controller: Gtk.EventControllerLegacy, event: Gdk.Event, scale: Gtk.Scale) -> bool:
        kind = event.get_event_type()
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._held = scale
        elif kind in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            self._held = None
            self._hide_later()
        return Gdk.EVENT_PROPAGATE

    def _show_value(self, index: int, db: float) -> None:
        self._value.set_label(decibels(db))
        # measure() counts the margin that places the readout; without it, each move drifted from the last
        width = self._value.measure(Gtk.Orientation.HORIZONTAL, -1)[1] - self._value.get_margin_start()
        strip = self._strip.get_width()
        centre = strip * (index + 0.5) / len(self._scales)  # the bands share the strip's width equally
        self._value.set_margin_start(round(max(0.0, min(centre - width / 2, strip - width))))
        self._value.set_opacity(1)
        self._hide_later()

    def _hide_later(self) -> None:
        if self._hide_source:
            GLib.source_remove(self._hide_source)
        self._hide_source = GLib.timeout_add(_VALUE_SHOWN_MS, self._hide_value)

    def _hide_value(self) -> bool:
        self._hide_source = 0
        if self._held is None:
            self._value.set_opacity(0)
        return GLib.SOURCE_REMOVE
