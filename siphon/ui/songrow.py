"""One song in a list: cover, title, artist · album, length, and its menu (⋮ button or right-click)."""

from collections.abc import Callable
from typing import Any

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .art import Cover, CoverArt
from .fmt import clock, drop_index


class SongItem(GObject.Object):
    """A list-model entry: the song, where it sits (playlist or queue index), and whether its file is gone."""

    def __init__(self, song: Any, index: int = -1, missing: bool = False) -> None:
        super().__init__()
        self.song = song
        self.index = index
        self.missing = missing


MenuFor = Callable[[SongItem], Gio.MenuModel | None]
Reorder = Callable[[int, int], None]


class SongRow(Gtk.Box):
    def __init__(self, player: Any, art: CoverArt, menu_for: MenuFor | None = None,
                 reorder: Reorder | None = None, cover_size: int = 40) -> None:
        super().__init__(spacing=12, margin_top=3, margin_bottom=3)
        self.add_css_class("song-row")
        self.item: SongItem | None = None
        self._cell: Gtk.ListItem | None = None
        self._player = player
        self._menu_for = menu_for

        if reorder is not None:
            self.append(Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["dim-label"],
                                  tooltip_text="Drag to Reorder"))
        self._cover = Cover(art, cover_size)
        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["song-title"])
        self._subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(self._title)
        text.append(self._subtitle)
        self._length = Gtk.Label(css_classes=["caption", "dim-label", "numeric"])
        self.append(self._cover)
        self.append(text)
        self.append(self._length)

        if menu_for is not None:
            self._menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                               tooltip_text="More")
            self._menu_button.add_css_class("flat")
            self._menu_button.add_css_class("circular")
            self._menu_button.set_create_popup_func(self._fill_menu)
            self.append(self._menu_button)
            self._context = Gtk.PopoverMenu(has_arrow=False, halign=Gtk.Align.START)
            self._context.set_parent(self)
            click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            click.connect("pressed", lambda gesture, _n, x, y: self._on_context(gesture, x, y))
            self.add_controller(click)
            press = Gtk.GestureLongPress(touch_only=True)
            press.connect("pressed", self._on_context)
            self.add_controller(press)
        # One click plays, like the list's own single-click mode, which would swallow the drag gesture.
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)
        if reorder is not None:
            self._install_drag(reorder)
        self._player_handler = player.connect("changed", lambda _p: self._sync_playing())

    # -- binding

    def bind(self, cell: Gtk.ListItem) -> None:
        self._cell = cell
        self.item = item = cell.get_item()
        song = item.song
        self._title.set_label(song.title)
        detail = "File missing" if item.missing else " · ".join(filter(None, [song.display_artist, song.album]))
        self._subtitle.set_label(detail)
        self._length.set_label(clock(song.duration) if song.duration else "")
        self._cover.show(None if item.missing else song.path)
        if item.missing:
            self.add_css_class("missing")
        else:
            self.remove_css_class("missing")
        self._sync_playing()

    def unbind(self) -> None:
        self.item = self._cell = None

    def teardown(self) -> None:
        self._player.disconnect(self._player_handler)
        if self._menu_for is not None:
            self._context.unparent()

    def _sync_playing(self) -> None:
        current = self._player.current
        playing = self.item is not None and current is not None and current.key == self.item.song.key
        if playing:
            self.add_css_class("playing")
        else:
            self.remove_css_class("playing")

    # -- activation

    def _on_pressed(self, gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        if n_press > 1:  # keep the list from activating the row a second time on a double click
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_released(self, _gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        if n_press == 1 and self._cell is not None:
            self.activate_action("list.activate-item", GLib.Variant("u", self._cell.get_position()))

    # -- menu

    def _fill_menu(self, button: Gtk.MenuButton) -> None:
        button.set_menu_model(self._menu_for(self.item) if self.item else None)

    def _on_context(self, gesture: Gtk.Gesture, x: float, y: float) -> None:
        model = self._menu_for(self.item) if self.item else None
        if model is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._context.set_menu_model(model)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._context.set_pointing_to(rect)
        self._context.popup()

    # -- drag and drop reordering

    def _install_drag(self, reorder: Reorder) -> None:
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", self._on_drag_prepare)
        source.connect("drag-begin", self._on_drag_begin)
        self.add_controller(source)
        target = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_drop_motion)
        target.connect("leave", lambda _t: self._mark_drop(None))
        target.connect("drop", self._on_drop, reorder)
        self.add_controller(target)

    def _on_drag_prepare(self, _source: Gtk.DragSource, _x: float, _y: float) -> Gdk.ContentProvider | None:
        if self.item is None:
            return None
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_INT, self.item.index))

    def _on_drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        source.set_icon(Gtk.WidgetPaintable.new(self), 24, self.get_height() // 2)

    def _on_drop_motion(self, _target: Gtk.DropTarget, _x: float, y: float) -> Gdk.DragAction:
        self._mark_drop(y > self.get_height() / 2)
        return Gdk.DragAction.MOVE

    def _mark_drop(self, after: bool | None) -> None:
        for name, on in (("drop-before", after is False), ("drop-after", after is True)):
            if on:
                self.add_css_class(name)
            else:
                self.remove_css_class(name)

    def _on_drop(self, _target: Gtk.DropTarget, src: int, _x: float, y: float, reorder: Reorder) -> bool:
        self._mark_drop(None)
        if self.item is None:
            return False
        dst = drop_index(src, self.item.index, y > self.get_height() / 2)
        if dst != src:
            reorder(src, dst)
        return True


def song_list(model: Gio.ListModel, make_row: Callable[[], SongRow]) -> Gtk.ListView:
    """A ListView of SongItems; the caller handles its "activate" signal (a click, or Enter)."""
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, cell: (cell.set_selectable(False), cell.set_child(make_row())))
    factory.connect("bind", lambda _f, cell: cell.get_child().bind(cell))
    factory.connect("unbind", lambda _f, cell: cell.get_child().unbind())
    factory.connect("teardown", lambda _f, cell: cell.get_child() and cell.get_child().teardown())
    view = Gtk.ListView(model=Gtk.NoSelection(model=model), factory=factory)
    view.add_css_class("song-list")
    view.add_css_class("navigation-sidebar")
    return view
