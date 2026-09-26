"""The now-playing bar at the bottom of the window: song, transport, seek, shuffle/repeat, volume, Up Next."""

from typing import Any

from gi.repository import Gio, Gtk, Pango

from .art import Cover, CoverArt
from .fmt import clock, songs
from .songrow import SongItem, SongRow, song_list

_REPEAT_NEXT = {"off": "all", "all": "one", "one": "off"}
_REPEAT_LOOK = {  # mode -> (icon, tooltip)
    "off": ("media-playlist-consecutive-symbolic", "Repeat: Off"),
    "all": ("media-playlist-repeat-symbolic", "Repeat: All"),
    "one": ("media-playlist-repeat-song-symbolic", "Repeat: One Song"),
}


def _flat(widget: Gtk.Widget) -> Gtk.Widget:
    widget.add_css_class("flat")
    widget.set_valign(Gtk.Align.CENTER)
    return widget


class NowPlaying(Gtk.Revealer):
    def __init__(self, player: Any, art: CoverArt) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self._player = player
        self._syncing = False  # set while the widgets are updated from the player, so they do not echo back
        self._dragging = False
        self._shuffles: list[Gtk.ToggleButton] = []
        self._repeats: list[Gtk.ToggleButton] = []

        self._elapsed = Gtk.Label(label="0:00", width_chars=5, xalign=1, css_classes=["caption", "numeric"])
        self._total = Gtk.Label(label="0:00", width_chars=5, xalign=0, css_classes=["caption", "numeric"])
        self._seek = Gtk.Scale(hexpand=True, adjustment=Gtk.Adjustment(lower=0, upper=1, step_increment=5,
                                                                       page_increment=30))
        self._seek.update_property([Gtk.AccessibleProperty.LABEL], ["Position"])
        self._seek.connect("change-value", self._on_seek_change)
        # GtkRange marks a slider drag with the "dragging" style class, from press to release.
        self._seek.connect("notify::css-classes", self._on_seek_drag)
        seek_row = Gtk.Box(spacing=6)
        for child in (self._elapsed, self._seek, self._total):
            seek_row.append(child)

        self._cover = Cover(art, 48)
        # Capped natural widths keep a long title from pushing the transport buttons off centre.
        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26,
                                css_classes=["heading"])
        self._artist = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=30,
                                 css_classes=["caption", "dim-label"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        text.append(self._title)
        text.append(self._artist)
        song = Gtk.Box(spacing=12)
        song.append(self._cover)
        song.append(text)

        previous = _flat(Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Previous"))
        previous.add_css_class("circular")
        previous.connect("clicked", lambda _b: player.previous())
        self._play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["circular", "suggested-action", "play-button"])
        self._play.connect("clicked", lambda _b: player.toggle())
        following = _flat(Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Next"))
        following.add_css_class("circular")
        following.connect("clicked", lambda _b: player.next())
        transport = Gtk.Box(spacing=6, margin_start=12, margin_end=12)
        for child in (previous, self._play, following):
            transport.append(child)

        # Popovers open upwards, away from the bottom edge of the window.
        self._volume = _flat(Gtk.MenuButton(tooltip_text="Volume", popover=self._volume_popover(),
                                            direction=Gtk.ArrowType.UP))
        self._queue_button = _flat(Gtk.MenuButton(icon_name="view-list-ordered-symbolic", tooltip_text="Up Next",
                                                  popover=self._queue_popover(art), direction=Gtk.ArrowType.UP))
        self._extras = [self._shuffle_button(), self._repeat_button(), self._volume]
        extras = Gtk.Box(spacing=2)
        for child in (*self._extras, self._queue_button):
            extras.append(child)

        main_row = Gtk.CenterBox(start_widget=song, center_widget=transport, end_widget=extras)
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=8,
                      margin_start=12, margin_end=12, css_classes=["now-playing"])
        bar.append(seek_row)
        bar.append(main_row)
        self.set_child(bar)

        player.connect("changed", lambda _p: self._sync())
        player.connect("position", lambda _p, seconds: self._show_position(seconds))
        self._sync()

    def set_narrow(self, narrow: bool) -> None:
        """Narrow windows keep shuffle and repeat in the Up Next popover and leave volume to the system."""
        for widget in self._extras:
            widget.set_visible(not narrow)
        self._popover_toggles.set_visible(narrow)

    # -- building

    def _shuffle_button(self) -> Gtk.ToggleButton:
        button = _flat(Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Shuffle"))
        button.connect("toggled", self._on_shuffle_toggled)
        self._shuffles.append(button)
        return button

    def _repeat_button(self) -> Gtk.ToggleButton:
        button = _flat(Gtk.ToggleButton())
        button.connect("clicked", self._on_repeat_clicked)
        self._repeats.append(button)
        return button

    def _volume_popover(self) -> Gtk.Popover:
        self._volume_scale = Gtk.Scale(adjustment=Gtk.Adjustment(lower=0, upper=100, step_increment=5,
                                                                 page_increment=10), width_request=200, hexpand=True)
        self._volume_scale.update_property([Gtk.AccessibleProperty.LABEL], ["Volume"])
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        self._volume_label = Gtk.Label(width_chars=4, xalign=1, css_classes=["numeric"])
        box = Gtk.Box(spacing=6, margin_start=6, margin_end=6)
        box.append(Gtk.Image(icon_name="audio-volume-low-symbolic"))
        box.append(self._volume_scale)
        box.append(self._volume_label)
        return Gtk.Popover(child=box)

    def _queue_popover(self, art: CoverArt) -> Gtk.Popover:
        self._up_next = Gio.ListStore(item_type=SongItem)
        view = song_list(self._up_next, lambda: SongRow(self._player, art, cover_size=32))
        view.connect("activate", lambda _v, position: self._player.jump(self._up_next.get_item(position).index))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=420, child=view)
        empty = Gtk.Label(label="Nothing up next", margin_top=24, margin_bottom=24, css_classes=["dim-label"])
        self._queue_stack = Gtk.Stack(vhomogeneous=False)
        self._queue_stack.add_named(scroller, "list")
        self._queue_stack.add_named(empty, "empty")

        self._queue_count = Gtk.Label(xalign=0, css_classes=["caption", "dim-label", "numeric"])
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        heading.append(Gtk.Label(label="Up Next", xalign=0, css_classes=["heading"]))
        heading.append(self._queue_count)
        self._popover_toggles = Gtk.Box(spacing=2, visible=False)
        self._popover_toggles.append(self._shuffle_button())
        self._popover_toggles.append(self._repeat_button())
        top = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=6, margin_bottom=6)
        top.append(heading)
        top.append(self._popover_toggles)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=340)
        box.append(top)
        box.append(Gtk.Separator())
        box.append(self._queue_stack)
        popover = Gtk.Popover(child=box)
        popover.connect("show", lambda _p: self._fill_queue())
        return popover

    # -- the user's changes

    def _on_shuffle_toggled(self, button: Gtk.ToggleButton) -> None:
        if not self._syncing:
            self._player.set_shuffle(button.get_active())

    def _on_repeat_clicked(self, _button: Gtk.ToggleButton) -> None:
        if not self._syncing:
            self._player.set_repeat(_REPEAT_NEXT[self._player.repeat])

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        if not self._syncing:
            self._player.set_volume(scale.get_value() / 100)

    # -- following the player

    def _sync(self) -> None:
        player = self._player
        song = player.current
        self._syncing = True
        self.set_reveal_child(song is not None or bool(player.queue))
        if song is not None:
            self._title.set_label(song.title)
            self._title.set_tooltip_text(song.title)
            self._artist.set_label(" · ".join(filter(None, [song.display_artist, song.album])))
            self._cover.show(song.path)
        playing = player.state == "playing"
        self._play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self._play.set_tooltip_text("Pause" if playing else "Play")
        self._play.update_property([Gtk.AccessibleProperty.LABEL], ["Pause" if playing else "Play"])
        duration = player.duration or 0.0
        self._seek.set_range(0, max(duration, 1.0))
        self._seek.set_sensitive(duration > 0)
        self._total.set_label(clock(duration))
        for button in self._shuffles:
            button.set_active(player.shuffle)
        icon, tooltip = _REPEAT_LOOK[player.repeat]
        for button in self._repeats:
            button.set_icon_name(icon)
            button.set_tooltip_text(tooltip)
            button.set_active(player.repeat != "off")
        self._volume_scale.set_value(round(player.volume * 100))
        self._volume_label.set_label(f"{round(player.volume * 100)}%")
        self._volume.set_icon_name(self._volume_icon(player.volume))
        self._syncing = False
        self._show_position(player.position)
        if self._queue_button.get_active():
            self._fill_queue()

    @staticmethod
    def _volume_icon(volume: float) -> str:
        level = "muted" if volume <= 0 else "low" if volume < 0.34 else "medium" if volume < 0.67 else "high"
        return f"audio-volume-{level}-symbolic"

    def _show_position(self, seconds: float) -> None:
        if self._dragging:
            return
        self._syncing = True
        self._seek.set_value(seconds)
        self._syncing = False
        self._elapsed.set_label(clock(seconds))

    def _fill_queue(self) -> None:
        player = self._player
        upcoming = [SongItem(song, i) for i, song in enumerate(player.queue) if i > player.index]
        self._up_next.splice(0, self._up_next.get_n_items(), upcoming)
        self._queue_count.set_label(songs(len(upcoming)) if upcoming else "")
        self._queue_stack.set_visible_child_name("list" if upcoming else "empty")

    # -- seeking: a drag previews the time and seeks once, on release

    def _on_seek_drag(self, scale: Gtk.Scale, _pspec) -> None:
        dragging = scale.has_css_class("dragging")
        if dragging != self._dragging:
            self._dragging = dragging
            if not dragging:
                self._player.seek(scale.get_value())

    def _on_seek_change(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        if self._dragging:
            self._elapsed.set_label(clock(value))
        else:  # keyboard or scroll wheel
            self._player.seek(max(0.0, value))
        return False
