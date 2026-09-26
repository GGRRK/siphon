"""The Playlists page: the list of playlists, and one playlist's songs to play, reorder and edit."""

import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .addsongs import AddSongsDialog
from .art import Cover, CoverArt
from .dialogs import ask_name, confirm
from .fmt import pretty_path, summary
from .music import PLAYLISTS_DIR, Music
from .songmenu import quoted, song_menu
from .songrow import SongItem, SongRow, song_list

Toast = Callable[..., Adw.Toast]
_NARROW = "max-width: 520sp"


def _first_cover(entries: list[tuple[Any, bool]]) -> Path | None:
    """The song whose cover stands for the playlist when it has no picture of its own."""
    return next((song.path for song, missing in entries if not missing), None)


def _button(icon: str, label: str, *classes: str) -> Gtk.Button:
    return Gtk.Button(child=Adw.ButtonContent(icon_name=icon, label=label), css_classes=["pill", *classes])


class PlaylistsPage(Adw.Bin):
    def __init__(self, music: Music, art: CoverArt, toast: Toast) -> None:
        super().__init__()
        self._music = music
        self._art = art
        self._toast = toast
        self._detail: PlaylistView | None = None

        self._count = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"])
        new = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="New Playlist"))
        new.add_css_class("flat")
        new.connect("clicked", lambda _b: self.new_playlist())
        heading = Gtk.Box(spacing=6)
        heading.append(self._count)
        heading.append(new)
        self._rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._rows.connect("row-activated", lambda _l, row: self.open(row.playlist))
        self._where = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=6, margin_bottom=18, margin_start=12, margin_end=12)
        for child in (heading, self._rows, self._where):
            box.append(child)
        listing = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     child=Adw.Clamp(maximum_size=900, child=box))

        create = _button("list-add-symbolic", "New Playlist", "suggested-action")
        create.set_halign(Gtk.Align.CENTER)
        create.connect("clicked", lambda _b: self.new_playlist())
        empty = Adw.StatusPage(icon_name="view-list-bullet-symbolic", title="No Playlists Yet", child=create,
                               description="Gather songs from your library into playlists: "
                                           "create one here, or use a song’s menu.")
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(listing, "list")
        self._stack.add_named(empty, "empty")

        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(title="Playlists", tag="playlists", child=self._stack))
        self.nav.connect("popped", self._on_popped)
        self.set_child(self.nav)
        music.connect("playlists-changed", self._refresh)
        music.connect("library-changed", self._refresh)  # counts, lengths and covers come from the library
        self._refresh()

    def new_playlist(self) -> None:
        def create(name: str) -> None:
            playlist = self._music.change_playlists(self._music.playlists.create, name)
            if playlist is not None:
                self.open(playlist)

        ask_name(self, "New Playlist", "Create", create)

    def open(self, playlist: Any) -> None:
        if self._detail is not None:
            self.nav.pop_to_tag("playlists")
        self._detail = PlaylistView(self._music, self._art, playlist, self._toast)
        self.nav.push(self._detail)

    def _on_popped(self, _nav: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self._detail:
            self._detail = None

    def _refresh(self, *_args) -> None:
        playlists = self._music.playlists.all()
        self._rows.remove_all()
        for playlist in playlists:
            self._rows.append(self._row(playlist))
        self._count.set_label(f"{len(playlists)} playlist{'s' if len(playlists) != 1 else ''}")
        self._where.set_label(f"Saved as M3U files in {pretty_path(self._music.root / PLAYLISTS_DIR)}, "
                              "so other music players can open them too.")
        self._stack.set_visible_child_name("list" if playlists else "empty")
        if self._detail is not None and not self._detail.refresh():
            self.nav.pop_to_tag("playlists")

    def _row(self, playlist: Any) -> Adw.ActionRow:
        entries = self._music.entries(playlist)
        row = Adw.ActionRow(title=GLib.markup_escape_text(playlist.name), activatable=True,
                            subtitle=summary([song for song, _missing in entries]))
        row.playlist = playlist
        cover = Cover(self._art, 48)
        cover.show(_first_cover(entries), playlist.cover)
        row.add_prefix(cover)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        return row


class PlaylistView(Adw.NavigationPage):
    """One playlist. Its songs can be dragged, or moved with Move Up/Down in their menus."""

    def __init__(self, music: Music, art: CoverArt, playlist: Any, toast: Toast) -> None:
        super().__init__(title=playlist.name)
        self._music = music
        self._art = art
        self._playlist = playlist
        self._toast = toast
        self._entries: list[tuple[Any, bool]] = []
        self._store = Gio.ListStore(item_type=SongItem)
        self._install_actions()

        self._cover = Cover(art, 144)
        self._name = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                               css_classes=["title-1"])
        self._summary = Gtk.Label(xalign=0, css_classes=["dim-label", "numeric"])
        self._play = _button("media-playback-start-symbolic", "Play", "suggested-action")
        self._play.set_action_name("playlist.play")
        self._shuffle = _button("media-playlist-shuffle-symbolic", "Shuffle")
        self._shuffle.set_action_name("playlist.shuffle")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Songs", action_name="playlist.add-songs",
                         css_classes=["circular"])
        more = Gio.Menu()
        more.append("Rename…", "playlist.rename")
        more.append("Delete Playlist…", "playlist.delete")
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more, tooltip_text="More",
                              css_classes=["circular"])
        self._buttons = buttons = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_top=6)
        for child in (self._play, self._shuffle, add, menu):
            buttons.append(child)
        self._text = text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                                     hexpand=True)
        for child in (self._name, self._summary, buttons):
            text.append(child)
        self._header = header = Gtk.Box(spacing=18, margin_top=12, margin_bottom=12, margin_start=18, margin_end=18)
        header.append(self._cover)
        header.append(text)

        self._list = song_list(self._store, lambda: SongRow(music.player, art, self._menu_for, self._move))
        self._list.connect("activate", self._on_activate)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                      child=Adw.ClampScrollable(maximum_size=900, child=self._list))
        fill = _button("list-add-symbolic", "Add Songs", "suggested-action")
        fill.set_halign(Gtk.Align.CENTER)
        fill.set_action_name("playlist.add-songs")
        empty = Adw.StatusPage(icon_name="view-list-bullet-symbolic", title="No Songs Yet", child=fill,
                               description="Pick songs from your library to fill this playlist.",
                               css_classes=["compact"], vexpand=True)
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(scroller, "list")
        self._stack.add_named(empty, "empty")

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(Adw.Clamp(maximum_size=900, child=header))
        page.append(self._stack)
        # Narrow windows stack the header: cover above a centred title and one row of buttons.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(_NARROW))
        narrow.connect("apply", lambda _b: self._stack_header(True))
        narrow.connect("unapply", lambda _b: self._stack_header(False))
        fit = Adw.BreakpointBin(child=page, width_request=300, height_request=300)
        fit.add_breakpoint(narrow)
        self.set_child(fit)
        self.refresh()

    def _stack_header(self, stacked: bool) -> None:
        self._header.set_orientation(Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
        align = Gtk.Align.CENTER if stacked else Gtk.Align.FILL
        for widget in (self._text, self._buttons):
            widget.set_halign(align)
        for label in (self._name, self._summary):
            label.set_xalign(0.5 if stacked else 0)
            label.set_justify(Gtk.Justification.CENTER if stacked else Gtk.Justification.LEFT)

    def refresh(self) -> bool:
        """Re-reads the playlist; False when it no longer exists."""
        playlist = self._music.find_playlist(self._playlist.file)
        if playlist is None:
            return False
        self._playlist = playlist
        self._entries = self._music.entries(playlist)
        self.set_title(playlist.name)
        self._name.set_label(playlist.name)
        self._summary.set_label(summary([song for song, _missing in self._entries]))
        self._cover.show(_first_cover(self._entries), playlist.cover)
        playable = bool(self._playable())
        for name in ("play", "shuffle"):
            self._actions.lookup_action(name).set_enabled(playable)
        self._store.splice(0, self._store.get_n_items(),
                           [SongItem(song, i, missing) for i, (song, missing) in enumerate(self._entries)])
        self._stack.set_visible_child_name("list" if self._entries else "empty")
        return True

    def _playable(self) -> list:
        return [song for song, missing in self._entries if not missing]

    # -- actions

    def _install_actions(self) -> None:
        self._actions = Gio.SimpleActionGroup()
        for name, handler in (("play", lambda *_: self._play_from(0)), ("shuffle", self._on_shuffle),
                              ("add-songs", self._on_add_songs), ("rename", self._on_rename),
                              ("delete", self._on_delete)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self._actions.add_action(action)
        for name, handler in (("move-up", lambda i: self._move(i, i - 1)),
                              ("move-down", lambda i: self._move(i, i + 1)), ("remove", self._remove)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("i"))
            action.connect("activate", lambda _a, param, handler=handler: handler(param.get_int32()))
            self._actions.add_action(action)
        self.insert_action_group("playlist", self._actions)

    def _menu_for(self, item: SongItem) -> Gio.MenuModel:
        edit = Gio.Menu()
        index = GLib.Variant("i", item.index)
        moves = (("Move Up", "playlist.move-up", item.index > 0),
                 ("Move Down", "playlist.move-down", item.index < len(self._entries) - 1),
                 ("Remove from Playlist", "playlist.remove", True))
        for label, action, shown in moves:
            if shown:
                entry = Gio.MenuItem.new(label, None)
                entry.set_action_and_target_value(action, index)
                edit.append_item(entry)
        return edit if item.missing else song_menu(self._music, item.song, extra=edit)

    def _play_from(self, position: int) -> None:
        if self._entries[position][1]:
            self._toast("That song’s file is missing.")
            return
        start = sum(not missing for _song, missing in self._entries[:position])
        self._music.player.play_songs(self._playable(), start)

    def _on_activate(self, _view: Gtk.ListView, position: int) -> None:
        self._play_from(position)

    def _on_shuffle(self, *_args) -> None:
        songs = self._playable()
        self._music.player.set_shuffle(True)
        self._music.player.play_songs(songs, random.randrange(len(songs)))

    def _move(self, src: int, dst: int) -> None:
        if 0 <= dst < len(self._entries):
            self._music.change_playlists(self._music.playlists.move, self._playlist, src, dst)

    def _remove(self, index: int) -> None:
        song, _missing = self._entries[index]
        file = self._playlist.file
        self._music.change_playlists(self._music.playlists.remove, self._playlist, index)

        def undo() -> None:
            playlist = self._music.find_playlist(file)
            if playlist is not None:
                self._music.change_playlists(self._music.playlists.add, playlist, [song.path])
                self._music.change_playlists(self._music.playlists.move, playlist, len(playlist.paths) - 1, index)

        self._toast(f"Removed {quoted(song.title)}", "Undo", undo)

    def _on_add_songs(self, *_args) -> None:
        def add(paths: list[Path]) -> None:
            self._music.change_playlists(self._music.playlists.add, self._playlist, paths)
            self._toast(f"Added {len(paths)} song{'s' if len(paths) != 1 else ''} to {quoted(self._playlist.name)}")

        AddSongsDialog(self._music, self._art, self._playlist, add).present(self)

    def _on_rename(self, *_args) -> None:
        ask_name(self, "Rename Playlist", "Rename",
                 lambda name: self._music.change_playlists(self._music.playlists.rename, self._playlist, name),
                 initial=self._playlist.name)

    def _on_delete(self, *_args) -> None:
        name = self._playlist.name

        def delete() -> None:
            self._music.change_playlists(self._music.playlists.delete, self._playlist)
            self._toast(f"Deleted {quoted(name)}")

        confirm(self, f"Delete {quoted(name)}?",
                "The playlist file is moved to the Trash. Its songs stay in your library.", "Delete", delete)
