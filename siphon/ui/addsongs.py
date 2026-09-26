"""The "Add Songs" dialog: the library with a search and a checkbox per song."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gio, Gtk, Pango

from .art import Cover, CoverArt
from .fmt import clock, songs
from .music import Music
from .songrow import SongItem

Toggle = Callable[[SongItem, bool], None]


class _PickRow(Gtk.Box):
    def __init__(self, art: CoverArt, is_chosen: Callable[[SongItem], bool], on_toggle: Toggle) -> None:
        super().__init__(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.item: SongItem | None = None
        self._is_chosen = is_chosen
        self._check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self._toggled = self._check.connect("toggled", lambda c: self.item and on_toggle(self.item, c.get_active()))
        self._cover = Cover(art, 32)
        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(self._title)
        text.append(self._subtitle)
        self._length = Gtk.Label(css_classes=["caption", "dim-label", "numeric"])
        for child in (self._check, self._cover, text, self._length):
            self.append(child)

    def bind(self, item: SongItem) -> None:
        self.item = item
        song = item.song
        self._title.set_label(song.title)
        # item.missing doubles as "already in the playlist" here: shown ticked and locked.
        self._subtitle.set_label("Already in this playlist" if item.missing
                                 else " · ".join(filter(None, [song.display_artist, song.album])))
        self._length.set_label(clock(song.duration) if song.duration else "")
        self._cover.show(song.path)
        self._check.handler_block(self._toggled)
        self._check.set_active(item.missing or self._is_chosen(item))
        self._check.handler_unblock(self._toggled)
        self._check.set_sensitive(not item.missing)
        self._check.update_property([Gtk.AccessibleProperty.LABEL], [song.title])


class AddSongsDialog(Adw.Dialog):
    def __init__(self, music: Music, art: CoverArt, playlist: Any, on_add: Callable[[list[Path]], None]) -> None:
        super().__init__(title="Add Songs", content_width=560, content_height=700)
        self._music = music
        self._present = set(playlist.paths)
        self._chosen: dict[str, Path] = {}  # insertion-ordered: songs are added in the order they were ticked
        self._on_add = on_add
        self._store = Gio.ListStore(item_type=SongItem)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self._add = Gtk.Button(label="Add", sensitive=False, css_classes=["suggested-action"])
        self._add.connect("clicked", self._on_add_clicked)
        self._title = Adw.WindowTitle(title="Add Songs")
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False, title_widget=self._title)
        header.pack_start(cancel)
        header.pack_end(self._add)
        self._search = Gtk.SearchEntry(placeholder_text="Search songs, artists and albums", hexpand=True,
                                       margin_start=12, margin_end=12, margin_bottom=6)
        self._search.update_property([Gtk.AccessibleProperty.LABEL], ["Search the library"])
        self._search.connect("search-changed", lambda _e: self._refresh())

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, cell: cell.set_child(_PickRow(art, self._is_chosen, self._on_toggle)))
        factory.connect("bind", lambda _f, cell: cell.get_child().bind(cell.get_item()))
        self._list = Gtk.ListView(model=Gtk.NoSelection(model=self._store), factory=factory,
                                  single_click_activate=True)
        self._list.connect("activate", self._on_activate)
        none = Adw.StatusPage(icon_name="edit-find-symbolic", title="No Results", css_classes=["compact"])
        empty = Adw.StatusPage(icon_name="folder-music-symbolic", title="The Library Is Empty",
                               description="Download some music first.", css_classes=["compact"])
        self._stack = Gtk.Stack()
        self._stack.add_named(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self._list), "list")
        self._stack.add_named(none, "none")
        self._stack.add_named(empty, "empty")

        view = Adw.ToolbarView(content=self._stack)
        view.add_top_bar(header)
        view.add_top_bar(self._search)
        self.set_child(view)
        self.set_focus(self._search)
        self._refresh()

    def _refresh(self) -> None:
        text = self._search.get_text().strip()
        shown = self._music.library.search(text, self._music.songs) if text else self._music.songs
        self._store.splice(0, self._store.get_n_items(),
                           [SongItem(song, missing=song.path in self._present) for song in shown])
        self._stack.set_visible_child_name("list" if shown else "none" if self._music.songs else "empty")

    def _is_chosen(self, item: SongItem) -> bool:
        return item.song.key in self._chosen

    def _on_toggle(self, item: SongItem, active: bool) -> None:
        if active:
            self._chosen[item.song.key] = item.song.path
        else:
            self._chosen.pop(item.song.key, None)
        count = len(self._chosen)
        self._title.set_subtitle(f"{songs(count)} selected" if count else "")
        self._add.set_sensitive(bool(count))

    def _on_activate(self, _view: Gtk.ListView, position: int) -> None:
        item = self._store.get_item(position)
        if not item.missing:
            self._on_toggle(item, item.song.key not in self._chosen)
            # A fresh item makes the list rebind the row, which redraws its checkbox.
            self._store.splice(position, 1, [SongItem(item.song)])

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._on_add(list(self._chosen.values()))
        self.close()
