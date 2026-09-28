"""Linux: Siphon's tray icon wherever a bar can show one, switching as bars come and go.

First choice, a StatusNotifierItem (siphon/sni.py) while a bar hosts those: KDE, Waybar, Quickshell, GNOME with the
AppIndicator extension and most others today. Without one, an icon in the X session's legacy system tray
(siphon/xembed.py): i3bar, polybar, tint2, trayer, stalonetray, older XFCE, MATE and LXDE panels. The XEmbed icon
leaves its tray again as soon as a StatusNotifierItem host appears, so a desktop with both never shows two icons.
With neither, the tray is unavailable and Siphon runs in the background only while it plays or downloads
(siphon/app.py).
"""

from collections.abc import Callable
from typing import Any

from . import tray as menus


class _Relay:
    """What one kind of icon sees of the Tray: everything passes through but availability, which LinuxTray
    decides from both kinds."""

    def __init__(self, tray: menus.Tray, report: Callable[[bool], None]) -> None:
        self._tray, self._report = tray, report

    @property
    def state(self) -> menus.State:
        return self._tray.state

    def run(self, command: str) -> None:
        self._tray.run(command)

    def scroll(self, notches: float) -> None:
        self._tray.scroll(notches)

    def set_token(self, token: str) -> None:
        self._tray.set_token(token)

    def set_available(self, available: bool) -> None:
        self._report(available)


def _start_xembed(owner: Any) -> Any:
    from . import xembed

    return xembed.start(owner)


class LinuxTray:
    """The Tray's backend on Linux. bus is the session bus (None without one); start_xembed(owner) makes the
    XEmbed icon or returns None where there is no X to look on, and is called the first time no
    StatusNotifierItem host shows Siphon."""

    session_ending = False  # a Windows matter

    def __init__(self, tray: menus.Tray, bus: Any, start_xembed: Callable[[Any], Any] = _start_xembed) -> None:
        self._tray = tray
        self._start_xembed = start_xembed
        self._sni_shown = self._x_shown = False
        self._x: Any = None
        self._x_tried = False
        self._closed = False
        self._sni: Any = None
        if bus is not None:
            from .sni import StatusNotifierItem

            self._sni = StatusNotifierItem(bus, _Relay(tray, self._on_sni))
        else:
            self._fall_back()

    @property
    def name(self) -> str:
        """The StatusNotifierItem's bus name ("" without a bus)."""
        return self._sni.name if self._sni is not None else ""

    @property
    def xembed(self) -> Any:
        """The XEmbed icon, once one was needed and X was there."""
        return self._x

    def update(self, state: menus.State) -> None:
        if self._sni is not None:
            self._sni.update(state)
        if self._x is not None:
            self._x.update(state)

    def balloon(self, _heading: str, _body: str) -> bool:
        return False  # the app sends the desktop a notification of its own

    def quitting(self) -> None:
        pass  # a Windows matter: GApplication keeps one Siphon per session on Linux

    def close(self) -> None:
        self._closed = True
        if self._sni is not None:
            self._sni.close()
        if self._x is not None:
            self._x.close()

    def _on_sni(self, shown: bool) -> None:
        if self._closed:
            return
        self._sni_shown = shown
        if shown:
            if self._x is not None:
                self._x.set_enabled(False)
        else:
            self._fall_back()
        self._publish()

    def _fall_back(self) -> None:
        if self._x is None and not self._x_tried:
            self._x_tried = True
            self._x = self._start_xembed(_Relay(self._tray, self._on_x))
        if self._x is not None:
            self._x.set_enabled(True)

    def _on_x(self, shown: bool) -> None:
        if not self._closed:
            self._x_shown = shown
            self._publish()

    def _publish(self) -> None:
        self._tray.set_available(self._sni_shown or self._x_shown)
