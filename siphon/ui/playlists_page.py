"""The Playlists page: the list of playlists, and one playlist's songs to play, reorder and edit."""

import random
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .. import picture
from ..covers import image_ext
from .addsongs import AddSongsDialog
from .art import Cover, CoverArt, give_back_memory
from .dialogs import ask_name, confirm
from .download_page import is_dismissal
from .fmt import pretty_path, summary
from .music import PLAYLISTS_DIR, Music
from .songmenu import quoted, song_menu
from .songrow import SongItem, SongRow, song_list

Toast = Callable[..., Adw.Toast]
_NARROW = "max-width: 520sp"
# What the Choose a Picture dialog lists: what GdkPixbuf or ffmpeg reads on either system (siphon/picture.py).
_PICTURE_SUFFIXES = ("jpg", "jpeg", "jpe", "jfif", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif", "avif",
                     "jxl", "ico", "tga", "pnm", "ppm", "pgm", "pbm", "qoi")


def _first_cover(entries: list[tuple[Any, bool]]) -> Path | None:
    """The song whose cover stands for the playlist when it has no picture of its own."""
    return next((song.path for song, missing in entries if not missing), None)


def _picture_bytes(playlist: Any) -> bytes | None:
    """The playlist's picture as it is now, to put back with Undo: None without one Siphon could keep."""
    try:
        data = playlist.cover.read_bytes() if playlist.cover is not None else None
    except OSError:
        return None
    return data if data and image_ext(data) else None


def _is_picture(path: str) -> bool:
    """By its name: what a paste acts on (a copied song is not taken for a picture that won't load)."""
    return Path(path).suffix.lower().lstrip(".") in _PICTURE_SUFFIXES


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
    """One playlist. Its songs can be dragged, or moved with Move Up/Down in their menus. Its picture is chosen with
    the pencil on the cover or Change Picture… in the menu, dropped on the cover, or pasted (Ctrl+V)."""

    def __init__(self, music: Music, art: CoverArt, playlist: Any, toast: Toast) -> None:
        super().__init__(title=playlist.name)
        self._music = music
        self._art = art
        self._playlist = playlist
        self._toast = toast
        self._entries: list[tuple[Any, bool]] = []
        self._store = Gio.ListStore(item_type=SongItem)
        self._working = False  # a chosen picture is being decoded
        self._install_actions()

        self._cover = Cover(art, 144)
        self._busy = Adw.Spinner(visible=False, width_request=64, height_request=64, halign=Gtk.Align.CENTER,
                                 valign=Gtk.Align.CENTER, tooltip_text="Preparing the Picture…",
                                 css_classes=["picture-busy"])
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Change Picture",
                          action_name="playlist.change-picture", halign=Gtk.Align.END, valign=Gtk.Align.END,
                          margin_end=6, margin_bottom=6, css_classes=["circular", "osd"])
        edit.update_property([Gtk.AccessibleProperty.LABEL], ["Change Picture"])
        cover = Gtk.Overlay(child=self._cover, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                            css_classes=["playlist-picture"])
        cover.add_overlay(self._busy)
        cover.add_overlay(edit)
        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, Gdk.Texture])
        drop.connect("drop", self._on_picture_drop)
        cover.add_controller(drop)
        self._name = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                               css_classes=["title-1"])
        self._summary = Gtk.Label(xalign=0, css_classes=["dim-label", "numeric"])
        self._play = _button("media-playback-start-symbolic", "Play", "suggested-action")
        self._play.set_action_name("playlist.play")
        self._shuffle = _button("media-playlist-shuffle-symbolic", "Shuffle")
        self._shuffle.set_action_name("playlist.shuffle")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Songs", action_name="playlist.add-songs",
                         css_classes=["circular"])
        edits = Gio.Menu()
        edits.append("Rename…", "playlist.rename")
        edits.append("Change Picture…", "playlist.change-picture")
        remove = Gio.MenuItem.new("Remove Picture", "playlist.remove-picture")
        remove.set_attribute_value("hidden-when", GLib.Variant("s", "action-disabled"))  # shown with a picture only
        edits.append_item(remove)
        delete = Gio.Menu()
        delete.append("Delete Playlist…", "playlist.delete")
        more = Gio.Menu()
        more.append_section(None, edits)
        more.append_section(None, delete)
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
        header.append(cover)
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
        # Global: a page just opened has nothing focused inside it yet. _on_paste lets the key go when it isn't ours.
        paste = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        paste.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>v"),
                                        action=Gtk.CallbackAction.new(self._on_paste)))
        self.add_controller(paste)
        self.refresh()

    def _stack_header(self, stacked: bool) -> None:
        self._header.set_orientation(Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
        align = Gtk.Align.CENTER if stacked else Gtk.Align.FILL
        for widget in (self._text, self._buttons):
            widget.set_halign(align)
        self._buttons.set_align(0.5 if stacked else 0)  # a button wrapped onto a line of its own (under 400 px) too
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
        self._actions.lookup_action("remove-picture").set_enabled(playlist.cover is not None
                                                                  and playlist.cover.is_file())
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
                              ("change-picture", self._on_change_picture),
                              ("remove-picture", self._on_remove_picture), ("delete", self._on_delete)):
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

    # -- the picture

    def _on_change_picture(self, *_args) -> None:
        pictures = Gtk.FileFilter(name="Pictures")
        for suffix in _PICTURE_SUFFIXES:
            pictures.add_suffix(suffix)
        everything = Gtk.FileFilter(name="All Files")
        everything.add_pattern("*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.splice(0, 0, [pictures, everything])
        dialog = Gtk.FileDialog(title="Choose a Picture", modal=True, filters=filters, default_filter=pictures)
        folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if folder and Path(folder).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(folder))
        dialog.open(self.get_root(), None, self._on_picture_chosen)

    def _on_picture_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error as error:
            if not is_dismissal(error):
                self._toast("Could not open that file.")
            return
        if file.get_path() is None:
            self._toast("Choose a picture on this computer.")
            return
        self._set_picture(Path(file.get_path()))

    def _on_picture_drop(self, _target: Gtk.DropTarget, value: Any, _x: float, _y: float) -> bool:
        if self._working:
            return False
        if isinstance(value, Gdk.Texture):
            self._set_picture(value)
            return True
        local = [file.get_path() for file in value.get_files() if file.get_path() is not None]
        if not local:
            self._toast("Drop a picture from this computer.")
            return False
        self._set_picture(Path(next((path for path in local if _is_picture(path)), local[0])))
        return True

    def _on_paste(self, _widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
        """Ctrl+V with a picture, or a picture's file, on the clipboard; any other paste is left alone."""
        window = self.get_root()
        focus = window.get_focus() if isinstance(window, Gtk.Window) else None
        if (not self.get_mapped() or self._working or isinstance(focus, Gtk.Editable)
                or (focus is not None and focus.get_ancestor(Gtk.Popover) is not None)
                or (isinstance(window, (Adw.Window, Adw.ApplicationWindow))
                    and window.get_visible_dialog() is not None)):
            return False
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        if formats.contain_gtype(Gdk.FileList):
            clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, self._on_pasted_files)
        elif formats.contain_gtype(Gdk.Texture):
            clipboard.read_texture_async(None, self._on_pasted_texture)
        else:
            return False
        return True

    def _on_pasted_files(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            files = clipboard.read_value_finish(result).get_files()
        except GLib.Error:
            return
        paths = [file.get_path() for file in files if file.get_path() is not None and _is_picture(file.get_path())]
        if paths:
            self._set_picture(Path(paths[0]))

    def _on_pasted_texture(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error:
            return
        if texture is not None:
            self._set_picture(texture)

    def _set_picture(self, source: Path | Gdk.Texture) -> None:
        """Make source the playlist's picture. Decoded off the main thread: 0.25 s for a 24 MP JPEG, 0.7 s for a
        12 MP HEIC photo (measured 2026-09-28)."""
        if self._working:
            return
        self._show_working(True)

        def work() -> None:  # worker thread
            data, problem = None, ""
            try:
                data = picture.square(source if isinstance(source, Path) else source.save_to_png_bytes().get_data())
            except picture.PictureError as error:
                problem = str(error)
            except Exception as exc:  # a page left busy would take no picture again
                traceback.print_exception(exc)
                problem = "Could not use that picture."
            GLib.idle_add(self._on_picture_ready, data, problem)

        threading.Thread(target=work, name="siphon-picture", daemon=True).start()

    def _on_picture_ready(self, data: bytes | None, problem: str) -> bool:
        self._show_working(False)
        give_back_memory()  # a 24 MP photo leaves 26 MB behind otherwise (measured 2026-09-28)
        playlist = self._music.find_playlist(self._playlist.file)
        if problem:
            self._toast(problem)
        elif playlist is not None:  # else it was deleted meanwhile
            old = _picture_bytes(playlist)
            saved = self._music.change_playlists(self._music.playlists.set_cover, playlist, data)
            if saved is False:  # None: change_playlists told the user already
                self._toast("Could not save the picture.")
            elif saved and old is not None:
                self._toast(f"Changed the picture of {quoted(playlist.name)}", "Undo",
                            lambda: self._put_back(playlist.file, old))
        return GLib.SOURCE_REMOVE

    def _show_working(self, working: bool) -> None:
        """The spinner only for a slow picture: most take 0.15-0.3 s, a 12 MP HEIC photo 0.7 (measured 2026-09-28)."""
        self._working = working
        self._actions.lookup_action("change-picture").set_enabled(not working)
        if not working:
            self._busy.set_visible(False)
            return

        def still_working() -> bool:
            self._busy.set_visible(self._working)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(250, still_working)

    def _on_remove_picture(self, *_args) -> None:
        playlist, old = self._playlist, _picture_bytes(self._playlist)
        self._music.change_playlists(self._music.playlists.remove_cover, playlist)
        undo = ("Undo", lambda: self._put_back(playlist.file, old)) if old is not None else ()
        self._toast(f"Removed the picture of {quoted(playlist.name)}", *undo)

    def _put_back(self, file: Path, data: bytes) -> None:
        playlist = self._music.find_playlist(file)
        if playlist is not None:
            self._music.change_playlists(self._music.playlists.set_cover, playlist, data)
