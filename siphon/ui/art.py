"""Cover thumbnails: found and decoded off the main thread, cached, shown by `Cover` widgets.

Scrolling a long list binds and rebinds rows faster than covers can load,
so the newest request is served first, and a row that moves on to another
song withdraws its old request. Besides songs' embedded covers, image files
(a playlist's picture) are shown as they are, keyed on their mtime so a
replaced picture decodes afresh.
"""

import ctypes
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

Key = tuple[Path, int, int | None]  # file, pixels, and an image file's mtime (None: a song, cover_file finds its cover)
Deliver = Callable[[Gdk.Texture | None], None]
Ticket = tuple[Key, Deliver]

_SCALE = 2  # decode at twice the logical size so covers stay sharp on HiDPI screens


class CoverArt:
    def __init__(self, cover_file: Callable[[Path], Path | None], workers: int = 2, capacity: int = 1500) -> None:
        self._cover_file = cover_file
        self._capacity = capacity
        self._cache: OrderedDict[Key, Gdk.Texture | None] = OrderedDict()
        self._waiting: dict[Key, list[Deliver]] = {}
        self._queue: OrderedDict[Key, None] = OrderedDict()  # not picked up yet; newest last
        self._lock = threading.Condition()
        for n in range(workers):
            threading.Thread(target=self._work, name=f"siphon-covers-{n}", daemon=True).start()

    def load(self, path: Path, size: int, deliver: Deliver, mtime: int | None = None) -> Ticket | None:
        """Calls `deliver` with the texture (None when there is none): at once when cached, else later.

        path is a song, or with its mtime an image file to show as it is. A later delivery returns a ticket
        that `cancel` takes back."""
        key = (path, size * _SCALE, mtime)
        if key in self._cache:
            self._cache.move_to_end(key)
            deliver(self._cache[key])
            return None
        with self._lock:
            self._waiting.setdefault(key, []).append(deliver)
            if key in self._queue:
                self._queue.move_to_end(key)
            elif len(self._waiting[key]) == 1:  # else a worker is decoding it already
                self._queue[key] = None
                self._lock.notify()
        return key, deliver

    def rest_with(self, window: Gtk.Window) -> None:
        """Forget the cached covers whenever the window is hidden (closed to the background, say): scrolled through,
        a large library's hold 30 MB (1500 covers, measured 2026-09-26). The covers on screen stay, held by their
        widgets; the rest decode again as they scroll into view."""
        window.connect("unmap", lambda _window: self.forget())

    def forget(self) -> None:
        self._cache.clear()
        give_back_memory()

    def cancel(self, ticket: Ticket) -> None:
        key, deliver = ticket
        with self._lock:
            waiting = self._waiting.get(key, [])
            if deliver in waiting:
                waiting.remove(deliver)
            if not waiting and key in self._queue:
                del self._queue[key]
                del self._waiting[key]

    def _work(self) -> None:  # worker thread
        while True:
            with self._lock:
                while not self._queue:
                    self._lock.wait()
                key, _ = self._queue.popitem()
            GLib.idle_add(self._deliver, key, self._decode(*key))

    def _deliver(self, key: Key, texture: Gdk.Texture | None) -> bool:
        self._cache[key] = texture
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        with self._lock:
            waiting = self._waiting.pop(key, [])
        for deliver in waiting:
            deliver(texture)
        return GLib.SOURCE_REMOVE

    def _decode(self, path: Path, px: int, mtime: int | None) -> Gdk.Texture | None:  # worker thread
        try:
            file = path if mtime is not None else self._cover_file(path)
            if file is None:
                return None
            _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(str(file))
            if not width or not height:
                return None
            # Scale the short side to px, then crop the middle: covers fill their square.
            scale = px / min(width, height)
            w, h = max(px, round(width * scale)), max(px, round(height * scale))
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(file), w, h, False)
            pixbuf = pixbuf.new_subpixbuf((w - px) // 2, (h - px) // 2, px, px).copy()
        except Exception:  # a bad cover must never break a list; the placeholder stays
            return None
        fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        return Gdk.MemoryTexture.new(px, px, fmt, pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


class Cover(Adw.Bin):
    """A square cover with rounded corners, or a music-note placeholder."""

    def __init__(self, art: CoverArt, size: int) -> None:
        super().__init__(overflow=Gtk.Overflow.HIDDEN, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.add_css_class("cover")
        if size >= 96:
            self.add_css_class("large")
        self.set_size_request(size, size)
        self._art = art
        self._size = size
        self._shown: tuple[Path | None, Path | None, int | None] = (None, None, None)  # song, picture, its mtime
        self._ticket: Ticket | None = None
        self._image = Gtk.Image(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.set_child(self._image)
        self._placeholder()

    def show(self, path: Path | None, picture: Path | None = None) -> None:
        """The song's cover; picture, an image file, instead when there is one that decodes."""
        mtime = _mtime(picture) if picture is not None else None
        shown = (path, picture if mtime is not None else None, mtime)
        if shown == self._shown:
            return
        if self._ticket is not None:
            self._art.cancel(self._ticket)
            self._ticket = None
        self._shown = shown
        self._placeholder()
        self._request(shown[1] is not None)

    def _request(self, picture: bool) -> None:
        shown = self._shown
        path, file, mtime = shown
        if not picture and path is None:
            return
        ticket = self._art.load(file if picture else path, self._size,
                                lambda texture: self._loaded(shown, picture, texture), mtime if picture else None)
        if ticket is not None:  # else it was delivered already, and may have asked for the song's cover since
            self._ticket = ticket

    def _loaded(self, shown: tuple, picture: bool, texture: Gdk.Texture | None) -> None:
        self._ticket = None
        if shown != self._shown:
            return
        if texture is None:
            if picture:  # a damaged picture: the song's cover after all
                self._request(False)
            return
        self.remove_css_class("placeholder")
        self._image.set_from_paintable(texture)
        self._image.set_pixel_size(self._size)

    def _placeholder(self) -> None:
        self.add_css_class("placeholder")
        self._image.set_from_icon_name("audio-x-generic-symbolic")
        self._image.set_pixel_size(max(16, self._size * 3 // 8))


def give_back_memory() -> None:
    """glibc keeps memory it got back for later instead of returning it to the system: the 30 MB of a full cache
    stayed until malloc_trim (measured); other C libraries follow their own rules."""
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL(None).malloc_trim(0)
        except (OSError, AttributeError):  # a C library without malloc_trim (musl)
            pass


def _mtime(file: Path) -> int | None:
    try:
        return os.stat(file).st_mtime_ns
    except OSError:
        return None
