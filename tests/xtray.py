"""Test fixtures for the XEmbed tray: an X server nobody sees, and a legacy system tray on it.

x_server() starts Xvfb, or else gamescope's headless backend (which runs Xwayland inside it, with no output
anywhere), and never the desktop's own X or Wayland. TrayManager is the manager side of the freedesktop System Tray
Protocol, as a bar keeps it: it owns _NET_SYSTEM_TRAY_S0, announces itself with MANAGER, docks the icons that ask
into a panel window of its own and sizes them; it reads back what an icon drew, moves the pointer (a warp) and
clicks and types through XTEST, so that grabs and hit-testing work as for real input. Needs python-xlib (a test dependency only).
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from Xlib import X, Xatom, display as xdisplay
from Xlib.ext import xtest
from Xlib.protocol import event as xevent

_ENV_DROP = ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY")


def _die_with_parent() -> None:
    """In the server's process: killed with the tests, should they end without cleaning up."""
    import ctypes

    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG


@contextmanager
def x_server(width: int = 1280, height: int = 800):
    """Yields the name of a private, invisible X display, or None when this machine can start none."""
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
    runtime = Path(tempfile.mkdtemp(prefix="sx-", dir="/tmp"))  # short: socket paths are limited
    runtime.chmod(0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    named = runtime / "display"
    if shutil.which("Xvfb"):
        read, write = os.pipe()
        command = ["Xvfb", "-displayfd", str(write), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"]
        server = subprocess.Popen(command, env=env, pass_fds=(write,), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True,
                                  preexec_fn=_die_with_parent)
        os.close(write)
        number = os.read(read, 16).decode().strip()
        os.close(read)
        name = f":{number}" if number else None
    elif shutil.which("gamescope"):
        command = ["gamescope", "--backend", "headless", "-W", str(width), "-H", str(height), "--",
                   "sh", "-c", f'echo "$DISPLAY" > {named}.part && mv {named}.part {named}; exec sleep infinity']
        server = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True,
                                  preexec_fn=_die_with_parent)
        deadline = time.monotonic() + 20
        while not named.exists() and server.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        name = named.read_text().strip() if named.exists() else None
    else:
        server, name = None, None
    try:
        yield name
    finally:
        if server is not None:
            try:
                os.killpg(server.pid, signal.SIGKILL)  # gamescope's reaper, Xwayland and the sleep with it
            except ProcessLookupError:
                pass
            server.wait(10)
        shutil.rmtree(runtime, ignore_errors=True)


class TrayManager:
    """A bar's legacy system tray: a panel at (x, y) with slot-by-slot room for icons, in the screen's own
    visual on a plain background, or offering a 32-bit visual (_NET_SYSTEM_TRAY_VISUAL) as compositing trays do.

    The panel is as big as the tests need from the start, inside a bar window: gamescope's window manager shrinks a
    top-level override-redirect window to its first child and will not grow it again."""

    _BAR = (384, 48)

    def __init__(self, name: str, argb: bool = False, x: int = 880, y: int = 740, slot: int = 24,
                 background: int = 0x336699, announce: bool = True) -> None:
        self.d = xdisplay.Display(name)
        self.screen = self.d.screen()
        self.root = self.screen.root
        self.slot, self.origin, self.background = slot, (x, y), background
        self.atom = self.d.intern_atom
        events = dict(event_mask=X.SubstructureNotifyMask | X.StructureNotifyMask)
        self.visual_id = None
        if argb:
            self.visual_id = next(v.visual_id for depth in self.screen.allowed_depths if depth.depth == 32
                                  for v in depth.visuals if v.visual_class == X.TrueColor)
            colormap = self.root.create_colormap(self.visual_id, X.AllocNone)
            look = dict(background_pixel=0, border_pixel=0, colormap=colormap)
            self.bar = self.root.create_window(x, y, *self._BAR, 0, 32, X.InputOutput, self.visual_id,
                                               override_redirect=True, **look)
            self.panel = self.bar.create_window(0, 0, *self._BAR, 0, 32, X.InputOutput, self.visual_id,
                                                **look, **events)
            self.panel.change_property(self.atom("_NET_SYSTEM_TRAY_VISUAL"), self.atom("VISUALID"), 32,
                                       [self.visual_id])
        else:
            depth = self.screen.root_depth
            self.bar = self.root.create_window(x, y, *self._BAR, 0, depth, X.InputOutput, X.CopyFromParent,
                                               background_pixel=background, override_redirect=True)
            self.panel = self.bar.create_window(0, 0, *self._BAR, 0, depth, X.InputOutput, X.CopyFromParent,
                                                background_pixel=background, **events)
        self.panel.map()
        self.bar.map()
        self.bar.configure(stack_mode=X.Above)
        self.selection = self.atom("_NET_SYSTEM_TRAY_S0")
        self.icons: list = []
        self.requests: list[int] = []
        self.gone: list[int] = []
        if announce:
            self.take()

    def take(self) -> None:
        """Own the selection and tell every client, as a starting tray does."""
        self.panel.set_selection_owner(self.selection, X.CurrentTime)
        message = xevent.ClientMessage(window=self.root, client_type=self.atom("MANAGER"),
                                       data=(32, [X.CurrentTime, self.selection, self.panel.id, 0, 0]))
        self.root.send_event(message, event_mask=X.StructureNotifyMask)
        self.d.sync()

    def pump(self) -> None:
        """Handle what arrived: dock requests, icons going away."""
        while self.d.pending_events():
            event = self.d.next_event()
            if event.type == X.ClientMessage and event.client_type == self.atom("_NET_SYSTEM_TRAY_OPCODE"):
                opcode, window = event.data[1][1], event.data[1][2]
                if opcode == 0:
                    self.requests.append(window)
                    self._dock(window)
            elif event.type == X.DestroyNotify and event.window.id in [icon.id for icon in self.icons]:
                self.gone.append(event.window.id)
                self.icons = [icon for icon in self.icons if icon.id != event.window.id]
            elif event.type == X.ReparentNotify and event.parent.id != self.panel.id and \
                    event.window.id in [icon.id for icon in self.icons]:
                self.icons = [icon for icon in self.icons if icon.id != event.window.id]

    def _dock(self, window_id: int) -> None:
        icon = self.d.create_resource_object("window", window_id)
        icon.change_attributes(event_mask=X.StructureNotifyMask)
        icon.change_save_set(X.SetModeInsert)
        icon.reparent(self.panel, len(self.icons) * self.slot, 0)
        icon.configure(width=self.slot, height=self.slot)
        icon.map()
        notify = xevent.ClientMessage(window=icon, client_type=self.atom("_XEMBED"),
                                      data=(32, [X.CurrentTime, 0, 0, self.panel.id, 0]))
        icon.send_event(notify)
        self.icons.append(icon)
        self.d.sync()

    def release(self, index: int = 0) -> None:
        """Let an icon go without quitting, as a tray does first when it quits by itself: back to the root."""
        icon = self.icons.pop(index)
        icon.unmap()
        icon.reparent(self.root, 0, 0)
        self.d.sync()

    def icon_rect(self, index: int = 0) -> tuple[int, int, int, int]:
        """Root x, y, width, height of a docked icon."""
        geometry = self.icons[index].get_geometry()
        return self.origin[0] + geometry.x, self.origin[1] + geometry.y, geometry.width, geometry.height

    def image(self, index: int = 0) -> tuple[int, int, int, bytes]:
        """What the icon shows: width, height, depth and its pixels as the server keeps them (BGRX or
        premultiplied BGRA, 4 bytes each)."""
        icon = self.icons[index]
        geometry = icon.get_geometry()
        raw = icon.get_image(0, 0, geometry.width, geometry.height, X.ZPixmap, 0xFFFFFFFF)
        return geometry.width, geometry.height, raw.depth, raw.data

    def resize(self, width: int, height: int, index: int = 0) -> None:
        """A bigger or smaller slot for the icon, up to the panel's height."""
        self.icons[index].configure(width=width, height=height)
        self.d.sync()

    def move_pointer(self, x: int, y: int) -> None:
        # A warp: gamescope's Xwayland takes XTEST motion as relative to where the pointer is.
        self.root.warp_pointer(x, y)
        self.d.sync()

    def click_at(self, x: int, y: int, button: int = 1) -> None:
        self.bar.configure(stack_mode=X.Above)
        self.move_pointer(x, y)
        xtest.fake_input(self.d, X.ButtonPress, button)
        self.d.sync()
        xtest.fake_input(self.d, X.ButtonRelease, button)
        self.d.sync()

    def click(self, button: int = 1, index: int = 0) -> None:
        x, y, width, height = self.icon_rect(index)
        self.click_at(x + width // 2, y + height // 2, button)

    def key(self, keysym: int) -> None:
        code = self.d.keysym_to_keycode(keysym)
        xtest.fake_input(self.d, X.KeyPress, code)
        xtest.fake_input(self.d, X.KeyRelease, code)
        self.d.sync()

    def owner(self) -> int:
        return self.d.get_selection_owner(self.selection).id if self.d.get_selection_owner(self.selection) else 0

    def windows_named(self, kind: str) -> list[tuple[int, int, int, int]]:
        """Mapped override-redirect windows on the root with this _NET_WM_WINDOW_TYPE: (x, y, width, height)."""
        found = []
        wanted = self.atom(f"_NET_WM_WINDOW_TYPE_{kind}")
        for child in self.root.query_tree().children:
            try:
                prop = child.get_full_property(self.atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM)
                if prop is None or wanted not in prop.value:
                    continue
                if child.get_attributes().map_state != X.IsViewable:
                    continue
                geometry = child.get_geometry()
                found.append((geometry.x, geometry.y, geometry.width, geometry.height))
            except Exception:  # gone meanwhile
                continue
        return found

    def stop(self) -> None:
        """Quit as a tray does: the connection closes, the icons go back to the root (the save set)."""
        self.d.close()
