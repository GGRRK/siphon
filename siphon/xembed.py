"""Linux on X11: Siphon's icon in a legacy system tray, through the freedesktop System Tray Protocol and XEmbed.

Bars without StatusNotifierItems (i3bar, polybar, tint2, trayer, stalonetray, older XFCE, MATE and LXDE panels) keep
their tray the old way: a manager owns the selection _NET_SYSTEM_TRAY_S<screen> and docks an application's own X
window into the bar. The icon draws itself, over the bar or in the ARGB visual the manager offers, and takes its own
clicks: left shows or hides the window, middle plays or pauses, the wheel sets the volume, right opens the menu.
GTK 4 cannot embed a window, so that menu and the tooltip are small override-redirect windows drawn here, with the
same items as the dbusmenu; the menu grabs the pointer so that a click anywhere else closes it. A manager that
quits takes the icon away; a new one (a bar restarting) announces itself and gets it back.

libX11, cairo and Pango through ctypes, on an X connection of Siphon's own read from the GLib main loop: nothing
beyond what GTK on X11 already loads. It runs when GTK draws on X11. On a Wayland desktop SIPHON_XEMBED=1 lets it
look for a tray in Xwayland too (DISPLAY): opt-in, because connecting starts Xwayland on desktops that start it on
demand, and such trays are rare there. SIPHON_XEMBED=0 leaves it out.
"""

import ctypes
import logging
import os
import sys
from collections.abc import Callable
from ctypes import (POINTER, Structure, Union, byref, c_char, c_char_p, c_double, c_int, c_long, c_ubyte, c_uint,
                    c_ulong, c_void_p)
from pathlib import Path
from typing import Any

from gi.repository import GLib

from . import tray as menus

log = logging.getLogger(__name__)

APP_ID = "io.github.ggrrk.Siphon"
_SVG = Path(__file__).resolve().parent.parent / "data" / f"{APP_ID}.svg"
_ICON_SIZE = 22  # asked for until the manager sizes the icon
_TOOLTIP_DELAY_MS = 600
_GRAB_TRIES, _GRAB_RETRY_MS = 25, 20  # another client may hold the pointer a moment when the menu opens

# -- X11's numbers
_KEY_PRESS, _BUTTON_PRESS, _BUTTON_RELEASE, _MOTION, _ENTER, _LEAVE = 2, 4, 5, 6, 7, 8
_EXPOSE, _DESTROY, _REPARENT, _CONFIGURE, _CLIENT_MESSAGE = 12, 17, 21, 22, 33
_KEY_MASK, _PRESS_MASK, _RELEASE_MASK, _ENTER_MASK, _LEAVE_MASK, _MOTION_MASK = 1, 4, 8, 16, 32, 64
_EXPOSURE_MASK, _STRUCTURE_MASK = 1 << 15, 1 << 17
_CW_BACK_PIXMAP, _CW_BACK_PIXEL, _CW_BORDER_PIXEL = 1, 2, 8
_CW_OVERRIDE_REDIRECT, _CW_SAVE_UNDER, _CW_EVENT_MASK, _CW_COLORMAP = 1 << 9, 1 << 10, 1 << 11, 1 << 13
_PARENT_RELATIVE, _INPUT_OUTPUT, _REPLACE, _GRAB_ASYNC, _GRAB_SUCCESS, _TRUE_COLOR = 1, 1, 0, 1, 0, 4
_XA_ATOM, _XA_CARDINAL, _XA_STRING = 4, 6, 31
_VISUAL_ID, _VISUAL_SCREEN, _VISUAL_DEPTH, _VISUAL_CLASS = 1, 2, 4, 8
_REQUEST_DOCK, _EMBEDDED_NOTIFY, _XEMBED_MAPPED = 0, 0, 1
_KEYS = {0xFF52: "up", 0xFF54: "down", 0xFF50: "home", 0xFF57: "end", 0xFF0D: "enter", 0xFF8D: "enter",
         0x20: "enter", 0xFF1B: "escape"}
# -- cairo's
_ARGB32, _SOURCE, _OVER, _COLOR_ALPHA = 0, 1, 2, 0x3000


class _Any(Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("window", c_ulong)]


_POINTER_FIELDS = [("root", c_ulong), ("subwindow", c_ulong), ("time", c_ulong), ("x", c_int), ("y", c_int),
                   ("x_root", c_int), ("y_root", c_int)]


class _Button(Structure):
    _fields_ = _Any._fields_ + _POINTER_FIELDS + [("state", c_uint), ("button", c_uint), ("same_screen", c_int)]


class _Key(Structure):
    _fields_ = _Any._fields_ + _POINTER_FIELDS + [("state", c_uint), ("keycode", c_uint), ("same_screen", c_int)]


class _Motion(Structure):
    _fields_ = _Any._fields_ + _POINTER_FIELDS + [("state", c_uint), ("is_hint", c_char), ("same_screen", c_int)]


class _Expose(Structure):
    _fields_ = _Any._fields_ + [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int), ("count", c_int)]


class _Configure(Structure):
    _fields_ = _Any._fields_ + [("child", c_ulong), ("x", c_int), ("y", c_int), ("width", c_int),
                                ("height", c_int), ("border_width", c_int), ("above", c_ulong),
                                ("override_redirect", c_int)]


class _Reparent(Structure):
    _fields_ = _Any._fields_ + [("child", c_ulong), ("parent", c_ulong), ("x", c_int), ("y", c_int),
                                ("override_redirect", c_int)]


class _ClientMessage(Structure):
    _fields_ = _Any._fields_ + [("message_type", c_ulong), ("format", c_int), ("l", c_long * 5)]


class XEvent(Union):
    _fields_ = [("type", c_int), ("xany", _Any), ("xbutton", _Button), ("xkey", _Key), ("xmotion", _Motion),
                ("xexpose", _Expose), ("xconfigure", _Configure), ("xreparent", _Reparent),
                ("xclient", _ClientMessage), ("pad", c_long * 24)]


class _Error(Structure):
    _fields_ = [("type", c_int), ("display", c_void_p), ("resourceid", c_ulong), ("serial", c_ulong),
                ("error_code", c_ubyte), ("request_code", c_ubyte), ("minor_code", c_ubyte)]


class _Attributes(Structure):  # XSetWindowAttributes
    _fields_ = [("background_pixmap", c_ulong), ("background_pixel", c_ulong), ("border_pixmap", c_ulong),
                ("border_pixel", c_ulong), ("bit_gravity", c_int), ("win_gravity", c_int), ("backing_store", c_int),
                ("backing_planes", c_ulong), ("backing_pixel", c_ulong), ("save_under", c_int),
                ("event_mask", c_long), ("do_not_propagate_mask", c_long), ("override_redirect", c_int),
                ("colormap", c_ulong), ("cursor", c_ulong)]


class _VisualInfo(Structure):
    _fields_ = [("visual", c_void_p), ("visualid", c_ulong), ("screen", c_int), ("depth", c_int),
                ("c_class", c_int), ("red_mask", c_ulong), ("green_mask", c_ulong), ("blue_mask", c_ulong),
                ("colormap_size", c_int), ("bits_per_rgb", c_int)]


_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, POINTER(_Error))
_IO_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p)
_EXIT_HANDLER = ctypes.CFUNCTYPE(None, c_void_p, c_void_p)
_P = c_void_p
_SIGNATURES = {
    "x": {
        "XOpenDisplay": (_P, [c_char_p]), "XCloseDisplay": (c_int, [_P]), "XConnectionNumber": (c_int, [_P]),
        "XDefaultScreen": (c_int, [_P]), "XRootWindow": (c_ulong, [_P, c_int]),
        "XDefaultVisual": (_P, [_P, c_int]), "XDisplayWidth": (c_int, [_P, c_int]),
        "XDisplayHeight": (c_int, [_P, c_int]), "XInternAtom": (c_ulong, [_P, c_char_p, c_int]),
        "XGetSelectionOwner": (c_ulong, [_P, c_ulong]), "XSelectInput": (c_int, [_P, c_ulong, c_long]),
        "XSendEvent": (c_int, [_P, c_ulong, c_int, c_long, POINTER(XEvent)]),
        "XCreateWindow": (c_ulong, [_P, c_ulong, c_int, c_int, c_uint, c_uint, c_uint, c_int, c_uint, _P,
                                    c_ulong, POINTER(_Attributes)]),
        "XDestroyWindow": (c_int, [_P, c_ulong]), "XMapRaised": (c_int, [_P, c_ulong]),
        "XChangeProperty": (c_int, [_P, c_ulong, c_ulong, c_ulong, c_int, c_int, _P, c_int]),
        "XGetWindowProperty": (c_int, [_P, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong, POINTER(c_ulong),
                                       POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong), POINTER(_P)]),
        "XFree": (c_int, [_P]), "XPending": (c_int, [_P]), "XEventsQueued": (c_int, [_P, c_int]),
        "XNextEvent": (c_int, [_P, POINTER(XEvent)]), "XFlush": (c_int, [_P]), "XSync": (c_int, [_P, c_int]),
        "XGetVisualInfo": (_P, [_P, c_long, POINTER(_VisualInfo), POINTER(c_int)]),
        "XCreateColormap": (c_ulong, [_P, c_ulong, _P, c_int]), "XFreeColormap": (c_int, [_P, c_ulong]),
        "XClearWindow": (c_int, [_P, c_ulong]),
        "XGrabPointer": (c_int, [_P, c_ulong, c_int, c_uint, c_int, c_int, c_ulong, c_ulong, c_ulong]),
        "XUngrabPointer": (c_int, [_P, c_ulong]), "XGrabKeyboard": (c_int, [_P, c_ulong, c_int, c_int, c_int, c_ulong]),
        "XUngrabKeyboard": (c_int, [_P, c_ulong]), "XLookupKeysym": (c_ulong, [POINTER(_Key), c_int]),
        "XSetErrorHandler": (_P, [_ERROR_HANDLER]), "XSetIOErrorHandler": (_P, [_IO_ERROR_HANDLER]),
    },
    "cairo": {
        "cairo_xlib_surface_create": (_P, [_P, c_ulong, _P, c_int, c_int]),
        "cairo_image_surface_create": (_P, [c_int, c_int, c_int]),
        "cairo_image_surface_create_for_data": (_P, [_P, c_int, c_int, c_int, c_int]),
        "cairo_surface_create_similar": (_P, [_P, c_int, c_int, c_int]),
        "cairo_surface_flush": (None, [_P]), "cairo_surface_destroy": (None, [_P]),
        "cairo_create": (_P, [_P]), "cairo_destroy": (None, [_P]), "cairo_set_operator": (None, [_P, c_int]),
        "cairo_set_source_surface": (None, [_P, _P, c_double, c_double]),
        "cairo_set_source_rgba": (None, [_P, c_double, c_double, c_double, c_double]),
        "cairo_paint": (None, [_P]), "cairo_fill": (None, [_P]), "cairo_stroke": (None, [_P]),
        "cairo_set_line_width": (None, [_P, c_double]), "cairo_rectangle": (None, [_P] + [c_double] * 4),
        "cairo_move_to": (None, [_P, c_double, c_double]), "cairo_new_sub_path": (None, [_P]),
        "cairo_arc": (None, [_P] + [c_double] * 5), "cairo_close_path": (None, [_P]),
    },
    "pango": {
        "pango_font_description_from_string": (_P, [c_char_p]), "pango_font_description_free": (None, [_P]),
        "pango_layout_set_text": (None, [_P, c_char_p, c_int]),
        "pango_layout_set_font_description": (None, [_P, _P]),
        "pango_layout_get_pixel_size": (None, [_P, POINTER(c_int), POINTER(c_int)]),
        "pango_layout_get_context": (_P, [_P]), "pango_layout_context_changed": (None, [_P]),
    },
    "pangocairo": {
        "pango_cairo_create_layout": (_P, [_P]), "pango_cairo_show_layout": (None, [_P, _P]),
        "pango_cairo_context_set_resolution": (None, [_P, c_double]),
    },
    "gobject": {"g_object_unref": (None, [_P])},
}
_SONAMES = {"x": "libX11.so.6", "cairo": "libcairo.so.2", "pango": "libpango-1.0.so.0",
            "pangocairo": "libpangocairo-1.0.so.0", "gobject": "libgobject-2.0.so.0"}


class _Lib:
    """The C libraries, their functions typed."""

    def __init__(self) -> None:
        for name, soname in _SONAMES.items():
            library = ctypes.CDLL(soname)
            for function, (restype, argtypes) in _SIGNATURES[name].items():
                entry = getattr(library, function)
                entry.restype, entry.argtypes = restype, argtypes
            setattr(self, name, library)
        try:  # libX11 1.7 and later: losing the X server need not end the process
            self.set_exit_handler = self.x.XSetIOErrorExitHandler
            self.set_exit_handler.restype, self.set_exit_handler.argtypes = None, [_P, _EXIT_HANDLER, _P]
        except AttributeError:
            self.set_exit_handler = None


_lib: _Lib | None | bool = None  # False once loading failed
_errors: dict[int, int] = {}  # Siphon's own displays -> X errors since the last check
_handlers: list[Any] = []  # the installed error handlers and the ones before them, kept alive


def _load() -> _Lib | None:
    global _lib
    if _lib is None:
        try:
            _lib = _Lib()
        except (OSError, AttributeError) as exc:
            log.info("no XEmbed tray: %s", exc)
            _lib = False
    return _lib or None


def _watch_errors(lib: _Lib, display: int) -> None:
    """X errors on Siphon's displays are counted, and losing their server only reported, instead of ending the
    process (Xlib's defaults). Xlib has one handler of each per process: GTK's own X display, when there is one,
    keeps whatever handled it before."""
    _errors[display] = 0
    if _handlers:
        return

    def on_error(dpy, event) -> int:
        if dpy in _errors:
            _errors[dpy] += 1
            return 0
        return _ERROR_HANDLER(previous)(dpy, event) if previous else 0

    def on_io_error(dpy) -> int:
        if dpy in _errors:
            return 0  # then the display's exit handler, which marks the connection dead
        return _IO_ERROR_HANDLER(previous_io)(dpy) if previous_io else 0

    handler, io_handler = _ERROR_HANDLER(on_error), _IO_ERROR_HANDLER(on_io_error)
    previous = lib.x.XSetErrorHandler(handler)
    previous_io = lib.x.XSetIOErrorHandler(io_handler)
    _handlers.extend((handler, io_handler, previous, previous_io))


def _gtk_display() -> Any:
    """GTK's display, when the app has loaded GTK (never loaded from here)."""
    gdk = sys.modules.get("gi.repository.Gdk")
    return gdk.Display.get_default() if gdk is not None else None


def gtk_on_x11() -> bool:
    display = _gtk_display()
    return display is not None and type(display).__gtype__.name == "GdkX11Display"


def display_name() -> str | None:
    """The X display to look for a tray on: GTK's own when it draws on X11; DISPLAY on another backend only with
    SIPHON_XEMBED=1. None leaves the XEmbed tray out."""
    wanted = os.environ.get("SIPHON_XEMBED")
    if wanted == "0" or sys.platform == "win32":
        return None
    if gtk_on_x11():
        return _gtk_display().get_name()
    if wanted == "1":
        return os.environ.get("DISPLAY") or None
    return None


def start(owner: Any) -> "XEmbedIcon | None":
    """An icon for the X session's legacy tray, disabled until set_enabled(); None where there is no X to look
    on. Beside a Wayland GTK, only with a libX11 (1.7 or later) that lets Siphon outlive the X server."""
    name = display_name()
    lib = _load() if name else None
    if lib is None:
        return None
    if lib.set_exit_handler is None and not gtk_on_x11():
        log.info("no XEmbed tray: this libX11 would end Siphon with Xwayland")
        return None
    try:
        return XEmbedIcon(owner, _Connection(lib, name))
    except OSError as exc:
        log.warning("no XEmbed tray: %s", exc)
        return None


# ---------------------------------------------------------------- the connection


def _watch_fd(fd: int, callback: Callable[[int, GLib.IOCondition], bool]) -> int:
    """callback(fd, condition) from the main loop whenever fd can be read (or broke)."""
    condition = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
    try:  # GLib 2.80 moved it
        import gi

        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix

        return GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)
    except (ImportError, ValueError, AttributeError):
        return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)


class _Connection:
    """Siphon's own connection to the X server: events read on the GLib main loop and handed to the handler of the
    window they are for; X errors counted, and losing the server marks it dead instead of ending Siphon."""

    def __init__(self, lib: _Lib, name: str) -> None:
        self.lib, self.x = lib, lib.x
        self.dpy = self.x.XOpenDisplay(name.encode())
        if not self.dpy:
            raise OSError(f"cannot open the X display {name}")
        _watch_errors(lib, self.dpy)
        self.dead = False
        self.on_lost: Callable[[], None] | None = None
        self._exit = None
        if lib.set_exit_handler is not None:
            self._exit = _EXIT_HANDLER(self._on_io_error)
            lib.set_exit_handler(self.dpy, self._exit, None)
        self.screen = self.x.XDefaultScreen(self.dpy)
        self.root = self.x.XRootWindow(self.dpy, self.screen)
        self.default_visual = self.x.XDefaultVisual(self.dpy, self.screen)
        self._atoms: dict[str, int] = {}
        self._handlers: dict[int, Callable[[XEvent], None]] = {}
        self._draining = False
        self._idle = 0
        self._event = XEvent()
        self._source = _watch_fd(self.x.XConnectionNumber(self.dpy), self._on_readable)

    @property
    def screen_size(self) -> tuple[int, int]:
        return self.x.XDisplayWidth(self.dpy, self.screen), self.x.XDisplayHeight(self.dpy, self.screen)

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.x.XInternAtom(self.dpy, name.encode(), False)
        return self._atoms[name]

    def listen(self, window: int, handler: Callable[[XEvent], None]) -> None:
        self._handlers[window] = handler

    def forget(self, window: int) -> None:
        self._handlers.pop(window, None)

    def ok(self) -> bool:
        """Whether the requests since the last check went through without an X error (a round trip)."""
        if self.dead:
            return False
        self.x.XSync(self.dpy, False)
        errors, _errors[self.dpy] = _errors.get(self.dpy, 0), 0
        self.flush()
        return not self.dead and not errors

    def flush(self) -> None:
        """Send what is queued; events a round trip read meanwhile are handled soon, from the main loop."""
        if self.dead:
            return
        self.x.XFlush(self.dpy)
        if not self._draining and not self._idle and self.x.XEventsQueued(self.dpy, 0):
            self._idle = GLib.idle_add(self._on_idle)

    def close(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0
        self._handlers.clear()
        if not self.dead:
            self.dead = True
            self.x.XCloseDisplay(self.dpy)
        _errors.pop(self.dpy, None)

    # -- reading

    def _on_readable(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        self._drain()
        if self.dead:
            self._source = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _on_idle(self) -> bool:
        self._idle = 0
        self._drain()
        return GLib.SOURCE_REMOVE

    def _drain(self) -> None:
        if self._draining or self.dead:
            return
        self._draining = True
        event = self._event
        try:
            while not self.dead and self.x.XPending(self.dpy) > 0:
                self.x.XNextEvent(self.dpy, byref(event))
                handler = self._handlers.get(event.xany.window)
                if handler is not None:
                    try:
                        handler(event)
                    except Exception:
                        log.exception("tray: an X event went wrong")
        finally:
            self._draining = False
        if not self.dead:
            self.x.XFlush(self.dpy)

    def _on_io_error(self, _dpy, _data) -> None:
        """The X server went away (inside some Xlib call): nothing may use the connection any more."""
        self.dead = True
        GLib.idle_add(self._lost)

    def _lost(self) -> bool:
        log.warning("tray: the X server went away")
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        if self.on_lost is not None:
            self.on_lost()
        return GLib.SOURCE_REMOVE

    # -- requests

    def cardinal(self, window: int, name: str) -> int | None:
        """A 32-bit property's first value (a CARDINAL, VISUALID or WINDOW), None when unset."""
        kind, form, count, after, data = c_ulong(), c_int(), c_ulong(), c_ulong(), c_void_p()
        status = self.x.XGetWindowProperty(self.dpy, window, self.atom(name), 0, 1, False, 0, byref(kind),
                                           byref(form), byref(count), byref(after), byref(data))
        value = None
        if status == 0 and data.value:
            if form.value == 32 and count.value:
                value = ctypes.cast(data, POINTER(c_ulong))[0]
            self.x.XFree(data)
        return value

    def set_longs(self, window: int, name: str, kind: int, values: list[int]) -> None:
        array = (c_long * len(values))(*values)
        self.x.XChangeProperty(self.dpy, window, self.atom(name), kind, 32, _REPLACE, ctypes.cast(array, _P),
                               len(values))

    def set_bytes(self, window: int, name: str, kind: int, value: bytes) -> None:
        self.x.XChangeProperty(self.dpy, window, self.atom(name), kind, 8, _REPLACE, value, len(value))

    def send(self, target: int, window: int, kind: str, values: list[int], mask: int = 0) -> None:
        event = XEvent()
        message = event.xclient
        message.type, message.window, message.message_type, message.format = (_CLIENT_MESSAGE, window,
                                                                               self.atom(kind), 32)
        for index, value in enumerate(values):
            message.l[index] = value
        self.x.XSendEvent(self.dpy, target, False, mask, byref(event))

    def visual(self, mask: int, **fields) -> tuple[int, int] | None:
        """(Visual pointer, depth) of the first visual matching the fields, or None."""
        template, count = _VisualInfo(**fields), c_int()
        found = self.x.XGetVisualInfo(self.dpy, mask, byref(template), byref(count))
        if not found:
            return None
        info = ctypes.cast(found, POINTER(_VisualInfo))[0]
        result = (info.visual, info.depth)
        self.x.XFree(found)
        return result

    def argb_visual(self) -> tuple[int, int] | None:
        """A 32-bit visual for windows with see-through parts, where a compositing manager runs to show them."""
        if not self.x.XGetSelectionOwner(self.dpy, self.atom(f"_NET_WM_CM_S{self.screen}")):
            return None
        return self.visual(_VISUAL_SCREEN | _VISUAL_DEPTH | _VISUAL_CLASS, screen=self.screen, depth=32,
                           c_class=_TRUE_COLOR)

    def create(self, x: int, y: int, width: int, height: int, event_mask: int, visual: tuple[int, int] | None,
               override: bool = False) -> tuple[int, int, int]:
        """A window on the root: in the visual given (with a colormap of its own, see-through where unpainted) or
        the root's, its background the parent's. Returns (window, Visual pointer to draw with, colormap)."""
        attributes = _Attributes(event_mask=event_mask, override_redirect=override, save_under=override)
        mask = _CW_EVENT_MASK | (_CW_OVERRIDE_REDIRECT | _CW_SAVE_UNDER if override else 0)
        colormap = 0
        if visual is not None:
            colormap = self.x.XCreateColormap(self.dpy, self.root, visual[0], 0)
            attributes.colormap = colormap
            mask |= _CW_COLORMAP | _CW_BACK_PIXEL | _CW_BORDER_PIXEL
            pointer, depth = visual
        else:
            attributes.background_pixmap = _PARENT_RELATIVE
            mask |= _CW_BACK_PIXMAP
            pointer, depth = self.default_visual, 0  # CopyFromParent
        window = self.x.XCreateWindow(self.dpy, self.root, x, y, max(1, width), max(1, height), 0, depth,
                                      _INPUT_OUTPUT, visual[0] if visual else None, mask, byref(attributes))
        return window, pointer, colormap

    def paint(self, window: int, visual: int, width: int, height: int, draw: Callable[[int], None],
              over: bool = False) -> None:
        """draw(cr) into an offscreen copy, then onto the window: in one go, replacing it or (over) blended over
        what is there, the tray's background."""
        cairo = self.lib.cairo
        target = cairo.cairo_xlib_surface_create(self.dpy, window, visual, width, height)
        buffer = cairo.cairo_surface_create_similar(target, _COLOR_ALPHA, width, height)
        cr = cairo.cairo_create(buffer)
        draw(cr)
        cairo.cairo_destroy(cr)
        cr = cairo.cairo_create(target)
        cairo.cairo_set_operator(cr, _OVER if over else _SOURCE)
        cairo.cairo_set_source_surface(cr, buffer, 0, 0)
        cairo.cairo_paint(cr)
        cairo.cairo_destroy(cr)
        cairo.cairo_surface_flush(target)
        cairo.cairo_surface_destroy(buffer)
        cairo.cairo_surface_destroy(target)
        self.flush()


# ---------------------------------------------------------------- drawing


def icon_pixels(size: int, svg: Path = _SVG) -> bytes | None:
    """The icon, size by size, as cairo's ARGB32: premultiplied, in the machine's byte order. None on failure."""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), size, size)
    except (GLib.Error, ImportError, ValueError) as exc:
        log.warning("the tray icon has no picture: %s", getattr(exc, "message", exc))
        return None
    width, height, stride, channels = (min(size, pixbuf.get_width()), min(size, pixbuf.get_height()),
                                       pixbuf.get_rowstride(), pixbuf.get_n_channels())
    data = pixbuf.read_pixel_bytes().get_data()
    out = bytearray(size * size * 4)
    left, top = (size - width) // 2, (size - height) // 2
    order = (2, 1, 0, 3) if sys.byteorder == "little" else (1, 2, 3, 0)  # where R, G, B and A go
    for y in range(height):
        row = y * stride
        for x in range(width):
            at = row + x * channels
            alpha = data[at + 3] if channels == 4 else 255
            to = ((top + y) * size + left + x) * 4
            for channel in range(3):
                out[to + order[channel]] = (data[at + channel] * alpha + 127) // 255
            out[to + order[3]] = alpha
    return bytes(out)


class _Style:
    """The menu's and the tooltip's look: libadwaita's light or dark popover, the desktop's font, its DPI."""

    def __init__(self) -> None:
        dark, font, dpi = False, "Sans 10", 96.0
        try:
            if "gi.repository.Adw" in sys.modules:
                dark = sys.modules["gi.repository.Adw"].StyleManager.get_default().get_dark()
            if "gi.repository.Gtk" in sys.modules:
                settings = sys.modules["gi.repository.Gtk"].Settings.get_default()
                if settings is not None:
                    font = settings.props.gtk_font_name or font
                    dpi = settings.props.gtk_xft_dpi / 1024 if settings.props.gtk_xft_dpi > 0 else dpi
        except Exception:  # an odd GTK: the defaults do
            pass
        self.font, self.dpi, self.scale = font, dpi, dpi / 96
        self.background = (0.212, 0.212, 0.227, 1.0) if dark else (1.0, 1.0, 1.0, 1.0)
        self.foreground = (1.0, 1.0, 1.0, 1.0) if dark else (0.0, 0.0, 0.024, 0.8)
        self.border = (1.0, 1.0, 1.0, 0.12) if dark else (0.0, 0.0, 0.0, 0.14)

    def px(self, value: float) -> int:
        return max(1, round(value * self.scale))

    def faded(self, alpha: float) -> tuple[float, float, float, float]:
        red, green, blue, own = self.foreground
        return red, green, blue, own * alpha


class _Text:
    """Pango layouts on a cairo context, in the style's font and DPI."""

    def __init__(self, lib: _Lib, style: _Style) -> None:
        self._lib, self._style = lib, style
        self._font = lib.pango.pango_font_description_from_string(style.font.encode())

    def layout(self, cr: int, text: str) -> int:
        lib = self._lib
        layout = lib.pangocairo.pango_cairo_create_layout(cr)
        lib.pangocairo.pango_cairo_context_set_resolution(lib.pango.pango_layout_get_context(layout),
                                                          self._style.dpi)
        lib.pango.pango_layout_context_changed(layout)
        lib.pango.pango_layout_set_font_description(layout, self._font)
        lib.pango.pango_layout_set_text(layout, text.encode(), -1)
        return layout

    def size(self, text: str) -> tuple[int, int]:
        cairo = self._lib.cairo
        surface = cairo.cairo_image_surface_create(_ARGB32, 1, 1)
        cr = cairo.cairo_create(surface)
        layout = self.layout(cr, text)
        width, height = c_int(), c_int()
        self._lib.pango.pango_layout_get_pixel_size(layout, byref(width), byref(height))
        self._lib.gobject.g_object_unref(layout)
        cairo.cairo_destroy(cr)
        cairo.cairo_surface_destroy(surface)
        return width.value, height.value

    def show(self, cr: int, text: str, x: float, y: float, color: tuple[float, float, float, float]) -> None:
        cairo = self._lib.cairo
        cairo.cairo_set_source_rgba(cr, *color)
        cairo.cairo_move_to(cr, x, y)
        layout = self.layout(cr, text)
        self._lib.pangocairo.pango_cairo_show_layout(cr, layout)
        self._lib.gobject.g_object_unref(layout)

    def close(self) -> None:
        self._lib.pango.pango_font_description_free(self._font)


def _rounded(cairo: Any, cr: int, x: float, y: float, width: float, height: float, radius: float) -> None:
    if radius <= 0:
        cairo.cairo_rectangle(cr, x, y, width, height)
        return
    radius = min(radius, width / 2, height / 2)
    cairo.cairo_new_sub_path(cr)
    for cx, cy, start in ((x + width - radius, y + radius, -0.5), (x + width - radius, y + height - radius, 0.0),
                          (x + radius, y + height - radius, 0.5), (x + radius, y + radius, 1.0)):
        cairo.cairo_arc(cr, cx, cy, radius, start * 3.141592653589793, (start + 0.5) * 3.141592653589793)
    cairo.cairo_close_path(cr)


class _Popup:
    """An override-redirect window drawn with cairo and Pango (the menu, the tooltip): rounded and see-through at
    the corners where a compositing manager shows that, square otherwise."""

    kind = ""  # its _NET_WM_WINDOW_TYPE
    radius = 12  # its corners', at 96 dpi, where they can be round

    def __init__(self, x: _Connection) -> None:
        self._x = x
        self.window = 0
        self._visual = self._colormap = 0
        self._rounded = False
        self.size = (0, 0)
        self.position = (0, 0)

    def _open(self, x: int, y: int, width: int, height: int, event_mask: int) -> None:
        conn = self._x
        argb = conn.argb_visual()
        self.window, self._visual, self._colormap = conn.create(x, y, width, height, event_mask | _EXPOSURE_MASK,
                                                                argb, override=True)
        self._rounded = argb is not None
        self.size, self.position = (width, height), (x, y)
        conn.set_longs(self.window, "_NET_WM_WINDOW_TYPE", _XA_ATOM, [conn.atom(self.kind)])
        conn.set_bytes(self.window, "WM_CLASS", _XA_STRING, b"siphon\0Siphon\0")
        conn.listen(self.window, self._on_event)
        conn.x.XMapRaised(conn.dpy, self.window)
        conn.flush()

    def close(self) -> None:
        if not self.window:
            return
        conn, window = self._x, self.window
        self.window = 0
        conn.forget(window)
        if not conn.dead:
            conn.x.XDestroyWindow(conn.dpy, window)
            if self._colormap:
                conn.x.XFreeColormap(conn.dpy, self._colormap)
            conn.flush()
        self._colormap = 0

    def _place(self, x: int, y: int, width: int, height: int, gap: int) -> tuple[int, int]:
        """Below and right of the point, or above or left of it where the screen ends first."""
        screen_width, screen_height = self._x.screen_size
        left = x if x + width <= screen_width else max(0, x - width)
        top = y + gap if y + gap + height <= screen_height else max(0, y - gap - height)
        return left, top

    def _paint(self) -> None:
        if self.window and not self._x.dead:
            width, height = self.size
            self._x.paint(self.window, self._visual, width, height, self._draw)

    def _frame(self, cr: int, style: _Style) -> float:
        """The background and border; returns the corner radius."""
        cairo = self._x.lib.cairo
        width, height = self.size
        radius = style.px(self.radius) if self._rounded else 0
        _rounded(cairo, cr, 0.5, 0.5, width - 1, height - 1, radius)
        cairo.cairo_set_source_rgba(cr, *style.background)
        cairo.cairo_fill(cr)
        _rounded(cairo, cr, 0.5, 0.5, width - 1, height - 1, radius)
        cairo.cairo_set_source_rgba(cr, *style.border)
        cairo.cairo_set_line_width(cr, 1.0)
        cairo.cairo_stroke(cr)
        return radius

    def _draw(self, cr: int) -> None:
        raise NotImplementedError

    def _on_event(self, event: XEvent) -> None:
        if event.type == _EXPOSE and event.xexpose.count == 0:
            self._paint()


class _Tooltip(_Popup):
    """"Siphon: Title – Artist" after the pointer rests on the icon a moment: X trays leave titles to the icon."""

    kind = "_NET_WM_WINDOW_TYPE_TOOLTIP"
    radius = 6

    def __init__(self, x: _Connection) -> None:
        super().__init__(x)
        self._timer = 0
        self._at = (0, 0)
        self.text = ""
        self._style: _Style | None = None
        self._text: _Text | None = None

    def schedule(self, x: int, y: int, text: str) -> None:
        self.cancel()
        self._at, self.text = (x, y), text
        self._timer = GLib.timeout_add(_TOOLTIP_DELAY_MS, self._show)

    def cancel(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.close()

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            if self.window:
                self.close()
                self._show()

    def _show(self) -> bool:
        self._timer = 0
        if self._x.dead:
            return GLib.SOURCE_REMOVE
        style = _Style()
        self._style, self._text = style, _Text(self._x.lib, style)
        width, height = self._text.size(self.text)
        size = (width + 2 * style.px(10), height + 2 * style.px(6))
        self._open(*self._place(*self._at, *size, style.px(18)), *size, 0)
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        super().close()
        if self._text is not None:
            self._text.close()
            self._text = None

    def _draw(self, cr: int) -> None:
        style = self._style
        self._frame(cr, style)
        self._text.show(cr, self.text, style.px(10), style.px(6), style.foreground)


class _Menu(_Popup):
    """The tray menu: menus.menu() drawn like a libadwaita popover menu, run by pointer or keyboard; a click
    outside closes it."""

    kind = "_NET_WM_WINDOW_TYPE_POPUP_MENU"
    _EVENTS = _PRESS_MASK | _RELEASE_MASK | _MOTION_MASK | _ENTER_MASK | _LEAVE_MASK

    def __init__(self, x: _Connection, run: Callable[[str, int], None]) -> None:
        super().__init__(x)
        self._run = run  # (command, X time of the click or key)
        self.items: list[menus.Item] = []
        self._rows: list[tuple[int, int, menus.Item]] = []  # top, bottom, item
        self.hover = -1
        self._grab_tries = 0
        self._grab_retry = 0
        self._text: _Text | None = None
        self._sizes: dict[str, tuple[int, int]] = {}
        self._anchor = (0, 0)

    @property
    def is_open(self) -> bool:
        return bool(self.window)

    def open(self, x: int, y: int, items: list[menus.Item]) -> None:
        self.close()
        style = _Style()
        self._style, self._text, self._sizes = style, _Text(self._x.lib, style), {}
        self.items, self._anchor, self.hover = items, (x, y), -1
        pad, inset = style.px(6), style.px(12)
        sizes = [self._measure(item.label) for item in items if not item.separator]
        row = max(style.px(32), max(height for _width, height in sizes) + style.px(12))
        width = max(style.px(160), max(width for width, _height in sizes) + 2 * (pad + inset))
        top, self._rows = pad, []
        for item in items:
            bottom = top + (style.px(13) if item.separator else row)
            self._rows.append((top, bottom, item))
            top = bottom
        size = (width, top + pad)
        self._open(*self._place(x, y, *size, 0), *size, self._EVENTS | _KEY_MASK)
        self._grab_tries = 0
        self._grab()

    def _measure(self, label: str) -> tuple[int, int]:
        if label not in self._sizes:
            self._sizes[label] = self._text.size(label)
        return self._sizes[label]

    def update(self, items: list[menus.Item]) -> None:
        """The player changed while the menu is open: its labels follow (a song coming or going reopens it)."""
        if not self.window:
            return
        if [item.id for item in items] != [item.id for item in self.items]:
            hover = self.hover
            self.open(*self._anchor, items)
            self.hover = hover if 0 <= hover < len(items) else -1
            return
        self.items = items
        self._rows = [(top, bottom, item) for (top, bottom, _old), item in zip(self._rows, items)]
        self._paint()

    def close(self) -> None:
        if self._grab_retry:
            GLib.source_remove(self._grab_retry)
            self._grab_retry = 0
        if self.window and not self._x.dead:
            self._x.x.XUngrabPointer(self._x.dpy, 0)
            self._x.x.XUngrabKeyboard(self._x.dpy, 0)
        super().close()
        if self._text is not None:
            self._text.close()
            self._text = None

    def _grab(self) -> bool:
        self._grab_retry = 0
        if not self.window or self._x.dead:
            return GLib.SOURCE_REMOVE
        conn = self._x
        pointer = conn.x.XGrabPointer(conn.dpy, self.window, True, self._EVENTS, _GRAB_ASYNC, _GRAB_ASYNC, 0, 0, 0)
        if pointer == _GRAB_SUCCESS:
            conn.x.XGrabKeyboard(conn.dpy, self.window, True, _GRAB_ASYNC, _GRAB_ASYNC, 0)
        else:
            self._grab_tries += 1
            if self._grab_tries < _GRAB_TRIES:
                self._grab_retry = GLib.timeout_add(_GRAB_RETRY_MS, self._grab)
            else:
                log.warning("tray: the menu could not grab the pointer; Escape or the icon closes it")
        conn.flush()
        return GLib.SOURCE_REMOVE

    def _row_at(self, x: int, y: int) -> int:
        width, _height = self.size
        if not 0 <= x < width:
            return -1
        return next((index for index, (top, bottom, item) in enumerate(self._rows)
                     if top <= y < bottom and not item.separator and item.enabled and item.command), -1)

    def _inside(self, x: int, y: int) -> bool:
        width, height = self.size
        return 0 <= x < width and 0 <= y < height

    def _hover(self, index: int) -> None:
        if index != self.hover:
            self.hover = index
            self._paint()

    def activate(self, index: int, time: int = 0) -> None:
        """Run a row's command, the menu gone first."""
        item = self._rows[index][2] if 0 <= index < len(self._rows) else None
        if item is not None and item.command and item.enabled:
            self.close()
            self._run(item.command, time)

    def _step(self, key: str) -> None:
        choices = [index for index, (_top, _bottom, item) in enumerate(self._rows)
                   if not item.separator and item.enabled and item.command]
        if not choices:
            return
        if key == "home" or (key == "down" and self.hover not in choices):
            self._hover(choices[0])
        elif key == "end" or (key == "up" and self.hover not in choices):
            self._hover(choices[-1])
        else:
            at = choices.index(self.hover) + (1 if key == "down" else -1)
            self._hover(choices[at % len(choices)])

    def _on_event(self, event: XEvent) -> None:
        kind = event.type
        if kind == _MOTION:
            self._hover(self._row_at(event.xmotion.x, event.xmotion.y))
        elif kind == _LEAVE:
            self._hover(-1)
        elif kind == _BUTTON_PRESS:
            if not self._inside(event.xbutton.x, event.xbutton.y):
                self.close()  # a click anywhere else
        elif kind == _BUTTON_RELEASE:
            if self._inside(event.xbutton.x, event.xbutton.y):
                self.activate(self._row_at(event.xbutton.x, event.xbutton.y), event.xbutton.time)
        elif kind == _KEY_PRESS:
            key = _KEYS.get(self._x.x.XLookupKeysym(byref(event.xkey), 0))
            if key == "escape":
                self.close()
            elif key == "enter":
                self.activate(self.hover, event.xkey.time)
            elif key:
                self._step(key)
        else:
            super()._on_event(event)

    def _draw(self, cr: int) -> None:
        style, cairo = self._style, self._x.lib.cairo
        self._frame(cr, style)
        width, _height = self.size
        pad, inset = style.px(6), style.px(12)
        for index, (top, bottom, item) in enumerate(self._rows):
            if item.separator:
                cairo.cairo_set_source_rgba(cr, *style.faded(0.15))
                cairo.cairo_rectangle(cr, pad, (top + bottom) // 2, width - 2 * pad, 1)
                cairo.cairo_fill(cr)
                continue
            if index == self.hover:
                _rounded(cairo, cr, pad, top, width - 2 * pad, bottom - top, style.px(6))
                cairo.cairo_set_source_rgba(cr, *style.faded(0.1))
                cairo.cairo_fill(cr)
            _text_width, text_height = self._measure(item.label)
            self._text.show(cr, item.label, pad + inset, top + (bottom - top - text_height) / 2,
                            style.foreground if item.enabled else style.faded(0.5))


# ---------------------------------------------------------------- the icon


class XEmbedIcon:
    """Siphon's icon in the X tray while enabled and a manager runs (available once the manager embeds it).

    owner is what the backend reports to (tray.Tray or linuxtray's relay): its state, run, scroll, set_token and
    set_available."""

    session_ending = False  # a Windows matter

    def __init__(self, owner: Any, x: _Connection) -> None:
        self._owner = owner
        self._x = x
        self._state: menus.State = owner.state
        self._selection = x.atom(f"_NET_SYSTEM_TRAY_S{x.screen}")
        self._enabled = False
        self.manager = 0  # the manager's window, while Siphon's icon is in (or on its way to) that tray
        self.icon = 0
        self.docked = False
        self._visual = self._colormap = 0
        self._argb = False
        self.size = (0, 0)
        self._pixels: tuple[int, bytes | None] = (0, None)  # the icon for the size it was last drawn at
        self._pressed = 0
        self._swallow = 0  # the press that closed the menu: its release does nothing
        self.menu = _Menu(x, self._run)
        self.tooltip = _Tooltip(x)
        x.on_lost = self._on_connection_lost
        # A new manager announces itself to every client with a MANAGER message on the root window.
        x.x.XSelectInput(x.dpy, x.root, _STRUCTURE_MASK)
        x.listen(x.root, self._on_root_event)
        x.flush()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enabled, the icon docks into the tray now or whenever one appears; disabled, it leaves the tray."""
        if enabled == self._enabled or self._x.dead:
            return
        self._enabled = enabled
        if enabled:
            self._find_manager()
        else:
            self._leave_manager()
            self._owner.set_available(False)
        self._x.flush()

    def update(self, state: menus.State) -> None:
        self._state = state
        self.menu.update(menus.menu(state))
        self.tooltip.set_text(menus.title(state))

    def balloon(self, _heading: str, _body: str) -> bool:
        return False  # the app sends the desktop a notification of its own

    def close(self) -> None:
        self._enabled = False
        self._leave_manager()
        self._x.close()

    # -- the manager

    def _find_manager(self) -> None:
        conn = self._x
        if not self._enabled or conn.dead:
            return
        conn.ok()  # forget errors from before
        manager = conn.x.XGetSelectionOwner(conn.dpy, self._selection)
        if manager and manager == self.manager and self.icon:
            return
        self._leave_manager()
        if not manager:
            self._owner.set_available(False)
            return
        conn.x.XSelectInput(conn.dpy, manager, _STRUCTURE_MASK)
        if not conn.ok():  # it quit meanwhile; the next one announces itself
            self._owner.set_available(False)
            return
        self.manager = manager
        conn.listen(manager, self._on_manager_event)
        self._dock()

    def _dock(self) -> None:
        conn = self._x
        visual_id = conn.cardinal(self.manager, "_NET_SYSTEM_TRAY_VISUAL")
        visual = conn.visual(_VISUAL_ID, visualid=visual_id) if visual_id else None
        self._argb = visual is not None and visual[1] == 32
        size = conn.cardinal(self.manager, "_NET_SYSTEM_TRAY_ICON_SIZE") or _ICON_SIZE
        events = _EXPOSURE_MASK | _STRUCTURE_MASK | _PRESS_MASK | _RELEASE_MASK | _ENTER_MASK | _LEAVE_MASK
        self.icon, self._visual, self._colormap = conn.create(0, 0, size, size, events,
                                                              visual if self._argb else None)
        self.size, self.docked = (size, size), False
        conn.set_longs(self.icon, "_XEMBED_INFO", conn.atom("_XEMBED_INFO"), [0, _XEMBED_MAPPED])
        conn.set_bytes(self.icon, "WM_CLASS", _XA_STRING, b"siphon\0Siphon\0")
        conn.set_bytes(self.icon, "WM_NAME", _XA_STRING, b"Siphon")
        conn.set_bytes(self.icon, "_NET_WM_NAME", conn.atom("UTF8_STRING"), b"Siphon")
        conn.set_longs(self.icon, "_NET_WM_PID", _XA_CARDINAL, [os.getpid()])
        conn.listen(self.icon, self._on_icon_event)
        conn.send(self.manager, self.manager, "_NET_SYSTEM_TRAY_OPCODE", [0, _REQUEST_DOCK, self.icon])
        conn.flush()

    def _leave_manager(self) -> None:
        """Out of the tray: the icon's window destroyed (the manager drops its slot), the manager forgotten."""
        self.menu.close()
        self.tooltip.cancel()
        conn = self._x
        if self.icon:
            conn.forget(self.icon)
            if not conn.dead:
                conn.x.XDestroyWindow(conn.dpy, self.icon)
                if self._colormap:
                    conn.x.XFreeColormap(conn.dpy, self._colormap)
        if self.manager:
            conn.forget(self.manager)
        self.icon = self.manager = self._colormap = 0
        self.docked = False
        conn.flush()

    def _manager_lost(self) -> None:
        self._leave_manager()
        self._owner.set_available(False)
        self._find_manager()  # a new one may own the selection already

    def _on_root_event(self, event: XEvent) -> None:
        message = event.xclient
        if (event.type == _CLIENT_MESSAGE and message.message_type == self._x.atom("MANAGER")
                and message.l[1] == self._selection and self._enabled):
            self._find_manager()

    def _on_manager_event(self, event: XEvent) -> None:
        if event.type == _DESTROY:
            self._manager_lost()

    def _on_connection_lost(self) -> None:
        self._leave_manager()  # nothing reaches the dead connection
        self._owner.set_available(False)

    # -- the icon

    def _embedded(self) -> None:
        if not self.docked:
            self.docked = True
            self._owner.set_available(True)

    def _on_icon_event(self, event: XEvent) -> None:
        kind = event.type
        if kind == _REPARENT:
            if event.xreparent.parent == self._x.root:
                self._manager_lost()  # the manager let go of it: it is quitting
            else:
                self._embedded()
        elif kind == _CLIENT_MESSAGE:
            if event.xclient.message_type == self._x.atom("_XEMBED") and event.xclient.l[1] == _EMBEDDED_NOTIFY:
                self._embedded()
        elif kind == _CONFIGURE:
            size = (event.xconfigure.width, event.xconfigure.height)
            if size != self.size:
                self.size = size
                self._draw()
        elif kind == _EXPOSE:
            if event.xexpose.count == 0:
                self._draw()
        elif kind == _BUTTON_PRESS:
            self._on_press(event.xbutton)
        elif kind == _BUTTON_RELEASE:
            self._on_release(event.xbutton)
        elif kind == _ENTER:
            if not self.menu.is_open:
                self.tooltip.schedule(event.xbutton.x_root, event.xbutton.y_root, menus.title(self._state))
        elif kind == _LEAVE:
            self.tooltip.cancel()

    def _on_press(self, button: _Button) -> None:
        self.tooltip.cancel()
        if self.menu.is_open:
            self.menu.close()
            self._swallow = button.button
            return
        if button.button in (4, 5):
            self._owner.scroll(1.0 if button.button == 4 else -1.0)
        else:
            self._pressed = button.button

    def _on_release(self, button: _Button) -> None:
        pressed, self._pressed = self._pressed, 0
        if self._swallow == button.button:
            self._swallow = 0
            return
        width, height = self.size
        if button.button != pressed or not (0 <= button.x < width and 0 <= button.y < height):
            return  # dragged off the icon
        if pressed == 1:
            self._run(menus.TOGGLE, button.time)
        elif pressed == 2:
            self._run(menus.PLAY_PAUSE, button.time)
        elif pressed == 3:
            self.menu.open(button.x_root, button.y_root, menus.menu(self._state))

    def _run(self, command: str, time: int = 0) -> None:
        def run() -> bool:
            if time and command in (menus.SHOW, menus.TOGGLE):
                # GTK on X11 presents the window with the click's time, so that the window manager lets it in front
                self._owner.set_token(f"_TIME{time}")
            self._owner.run(command)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run)  # after this event: the menu's grab is gone first, then the window opens or Siphon quits

    def _draw(self) -> None:
        conn = self._x
        if not self.icon or conn.dead:
            return
        width, height = self.size
        side = min(width, height)
        if side <= 0:
            return
        if self._pixels[0] != side:
            self._pixels = (side, icon_pixels(side))
        pixels = self._pixels[1]
        conn.x.XClearWindow(conn.dpy, self.icon)  # the tray's background, or clear in an ARGB tray
        if pixels is None:
            conn.flush()
            return
        cairo = conn.lib.cairo
        image = ctypes.create_string_buffer(pixels, len(pixels))

        def draw(cr: int) -> None:
            surface = cairo.cairo_image_surface_create_for_data(image, _ARGB32, side, side, side * 4)
            cairo.cairo_set_source_surface(cr, surface, (width - side) // 2, (height - side) // 2)
            cairo.cairo_paint(cr)
            cairo.cairo_surface_destroy(surface)

        conn.paint(self.icon, self._visual, width, height, draw, over=True)
