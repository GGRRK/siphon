"""The song menu (Play Next, Add to Queue, Add to Playlist ▸, Show in Folder, Move to Trash) and its actions.

The actions live on the window and take the song's path as their target,
so every list builds its menus from plain Gio.Menu models.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from .dialogs import ask_name, confirm
from .music import Music


def quoted(text: str, limit: int = 40) -> str:
    return f"“{text if len(text) <= limit else text[:limit - 1].rstrip() + '…'}”"


def song_menu(music: Music, song: Any, extra: Gio.MenuModel | None = None) -> Gio.Menu:
    key = GLib.Variant("s", song.key)
    menu = Gio.Menu()
    play = Gio.Menu()
    for label, action in (("Play Next", "win.play-next"), ("Add to Queue", "win.enqueue")):
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, key)
        play.append_item(item)
    menu.append_section(None, play)

    lists = Gio.Menu()
    for playlist in music.playlists.all():
        item = Gio.MenuItem.new(playlist.name, None)
        item.set_action_and_target_value("win.add-to-playlist", GLib.Variant("(ss)", (song.key, str(playlist.file))))
        lists.append_item(item)
    create = Gio.MenuItem.new("New Playlist…", None)
    create.set_action_and_target_value("win.new-playlist-with", key)
    new = Gio.Menu()
    new.append_item(create)
    submenu = Gio.Menu()
    submenu.append_section(None, lists)
    submenu.append_section(None, new)
    menu.append_submenu("Add to Playlist", submenu)

    if extra is not None:
        menu.append_section(None, extra)
    files = Gio.Menu()
    for label, action in (("Show in Folder", "win.show-song"), ("Move to Trash…", "win.trash-song")):
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, key)
        files.append_item(item)
    menu.append_section(None, files)
    return menu


class SongActions:
    def __init__(self, window: Gtk.ApplicationWindow, music: Music, toast: Callable[[str], Adw.Toast],
                 show_file: Callable[[Path], None]) -> None:
        self._window = window
        self._music = music
        self._toast = toast
        self._show_file = show_file
        for name, kind, handler in (("play-next", "s", self._play_next), ("enqueue", "s", self._enqueue),
                                    ("add-to-playlist", "(ss)", self._add_to_playlist),
                                    ("new-playlist-with", "s", self._new_playlist_with),
                                    ("show-song", "s", self._show), ("trash-song", "s", self._trash)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(kind))
            action.connect("activate", handler)
            window.add_action(action)

    def _song(self, param: GLib.Variant) -> Any:
        return self._music.song_at(Path(param.get_string()))

    def _play_next(self, _action, param: GLib.Variant) -> None:
        song = self._song(param)
        self._music.player.play_next([song])
        self._toast(f"{quoted(song.title)} plays next")

    def _enqueue(self, _action, param: GLib.Variant) -> None:
        song = self._song(param)
        self._music.player.enqueue([song])
        self._toast(f"Added {quoted(song.title)} to the queue")

    def _add_to_playlist(self, _action, param: GLib.Variant) -> None:
        key, file = param.unpack()
        playlist = self._music.find_playlist(Path(file))
        if playlist is None:
            return
        self._music.change_playlists(self._music.playlists.add, playlist, [Path(key)])
        self._toast(f"Added to {quoted(playlist.name)}")

    def _new_playlist_with(self, _action, param: GLib.Variant) -> None:
        path = Path(param.get_string())

        def create(name: str) -> None:
            playlist = self._music.change_playlists(self._music.playlists.create, name)
            if playlist is not None:
                self._music.change_playlists(self._music.playlists.add, playlist, [path])
                self._toast(f"Added to {quoted(playlist.name)}")

        ask_name(self._window, "New Playlist", "Create", create)

    def _show(self, _action, param: GLib.Variant) -> None:
        self._show_file(Path(param.get_string()))

    def _trash(self, _action, param: GLib.Variant) -> None:
        song = self._song(param)

        def trash() -> None:
            if self._music.trash(song):
                self._toast(f"Moved {quoted(song.title)} to the Trash")

        confirm(self._window, "Move to Trash?",
                f"{quoted(song.title, 80)} is removed from your library and from every playlist. "
                "You can restore the file from the Trash.", "Move to Trash", trash)
