"""The Library page: every song in the music folder, searchable and sortable; activating one plays the list."""

import unicodedata
from collections.abc import Callable
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import settings
from .art import CoverArt
from .fmt import pretty_path, summary
from .music import Music
from .songmenu import song_menu
from .songrow import SongItem, SongRow, song_list

SORT_LABELS = {"added": "Recently Added", "title": "Title", "artist": "Artist"}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def sort_songs(songs: list, mode: str) -> list:
    """The library hands songs over newest first, which is the "added" order already."""
    if mode == "title":
        return sorted(songs, key=lambda s: (_fold(s.title), _fold(s.artist)))
    if mode == "artist":
        # Songs without an artist go last rather than first.
        return sorted(songs, key=lambda s: (not s.artist, _fold(s.artist), _fold(s.album), s.track_no or 0,
                                            _fold(s.title)))
    return list(songs)


class LibraryPage(Gtk.Box):
    def __init__(self, music: Music, art: CoverArt, prefs: settings.Settings, save_prefs: Callable[[], None],
                 go_download: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._music = music
        self._prefs = prefs
        self._save_prefs = save_prefs
        self._sorted: list | None = None  # the library in the chosen order; rebuilt when either changes
        self._shown: list = []
        # One item per song, kept across refreshes: handing the list the same items again is ~50x cheaper
        # than new ones, which would rebind every row.
        self._items: dict[str, SongItem] = {}
        self._store = Gio.ListStore(item_type=SongItem)

        self._search = Gtk.SearchEntry(placeholder_text="Search songs, artists and albums", hexpand=True)
        self._search.update_property([Gtk.AccessibleProperty.LABEL], ["Search the library"])
        self._search.connect("search-changed", lambda _e: self._refresh(to_top=True))
        self._search.connect("stop-search", lambda entry: entry.set_text(""))
        self._summary = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["dim-label", "numeric"])
        self._busy = Adw.Spinner(visible=False, tooltip_text="Looking for new music…")
        self._sort_button = Gtk.MenuButton(menu_model=self._sort_menu(), tooltip_text="Sort", always_show_arrow=True)
        self._sort_button.add_css_class("flat")
        info = Gtk.Box(spacing=6)
        info.append(self._summary)
        info.append(self._busy)
        info.append(self._sort_button)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_start=12, margin_end=12)
        top.append(self._search)
        top.append(info)
        self._top = Adw.Clamp(maximum_size=900, child=top)
        self.append(self._top)

        self._list = song_list(self._store, lambda: SongRow(music.player, art, self._menu_for))
        self._list.connect("activate", self._on_activate)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                      child=Adw.ClampScrollable(maximum_size=900, child=self._list))
        download = Gtk.Button(label="Download Music", halign=Gtk.Align.CENTER,
                              css_classes=["pill", "suggested-action"])
        download.connect("clicked", lambda _b: go_download())
        self._empty = Adw.StatusPage(icon_name="folder-music-symbolic", title="No Music Yet", child=download)
        loading = Adw.StatusPage(title="Looking for Music…")
        loading.set_paintable(Adw.SpinnerPaintable(widget=loading))
        none = Adw.StatusPage(icon_name="edit-find-symbolic", title="No Results",
                              description="Try other words, or fewer of them.")
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        for name, child in (("list", scroller), ("empty", self._empty), ("loading", loading), ("none", none)):
            self._stack.add_named(child, name)
        self.append(self._stack)

        music.connect("library-changed", self._on_library_changed)
        self._on_library_changed(music)

    def focus_search(self) -> None:
        self._search.grab_focus()

    # -- sorting

    def _sort_menu(self) -> Gio.Menu:
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new_stateful("sort", GLib.VariantType.new("s"), GLib.Variant("s", self._prefs.sort))
        action.connect("change-state", self._on_sort)
        group.add_action(action)
        self.insert_action_group("library", group)
        menu = Gio.Menu()
        for mode in settings.SORTS:
            menu.append(SORT_LABELS[mode], f"library.sort::{mode}")
        return menu

    def _on_sort(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        action.set_state(value)
        self._prefs.sort = value.get_string()
        self._save_prefs()
        self._sorted = None
        self._refresh(to_top=True)

    # -- contents

    def _on_library_changed(self, _music: Music) -> None:
        old = self._items
        self._items = {song.key: old[song.key] if song.key in old and old[song.key].song == song else SongItem(song)
                       for song in self._music.songs}
        self._sorted = None
        self._refresh()

    def _refresh(self, to_top: bool = False) -> None:
        songs = self._music.songs
        if self._sorted is None:
            self._sorted = sort_songs(songs, self._prefs.sort)
        text = self._search.get_text().strip()
        self._shown = self._music.library.search(text, self._sorted) if text else self._sorted
        self._store.splice(0, self._store.get_n_items(), [self._items[song.key] for song in self._shown])
        self._summary.set_label(summary(self._shown, len(songs) if text else None) if songs else "")
        self._sort_button.set_label(SORT_LABELS[self._prefs.sort])
        self._busy.set_visible(self._music.scanning and bool(songs))
        self._top.set_visible(bool(songs))
        self._empty.set_description(f"Songs you download show up here, along with the music already in "
                                    f"{pretty_path(self._music.root)}.")
        if not songs:
            self._stack.set_visible_child_name("loading" if self._music.scanning else "empty")
        else:
            self._stack.set_visible_child_name("list" if self._shown else "none")
        if to_top and self._shown:
            self._list.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    def _menu_for(self, item: SongItem) -> Gio.MenuModel:
        return song_menu(self._music, item.song)

    def _on_activate(self, _view: Gtk.ListView, position: int) -> None:
        self._music.player.play_songs(self._shown, position)
