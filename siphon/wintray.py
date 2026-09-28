"""Windows: Siphon's icon in the notification area, and one Siphon per session. ctypes only.

A hidden top-level window runs its own message loop on a thread of its own; not a message-only window, which
would miss what Windows broadcasts: TaskbarCreated (Explorer restarted, so the icon must be added again) and the
end of the session. The window owns the icon and its menu and hands every command to the GTK main loop with
GLib.idle_add. It is also how a second Siphon finds the first: hand_over() sends it the second one's command line
with WM_COPYDATA, since GLib's own single instance needs a D-Bus session bus that Windows does not have.
"""

import atexit
import ctypes
import json
import sys
import threading
import time
import traceback
from ctypes import POINTER, Structure, byref, c_int, c_int32, c_size_t, c_ssize_t, c_ubyte, c_uint, c_uint16, sizeof
from ctypes import c_uint32, c_void_p, c_wchar_p
from pathlib import Path

from gi.repository import GLib

from . import tray as menus

# Windows' types, pointer-sized where theirs are: the structures lay out as on 64-bit Windows wherever this loads.
BOOL = c_int
UINT = c_uint
DWORD = c_uint32
WORD = c_uint16
LONG = c_int32
ATOM = WORD
HANDLE = HWND = HICON = HMENU = HINSTANCE = HCURSOR = HBRUSH = c_void_p
WPARAM = UINT_PTR = ULONG_PTR = DWORD_PTR = c_size_t
LPARAM = LRESULT = c_ssize_t
WCHAR = c_uint16  # a UTF-16 code unit; ctypes' own c_wchar is 4 bytes outside Windows
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(LRESULT, HWND, UINT, WPARAM, LPARAM)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_COPYDATA = 0x004A
WM_CONTEXTMENU = 0x007B
WM_COMMAND = 0x0111
WM_MBUTTONUP = 0x0208
WM_USER = 0x0400
WM_APP = 0x8000
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
NOTIFYICON_VERSION_4 = 4
NIIF_USER, NIIF_LARGE_ICON = 0x04, 0x20
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
WS_EX_TOOLWINDOW = 0x80
SMTO_ABORTIFHUNG = 0x2
ERROR_ALREADY_EXISTS = 183

CLASS_NAME = "io.github.ggrrk.Siphon.Tray"  # the smoke test and a second Siphon find the window by it
MUTEX_NAME = "Local\\io.github.ggrrk.Siphon"  # Local: one Siphon per session, not per machine
COPYDATA_ARGS = 0x53495048  # "SIPH": WM_COPYDATA from a second Siphon, its arguments as a JSON list in UTF-8
_CALLBACK, _UPDATE, _BALLOON, _CLOSE = WM_APP + 1, WM_APP + 2, WM_APP + 3, WM_APP + 4
_ICON_ID = 1
_END_WAIT = 4.0  # seconds an ending session waits for Siphon to finish; Windows allows about five
_MAX_ARGS = 1 << 20


class GUID(Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", c_ubyte * 8)]


class NOTIFYICONDATAW(Structure):
    _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT), ("uFlags", UINT), ("uCallbackMessage", UINT),
                ("hIcon", HICON), ("szTip", WCHAR * 128), ("dwState", DWORD), ("dwStateMask", DWORD),
                ("szInfo", WCHAR * 256), ("uVersion", UINT),  # a union with uTimeout, unused since Vista
                ("szInfoTitle", WCHAR * 64), ("dwInfoFlags", DWORD), ("guidItem", GUID), ("hBalloonIcon", HICON)]


class WNDCLASSEXW(Structure):
    _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int),
                ("cbWndExtra", c_int), ("hInstance", HINSTANCE), ("hIcon", HICON), ("hCursor", HCURSOR),
                ("hbrBackground", HBRUSH), ("lpszMenuName", c_wchar_p), ("lpszClassName", c_wchar_p),
                ("hIconSm", HICON)]


class POINT(Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MSG(Structure):
    _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM), ("time", DWORD),
                ("pt", POINT), ("lPrivate", DWORD)]


class COPYDATASTRUCT(Structure):
    _fields_ = [("dwData", ULONG_PTR), ("cbData", DWORD), ("lpData", c_void_p)]


class Win32:
    """The calls this module makes, declared: handles and message parameters are 64 bits wide on 64-bit Windows,
    and a call without argtypes and restype would pass and return them as 32-bit ints."""

    def __init__(self, load=None) -> None:
        load = load or (lambda name: ctypes.WinDLL(name, use_last_error=True))
        kernel32, user32, shell32 = load("kernel32"), load("user32"), load("shell32")
        for dll, name, restype, *argtypes in (
                (kernel32, "GetModuleHandleW", HINSTANCE, c_wchar_p),
                (kernel32, "CreateMutexW", HANDLE, c_void_p, BOOL, c_wchar_p),
                (user32, "RegisterClassExW", ATOM, POINTER(WNDCLASSEXW)),
                (user32, "UnregisterClassW", BOOL, c_wchar_p, HINSTANCE),
                (user32, "CreateWindowExW", HWND, DWORD, c_wchar_p, c_wchar_p, DWORD, c_int, c_int, c_int, c_int,
                 HWND, HMENU, HINSTANCE, c_void_p),
                (user32, "DestroyWindow", BOOL, HWND),
                (user32, "DefWindowProcW", LRESULT, HWND, UINT, WPARAM, LPARAM),
                (user32, "GetMessageW", BOOL, POINTER(MSG), HWND, UINT, UINT),
                (user32, "TranslateMessage", BOOL, POINTER(MSG)),
                (user32, "DispatchMessageW", LRESULT, POINTER(MSG)),
                (user32, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM),
                (user32, "PostQuitMessage", None, c_int),
                (user32, "SendMessageTimeoutW", LRESULT, HWND, UINT, WPARAM, LPARAM, UINT, UINT, POINTER(DWORD_PTR)),
                (user32, "RegisterWindowMessageW", UINT, c_wchar_p),
                (user32, "FindWindowW", HWND, c_wchar_p, c_wchar_p),
                (user32, "GetWindowThreadProcessId", DWORD, HWND, POINTER(DWORD)),
                (user32, "AllowSetForegroundWindow", BOOL, DWORD),
                (user32, "SetForegroundWindow", BOOL, HWND),
                (user32, "CreatePopupMenu", HMENU),
                (user32, "AppendMenuW", BOOL, HMENU, UINT, UINT_PTR, c_wchar_p),
                (user32, "SetMenuDefaultItem", BOOL, HMENU, UINT, UINT),
                (user32, "TrackPopupMenu", BOOL, HMENU, UINT, c_int, c_int, c_int, HWND, c_void_p),
                (user32, "DestroyMenu", BOOL, HMENU),
                (user32, "GetSystemMetrics", c_int, c_int),
                (user32, "LoadImageW", HANDLE, HINSTANCE, c_wchar_p, UINT, c_int, c_int, UINT),
                (user32, "DestroyIcon", BOOL, HICON),
                (shell32, "Shell_NotifyIconW", BOOL, DWORD, POINTER(NOTIFYICONDATAW))):
            function = getattr(dll, name)
            function.restype, function.argtypes = restype, argtypes
            setattr(self, name, function)

    @staticmethod
    def last_error() -> int:
        return ctypes.get_last_error()


def icon_file() -> Path:
    """siphon.ico: in the Windows build's data folder, or in packaging/windows of a source checkout."""
    root = Path(__file__).resolve().parent.parent
    bundled = root / "data" / "siphon.ico"
    return bundled if bundled.is_file() else root / "packaging" / "windows" / "siphon.ico"


def put(field, text: str) -> None:
    """Copy text into a fixed WCHAR array of a structure, cut to fit before its terminating NUL."""
    units = text.encode("utf-16-le")[:(len(field) - 1) * 2]
    if units and 0xD800 <= int.from_bytes(units[-2:], "little") <= 0xDBFF:
        units = units[:-2]  # never half of a surrogate pair
    ctypes.memmove(field, units, len(units))
    field[len(units) // 2] = 0


def text(field) -> str:
    """A WCHAR array's text, up to its NUL."""
    return bytes(field).decode("utf-16-le").split("\0", 1)[0]


def tip(state: menus.State) -> str:
    return menus.shorten(f"Siphon\n{state.song}" if state.song else "Siphon", 127)


def _signed_word(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _log(message: str) -> None:
    # stderr: siphon.log in the windowed build, which the smoke test reads
    print(f"siphon: {message}", file=sys.stderr, flush=True)


_mutex = None  # held for as long as this Siphon runs


def hand_over(args: list[str], api: Win32 | None = None, wait: float = 10.0) -> bool:
    """Run first. True when another Siphon runs in this session and took args (it shows its window and queues
    the links): this one exits. False when this is the only one, or the other never answers."""
    global _mutex
    api = api or Win32()
    _mutex = api.CreateMutexW(None, False, MUTEX_NAME)
    if not _mutex or api.last_error() != ERROR_ALREADY_EXISTS:
        return False
    deadline = time.monotonic() + wait  # the other may still be starting: its window comes a moment later
    while not (window := api.FindWindowW(CLASS_NAME, None)):
        if time.monotonic() >= deadline:
            _log("another Siphon is starting or stuck; opening a window of this one's own")
            return False
        time.sleep(0.1)
    pid = DWORD()
    api.GetWindowThreadProcessId(window, byref(pid))
    api.AllowSetForegroundWindow(pid.value)  # the user started this one: the other may come to the front
    payload = json.dumps(args).encode("utf-8")
    buffer = ctypes.create_string_buffer(payload, max(len(payload), 1))
    data = COPYDATASTRUCT(COPYDATA_ARGS, len(payload), ctypes.cast(buffer, c_void_p))
    answer = DWORD_PTR()
    sent = api.SendMessageTimeoutW(window, WM_COPYDATA, 0, ctypes.addressof(data), SMTO_ABORTIFHUNG, 5000,
                                   byref(answer))
    return bool(sent) and answer.value == 1


class NotifyIcon:
    """The icon, its hidden window and the thread that runs them. Everything that touches the tray runs on that
    thread; the main thread only posts to it, and hears back through GLib.idle_add."""

    def __init__(self, tray: menus.Tray, api: Win32 | None = None, icon: Path | None = None,
                 start: bool = True) -> None:
        self._tray = tray
        self._api = api or Win32()
        self._icon_path = icon or icon_file()
        self._state = tray.state  # replaced whole by the main thread, read by the tray's
        self._message = ("", "")  # the balloon's heading and text
        self._hwnd = None
        self._icon = self._balloon_icon = None
        self._taskbar_created = 0
        self._added = False
        self._lock = threading.Lock()
        self._ended = threading.Event()  # the main loop is done: a session that ends may go on
        self._closing = False
        self.session_ending = False  # set by the tray's thread the moment Windows asks
        self._wndproc = WNDPROC(self._on_message)  # referenced for as long as the window lives
        self._ready = threading.Event()  # the window exists, or could not be made
        self._thread = threading.Thread(target=self._run, name="siphon-tray", daemon=True)
        if start:
            self._thread.start()
            self._ready.wait(5)
            atexit.register(self.close)  # the icon goes on every way out of Python, not only a clean quit

    # -- from the main thread

    def update(self, state: menus.State) -> None:
        self._state = state
        self._post(_UPDATE)

    def balloon(self, heading: str, body: str) -> bool:
        self._message = (heading, body)
        self._post(_BALLOON)
        return self._hwnd is not None

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._ended.set()  # a WM_ENDSESSION waiting for the main loop returns now
        if self._hwnd and self._thread.is_alive():
            self._post(_CLOSE)
            self._thread.join(2)
        self._remove()  # when the thread could not

    def _post(self, message: int) -> None:
        if self._hwnd:
            self._api.PostMessageW(self._hwnd, message, 0, 0)

    # -- the tray's thread

    def _run(self) -> None:
        try:
            self._create()
        except OSError as exc:
            _log(f"no tray icon: {exc}")
            return
        finally:
            self._ready.set()
        msg = MSG()
        while self._api.GetMessageW(byref(msg), None, 0, 0) > 0:
            self._api.TranslateMessage(byref(msg))
            self._api.DispatchMessageW(byref(msg))
        self._destroyed()

    def _create(self) -> None:
        api = self._api
        instance = api.GetModuleHandleW(None)
        self._taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")
        window_class = WNDCLASSEXW(cbSize=sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=instance,
                                   lpszClassName=CLASS_NAME)
        if not api.RegisterClassExW(byref(window_class)):
            raise OSError(f"RegisterClassExW failed with error {api.last_error()}")
        # never shown; a tool window stays out of the taskbar and Alt+Tab even so
        hwnd = api.CreateWindowExW(WS_EX_TOOLWINDOW, CLASS_NAME, "Siphon Tray", 0, 0, 0, 0, 0, None, None,
                                   instance, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW failed with error {api.last_error()}")
        self._hwnd = hwnd
        path = str(self._icon_path)
        # the .ico holds 16 to 256 px: Windows picks the size the tray and the balloon need at this DPI
        self._icon = api.LoadImageW(None, path, IMAGE_ICON, api.GetSystemMetrics(SM_CXSMICON),
                                    api.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)
        self._balloon_icon = api.LoadImageW(None, path, IMAGE_ICON, api.GetSystemMetrics(SM_CXICON),
                                            api.GetSystemMetrics(SM_CYICON), LR_LOADFROMFILE)
        if not self._icon:
            _log(f"the tray icon has no picture: {path} did not load (error {api.last_error()})")
        self._add()

    def _data(self, flags: int = 0) -> NOTIFYICONDATAW:
        return NOTIFYICONDATAW(cbSize=sizeof(NOTIFYICONDATAW), hWnd=self._hwnd, uID=_ICON_ID, uFlags=flags)

    def _add(self) -> None:
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        data.uCallbackMessage, data.hIcon = _CALLBACK, self._icon
        put(data.szTip, tip(self._state))
        api = self._api
        with self._lock:
            # TaskbarCreated can come while the icon is still there (a DPI change): then it only needs updating
            added = bool(api.Shell_NotifyIconW(NIM_ADD, byref(data)) or api.Shell_NotifyIconW(NIM_MODIFY, byref(data)))
            if added:
                data.uVersion = NOTIFYICON_VERSION_4  # NIN_SELECT, WM_CONTEXTMENU, and the click's position
                api.Shell_NotifyIconW(NIM_SETVERSION, byref(data))
            self._added = added
        _log("tray icon added" if added else f"no tray icon: Shell_NotifyIcon failed with error {api.last_error()}")
        GLib.idle_add(self._tray.set_available, added)

    def _remove(self) -> None:
        with self._lock:
            if not self._added:
                return
            self._added = False
            removed = self._api.Shell_NotifyIconW(NIM_DELETE, byref(self._data()))
        _log("tray icon removed" if removed else "the tray icon was already gone")

    def _destroyed(self) -> None:
        for icon in (self._icon, self._balloon_icon):
            if icon:
                self._api.DestroyIcon(icon)
        self._icon = self._balloon_icon = None
        self._api.UnregisterClassW(CLASS_NAME, self._api.GetModuleHandleW(None))

    def _on_message(self, hwnd, message: int, wparam: int, lparam: int) -> int:
        try:
            result = self._handle(message, wparam or 0, lparam or 0)
        except Exception:  # an exception must not cross back into Windows
            traceback.print_exc()
            result = None
        return self._api.DefWindowProcW(hwnd, message, wparam, lparam) if result is None else result

    def _handle(self, message: int, wparam: int, lparam: int) -> int | None:
        """A message's result, or None for DefWindowProc's."""
        api = self._api
        if message == _CALLBACK:
            event = lparam & 0xFFFF  # version 4: the event in the low word, the position in wparam
            if event in (NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK):
                self._command(menus.IDS[menus.SHOW])
            elif event == WM_MBUTTONUP:
                self._command(menus.IDS[menus.PLAY_PAUSE])
            elif event == WM_CONTEXTMENU:
                self._menu(_signed_word(wparam), _signed_word(wparam >> 16))
            return 0
        if message == WM_COMMAND:  # from the smoke test; the menu itself returns its choice (TPM_RETURNCMD)
            self._command(wparam & 0xFFFF)
            return 0
        if message == WM_COPYDATA:
            return self._copydata(lparam)
        if message == _UPDATE:
            with self._lock:
                if self._added:
                    data = self._data(NIF_TIP | NIF_SHOWTIP)
                    put(data.szTip, tip(self._state))
                    api.Shell_NotifyIconW(NIM_MODIFY, byref(data))
            return 0
        if message == _BALLOON:
            self._show_balloon()
            return 0
        if message == _CLOSE:
            self._remove()
            api.DestroyWindow(self._hwnd)
            return 0
        if message == WM_DESTROY:
            api.PostQuitMessage(0)
            return 0
        if message == WM_QUERYENDSESSION:  # before any window hears that the session ends: no installer then
            self.session_ending = True
            return 1
        if message == WM_ENDSESSION:
            if not wparam:  # called off
                self.session_ending = False
                return 0
            # Windows may end the process once this returns: let Siphon save and stop first
            self.session_ending = True
            GLib.idle_add(self._tray.end_session)
            self._ended.wait(_END_WAIT)
            return 0
        if message == self._taskbar_created and self._taskbar_created:
            self._add()
            return 0
        return None

    def _command(self, number: int) -> None:
        command = menus.COMMANDS.get(number)
        if command:
            GLib.idle_add(self._tray.run, command)

    def _copydata(self, lparam: int) -> int:
        data = COPYDATASTRUCT.from_address(lparam)
        if data.dwData != COPYDATA_ARGS or data.cbData > _MAX_ARGS:
            return 0
        raw = ctypes.string_at(data.lpData, data.cbData) if data.lpData and data.cbData else b"[]"
        try:
            args = json.loads(raw.decode("utf-8"))
        except ValueError:
            return 0
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return 0
        GLib.idle_add(self._tray.open, args)
        return 1

    def _menu(self, x: int, y: int) -> None:
        api = self._api
        handle = api.CreatePopupMenu()
        if not handle:
            return
        try:
            for item in menus.menu(self._state):
                if item.separator:
                    api.AppendMenuW(handle, MF_SEPARATOR, 0, None)
                else:  # & marks a mnemonic: a song's own ones are doubled
                    api.AppendMenuW(handle, MF_STRING if item.enabled else MF_STRING | MF_GRAYED, item.id,
                                    item.label.replace("&", "&&"))
            api.SetMenuDefaultItem(handle, menus.IDS[menus.SHOW], 0)  # bold: what a click on the icon does
            # the foreground first, or the menu would stay open after a click elsewhere; WM_NULL after, or a
            # second right click would close it at once (both as the documentation of TrackPopupMenu says)
            api.SetForegroundWindow(self._hwnd)
            chosen = api.TrackPopupMenu(handle, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, x, y, 0,
                                        self._hwnd, None)
            api.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            api.DestroyMenu(handle)
        self._command(chosen)

    def _show_balloon(self) -> None:
        heading, body = self._message
        with self._lock:
            if not self._added:
                return
            data = self._data(NIF_INFO)
            put(data.szInfoTitle, heading)
            put(data.szInfo, body)
            data.dwInfoFlags = NIIF_USER | (NIIF_LARGE_ICON if self._balloon_icon else 0)
            data.hBalloonIcon = self._balloon_icon
            shown = self._api.Shell_NotifyIconW(NIM_MODIFY, byref(data))
        _log("tray balloon shown" if shown else f"no tray balloon: error {self._api.last_error()}")
