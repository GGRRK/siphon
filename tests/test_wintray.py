"""The Windows tray and single instance, checked anywhere: the structures against their documented 64-bit layout,
the declared signatures, and the logic over a fake user32/shell32 whose message queue runs the real window
procedure (a ctypes callback) on the tray's own thread, as Windows would."""

import ctypes
import json
import queue
import sys
import threading
import time
from ctypes import POINTER, sizeof
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import GLib, GObject

import siphon.__main__ as entry
from siphon import tray, wintray
from siphon.wintray import (COPYDATASTRUCT, GUID, HWND, LPARAM, LRESULT, MSG, NOTIFYICONDATAW, POINT, UINT, WNDCLASSEXW,
                            WPARAM, Win32)

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning:gi.events")  # MainContext.iteration()
x64 = pytest.mark.skipif(sizeof(ctypes.c_void_p) != 8, reason="the documented sizes are 64-bit Windows'")
HWND_TRAY = 0x1234


@pytest.fixture(autouse=True)
def no_single_instance_yet(monkeypatch):
    """hand_over's module state (the mutex, whether this Siphon owns it) starts afresh in every test."""
    for name, value in (("_api", None), ("_mutex", None), ("_owned", False)):
        monkeypatch.setattr(wintray, name, value)


def pump(until, timeout: float = 3.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "timed out"
        context.iteration(False) or time.sleep(0.005)


# ---------------------------------------------------------------- layout and signatures


@x64
def test_structures_have_the_documented_64_bit_sizes():
    assert [sizeof(s) for s in (NOTIFYICONDATAW, WNDCLASSEXW, MSG, COPYDATASTRUCT, GUID, POINT)] == [
        976, 80, 48, 24, 16, 8]


@x64
def test_notifyicondata_offsets():
    offsets = {name: getattr(NOTIFYICONDATAW, name).offset for name in (
        "hWnd", "uID", "uFlags", "uCallbackMessage", "hIcon", "szTip", "dwState", "szInfo", "uVersion",
        "szInfoTitle", "dwInfoFlags", "guidItem", "hBalloonIcon")}
    assert offsets == {"hWnd": 8, "uID": 16, "uFlags": 20, "uCallbackMessage": 24, "hIcon": 32, "szTip": 40,
                       "dwState": 296, "szInfo": 304, "uVersion": 816, "szInfoTitle": 820, "dwInfoFlags": 948,
                       "guidItem": 952, "hBalloonIcon": 968}
    assert (MSG.wParam.offset, MSG.lParam.offset, MSG.pt.offset) == (16, 24, 36)


class FakeFunction:
    def __init__(self, name):
        self.name, self.restype, self.argtypes = name, "unset", "unset"


class FakeDll:
    def __init__(self, name):
        self.name, self.functions = name, {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, FakeFunction(name))

def linger(seconds: float = 0.2) -> None:
    """Run the main loop a while: for checks that something did not happen."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)



def test_every_call_is_declared_and_pointer_sized_where_windows_is():
    dlls = {}
    api = Win32(lambda name: dlls.setdefault(name, FakeDll(name)))
    functions = {f.name: f for dll in dlls.values() for f in dll.functions.values()}
    assert len(functions) == 30 and all(f.restype != "unset" and f.argtypes != "unset" for f in functions.values())
    pointer = sizeof(ctypes.c_void_p)
    assert sizeof(HWND) == sizeof(WPARAM) == sizeof(LPARAM) == sizeof(LRESULT) == pointer
    assert (api.DefWindowProcW.restype, tuple(api.DefWindowProcW.argtypes)) == (LRESULT, (HWND, UINT, WPARAM, LPARAM))
    assert tuple(api.PostMessageW.argtypes) == (HWND, UINT, WPARAM, LPARAM)
    assert api.SendMessageTimeoutW.restype is LRESULT
    assert api.Shell_NotifyIconW.argtypes[1] is POINTER(NOTIFYICONDATAW)
    assert api.LoadImageW.restype is wintray.HANDLE and api.CreateWindowExW.restype is HWND
    assert "Shell_NotifyIconW" in dlls["shell32"].functions and "CreateMutexW" in dlls["kernel32"].functions
    proto = wintray.WNDPROC
    assert proto._restype_ is LRESULT and proto._argtypes_ == (HWND, UINT, WPARAM, LPARAM)


def test_text_fields_are_utf16_cut_to_fit():
    data = NOTIFYICONDATAW()
    wintray.put(data.szTip, "Siphon\nSöng – 🎵")
    assert wintray.text(data.szTip) == "Siphon\nSöng – 🎵"
    wintray.put(data.szInfoTitle, "x" * 100)
    assert wintray.text(data.szInfoTitle) == "x" * 63  # 64 units with the NUL
    wintray.put(data.szInfoTitle, "y" * 62 + "🎵")  # the emoji's two units do not fit: never half of it
    assert wintray.text(data.szInfoTitle) == "y" * 62


def test_the_tip_names_the_song():
    assert wintray.tip(tray.State()) == "Siphon"
    assert wintray.tip(tray.State(song="Song – Artist")) == "Siphon\nSong – Artist"
    assert len(wintray.tip(tray.State(song="z" * 300))) == 127


def test_the_icon_file_ships_and_is_found():
    assert wintray.icon_file() == ROOT / "packaging" / "windows" / "siphon.ico"  # from source
    assert wintray.icon_file().is_file()
    spec = (ROOT / "packaging" / "windows" / "siphon.spec").read_text()
    assert '(str(HERE / "siphon.ico"), "data")' in spec  # frozen: <bundle>/bin/data/siphon.ico


# ---------------------------------------------------------------- the icon over a fake Windows


class FakeApi:
    """user32, shell32 and kernel32 as far as NotifyIcon and hand_over use them. PostMessageW queues; GetMessageW
    blocks on the queue and DispatchMessageW calls the registered window procedure, all on the calling thread."""

    WM_QUIT = 0x0012

    def __init__(self):
        self.calls = []
        self.icons = []  # the notification area: NIM_ADD adds, NIM_DELETE removes
        self.queue = queue.Queue()
        self.wndproc = None
        self.add_ok = True
        self.chosen = 0
        self.error = 0
        self.windows = {}
        self.reenter = None  # a message the next Shell_NotifyIconW delivers to the window while it waits
        self.menus_drop_left = False  # SM_MENUDROPALIGNMENT: a left-handed user's setting

    def last_error(self):
        return self.error

    def GetModuleHandleW(self, name):
        return 0x400000

    def RegisterWindowMessageW(self, name):
        self.calls.append(("RegisterWindowMessageW", name))
        return 0xC0DE

    def RegisterClassExW(self, pointer):
        window_class = pointer._obj
        self.wndproc = window_class.lpfnWndProc
        self.calls.append(("RegisterClassExW", window_class.lpszClassName, window_class.cbSize))
        return 1

    def UnregisterClassW(self, name, instance):
        self.calls.append(("UnregisterClassW", name))
        return 1

    def CreateWindowExW(self, ex_style, class_name, title, style, x, y, w, h, parent, menu, instance, param):
        self.calls.append(("CreateWindowExW", ex_style, class_name, title, style, parent))
        self.windows[class_name] = HWND_TRAY
        return HWND_TRAY

    def ChangeWindowMessageFilterEx(self, hwnd, message, action, change):
        self.calls.append(("ChangeWindowMessageFilterEx", hwnd, message, action, change))
        return 1

    def DestroyWindow(self, hwnd):
        self.calls.append(("DestroyWindow", hwnd))
        self.windows.clear()
        self.send(hwnd, wintray.WM_DESTROY, 0, 0)
        return 1

    def DefWindowProcW(self, hwnd, message, wparam, lparam):
        self.calls.append(("DefWindowProcW", message))
        return 0

    def GetSystemMetrics(self, index):
        return {wintray.SM_CXSMICON: 16, wintray.SM_CYSMICON: 16, wintray.SM_CXICON: 32, wintray.SM_CYICON: 32,
                wintray.SM_MENUDROPALIGNMENT: int(self.menus_drop_left)}[index]

    def LoadImageW(self, instance, path, kind, cx, cy, flags):
        self.calls.append(("LoadImageW", Path(path).name, cx, flags))
        return 0x1C00 + cx

    def DestroyIcon(self, icon):
        self.calls.append(("DestroyIcon", icon))
        return 1

    def Shell_NotifyIconW(self, message, pointer):
        data = pointer._obj
        if self.reenter is not None:  # as Windows does while the call waits on Explorer's reply
            sent, self.reenter = self.reenter, None
            self.send(data.hWnd, sent, 0, 0)
        record = SimpleNamespace(message=message, flags=data.uFlags, id=data.uID, hwnd=data.hWnd,
                                 callback=data.uCallbackMessage, icon=data.hIcon, tip=wintray.text(data.szTip),
                                 info=wintray.text(data.szInfo), title=wintray.text(data.szInfoTitle),
                                 info_flags=data.dwInfoFlags, balloon_icon=data.hBalloonIcon, version=data.uVersion,
                                 size=data.cbSize)
        self.calls.append(("Shell_NotifyIconW", record))
        if message == wintray.NIM_ADD:
            if not self.add_ok or data.uID in self.icons:
                return 0
            self.icons.append(data.uID)
        elif message == wintray.NIM_DELETE:
            if data.uID not in self.icons:
                return 0
            self.icons.remove(data.uID)
        elif message == wintray.NIM_MODIFY and data.uID not in self.icons:
            return 0
        return 1

    def PostMessageW(self, hwnd, message, wparam, lparam):
        self.calls.append(("PostMessageW", message))
        self.queue.put((hwnd, message, wparam, lparam))
        return 1

    def PostQuitMessage(self, code):
        self.queue.put((None, self.WM_QUIT, 0, 0))

    def GetMessageW(self, pointer, hwnd, low, high):
        message = pointer._obj
        message.hwnd, message.message, message.wParam, message.lParam = self.queue.get(timeout=10)
        return 0 if message.message == self.WM_QUIT else 1

    def TranslateMessage(self, pointer):
        return 0

    def DispatchMessageW(self, pointer):
        message = pointer._obj
        return self.send(message.hwnd, message.message, message.wParam, message.lParam)

    def send(self, hwnd, message, wparam, lparam):
        """What SendMessage does within the window's own thread: call its procedure."""
        return self.wndproc(hwnd, message, wparam, lparam)

    def CreatePopupMenu(self):
        self.menu = []
        return 0x3E

    def AppendMenuW(self, menu, flags, item_id, label):
        self.menu.append((flags, item_id, label))
        return 1

    def SetMenuDefaultItem(self, menu, item, by_position):
        self.calls.append(("SetMenuDefaultItem", item))
        return 1

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        return 1

    def TrackPopupMenu(self, menu, flags, x, y, reserved, hwnd, rect):
        self.calls.append(("TrackPopupMenu", flags, x, y))
        return self.chosen

    def DestroyMenu(self, menu):
        self.calls.append(("DestroyMenu", menu))
        return 1

    def notify_calls(self):
        return [record for name, record, *_ in self.calls if name == "Shell_NotifyIconW"]


class Player(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
    state, current = "stopped", None

    def has_next(self):
        return False


@pytest.fixture
def icon(capfd):
    """A Tray over a NotifyIcon on a fake Windows, its thread running; what the tray emitted is recorded."""
    api = FakeApi()
    player = Player()
    made = SimpleNamespace(api=api, player=player, heard=[], capfd=capfd)
    made.tray = tray.Tray(player, lambda owner: wintray.NotifyIcon(owner, api=api))
    made.backend = made.tray._backend
    for signal in ("command", "open", "scroll"):
        made.tray.connect(signal, lambda _t, value: made.heard.append(value))
    made.tray.connect("session-end", lambda _t: made.heard.append("session-end"))
    pump(lambda: made.tray.available)
    yield made
    made.tray.close()


def deliver(icon, message, wparam=0, lparam=0):
    """A message to the tray window, handled on its own thread as Windows would, then idle calls run here."""
    icon.api.PostMessageW(HWND_TRAY, message, wparam, lparam)


def test_the_icon_is_added_on_a_hidden_top_level_window(icon):
    api = icon.api
    created = next(c for c in api.calls if c[0] == "CreateWindowExW")
    # a top-level window with no parent (not HWND_MESSAGE, -3), never shown, kept off the taskbar
    assert created == ("CreateWindowExW", wintray.WS_EX_TOOLWINDOW, wintray.CLASS_NAME, "Siphon Tray", 0, None)
    assert ("RegisterClassExW", wintray.CLASS_NAME, sizeof(WNDCLASSEXW)) in api.calls
    assert ("RegisterWindowMessageW", "TaskbarCreated") in api.calls
    # let in from Explorer even when Siphon runs elevated
    assert ("ChangeWindowMessageFilterEx", HWND_TRAY, 0xC0DE, wintray.MSGFLT_ALLOW, None) in api.calls
    assert ("LoadImageW", "siphon.ico", 16, wintray.LR_LOADFROMFILE) in api.calls
    add, version = api.notify_calls()[:2]
    assert (add.message, add.hwnd, add.id, add.callback, add.icon, add.tip, add.size) == (
        wintray.NIM_ADD, HWND_TRAY, 1, wintray.WM_APP + 1, 0x1C10, "Siphon", sizeof(NOTIFYICONDATAW))
    assert add.flags == wintray.NIF_MESSAGE | wintray.NIF_ICON | wintray.NIF_TIP | wintray.NIF_SHOWTIP
    assert (version.message, version.version) == (wintray.NIM_SETVERSION, 4)
    assert api.icons == [1]
    assert "siphon: tray icon added" in icon.capfd.readouterr().err


@pytest.mark.parametrize("event, command", [
    (wintray.NIN_SELECT, "show"), (wintray.NIN_KEYSELECT, "show"), (wintray.NIN_BALLOONUSERCLICK, "show"),
    (wintray.WM_MBUTTONUP, "play-pause")])
def test_clicks_on_the_icon(icon, event, command):
    deliver(icon, wintray.WM_APP + 1, 0, (1 << 16) | event)  # version 4: the icon's id in the high word
    pump(lambda: icon.heard)
    assert icon.heard == [command]


def test_the_menu_follows_the_player_and_runs_the_choice(icon):
    api = icon.api
    icon.player.current = SimpleNamespace(title="Rock & Roll", artist="Band")
    icon.player.state = "playing"
    icon.player.emit("changed")
    api.chosen = tray.IDS[tray.QUIT]
    deliver(icon, wintray.WM_APP + 1, (300 << 16) | (0xFFFF & -20), (1 << 16) | wintray.WM_CONTEXTMENU)
    pump(lambda: icon.heard)
    assert icon.heard == ["quit"]
    grayed = wintray.MF_STRING | wintray.MF_GRAYED
    assert api.menu == [(0, 1, "Show Siphon"), (grayed, 2, "Rock && Roll – Band"), (wintray.MF_SEPARATOR, 0, None),
                        (0, 4, "Pause"), (grayed, 5, "Next"), (0, 6, "Previous"), (wintray.MF_SEPARATOR, 0, None),
                        (0, 8, "Quit Siphon")]
    names = [c[0] for c in api.calls]
    track = names.index("TrackPopupMenu")
    assert names[track - 1] == "SetForegroundWindow" and ("SetMenuDefaultItem", 1) in api.calls
    assert api.calls[track] == ("TrackPopupMenu", wintray.TPM_RIGHTBUTTON | wintray.TPM_RETURNCMD |
                                wintray.TPM_NONOTIFY, -20, 300)  # the anchor from wParam, signed
    assert api.calls[track + 1] == ("PostMessageW", wintray.WM_NULL) and names[track + 2] == "DestroyMenu"


def test_the_menu_drops_to_the_side_the_user_set(icon):
    icon.api.menus_drop_left = True  # right-aligned to the click, as Windows's own menus then are
    deliver(icon, wintray.WM_APP + 1, 0, wintray.WM_CONTEXTMENU)
    pump(lambda: any(c[0] == "TrackPopupMenu" for c in icon.api.calls))
    track = next(c for c in icon.api.calls if c[0] == "TrackPopupMenu")
    assert track[1] == wintray.TPM_RIGHTBUTTON | wintray.TPM_RETURNCMD | wintray.TPM_NONOTIFY | wintray.TPM_RIGHTALIGN


def test_a_dismissed_menu_runs_nothing(icon):
    deliver(icon, wintray.WM_APP + 1, 0, wintray.WM_CONTEXTMENU)
    pump(lambda: any(c[0] == "DestroyMenu" for c in icon.api.calls))
    linger()
    assert icon.heard == []


def test_a_posted_command_is_the_menus(icon):
    deliver(icon, wintray.WM_COMMAND, tray.IDS[tray.QUIT])
    deliver(icon, wintray.WM_COMMAND, 999)
    pump(lambda: icon.heard)
    linger()
    assert icon.heard == ["quit"]


def test_the_tip_follows_the_song(icon):
    icon.player.current = SimpleNamespace(title="Song", artist="")
    icon.player.emit("changed")
    pump(lambda: any(r.message == wintray.NIM_MODIFY for r in icon.api.notify_calls()))
    modify = [r for r in icon.api.notify_calls() if r.message == wintray.NIM_MODIFY][-1]
    assert (modify.flags, modify.tip) == (wintray.NIF_TIP | wintray.NIF_SHOWTIP, "Siphon\nSong")


def test_the_balloon(icon):
    assert icon.tray.balloon("Siphon Is Still Running", "It keeps playing.")
    pump(lambda: any(r.flags & wintray.NIF_INFO for r in icon.api.notify_calls()))
    balloon = [r for r in icon.api.notify_calls() if r.flags & wintray.NIF_INFO][-1]
    assert balloon.flags == wintray.NIF_INFO | wintray.NIF_SHOWTIP  # the tip keeps working after it
    assert (balloon.message, balloon.title, balloon.info) == (wintray.NIM_MODIFY, "Siphon Is Still Running",
                                                              "It keeps playing.")
    assert (balloon.info_flags, balloon.balloon_icon) == (wintray.NIIF_USER | wintray.NIIF_LARGE_ICON, 0x1C20)
    pump(lambda: "tray balloon shown" in icon.capfd.readouterr().err)


def test_explorer_restarting_brings_the_icon_back(icon):
    icon.api.icons.clear()  # Explorer died and took the icon with it
    deliver(icon, 0xC0DE)  # TaskbarCreated
    pump(lambda: icon.api.icons == [1])
    assert icon.tray.available


def test_explorer_restarting_during_a_tray_call_does_not_deadlock(icon):
    icon.api.reenter = 0xC0DE  # TaskbarCreated, delivered inside the NIM_MODIFY that changes the tip
    icon.player.current = SimpleNamespace(title="Song", artist="")
    icon.player.emit("changed")
    pump(lambda: [r.message for r in icon.api.notify_calls()].count(wintray.NIM_SETVERSION) == 2)
    assert icon.api.icons == [1] and icon.tray.available


def test_no_notification_area_means_no_tray(capfd):
    api = FakeApi()
    api.add_ok = False
    api.error = 1460
    owner = tray.Tray(Player(), lambda o: wintray.NotifyIcon(o, api=api))
    pump(lambda: "no tray icon" in capfd.readouterr().err)
    assert not owner.available
    ops = [r.message for r in api.notify_calls()]
    assert ops == [wintray.NIM_ADD, wintray.NIM_MODIFY]  # an icon left over from before would do too
    owner.close()


def test_a_second_siphons_links_arrive_through_wm_copydata(icon):
    received = []

    def send_timeout(hwnd, message, wparam, lparam, flags, timeout, result):
        # SendMessageTimeoutW: another process's call, handled by the tray window's procedure
        received.append((hwnd, message, flags))
        result._obj.value = icon.backend._handle(message, wparam, lparam)
        return 1

    other = SimpleNamespace(CreateMutexW=lambda *a: 0x77, last_error=lambda: wintray.ERROR_ALREADY_EXISTS,
                            WaitForSingleObject=lambda handle, ms: WAIT_TIMEOUT,
                            FindWindowW=lambda cls, title: icon.api.windows.get(cls, 0),
                            GetWindowThreadProcessId=lambda hwnd, pid: setattr(pid._obj, "value", 4321) or 99,
                            AllowSetForegroundWindow=lambda pid: received.append(("allow", pid)) or 1,
                            SendMessageTimeoutW=send_timeout)
    args = ["https://youtu.be/x", "ünïcode"]
    assert wintray.hand_over(args, api=other)
    assert received[0] == ("allow", 4321)
    assert received[1] == (HWND_TRAY, wintray.WM_COPYDATA, wintray.SMTO_ABORTIFHUNG)
    pump(lambda: icon.heard)
    assert icon.heard == [args]


def copydata(kind: int, payload: bytes) -> tuple[COPYDATASTRUCT, object]:
    buffer = ctypes.create_string_buffer(payload, max(len(payload), 1))
    return COPYDATASTRUCT(kind, len(payload), ctypes.cast(buffer, ctypes.c_void_p)), buffer


@pytest.mark.parametrize("kind, payload", [(0x1234, b'["x"]'), (wintray.COPYDATA_ARGS, b"not json"),
                                           (wintray.COPYDATA_ARGS, b'{"a": 1}'), (wintray.COPYDATA_ARGS, b"[1]")])
def test_other_copydata_is_refused(icon, kind, payload):
    data, _buffer = copydata(kind, payload)
    assert icon.backend._handle(wintray.WM_COPYDATA, 0, ctypes.addressof(data)) == 0


def test_copydata_without_its_structure_is_refused(icon):
    assert icon.backend._handle(wintray.WM_COPYDATA, 0, 0) == 0


def test_an_empty_command_line_just_shows_the_window(icon):
    data, _buffer = copydata(wintray.COPYDATA_ARGS, b"")
    assert icon.backend._handle(wintray.WM_COPYDATA, 0, ctypes.addressof(data)) == 1
    pump(lambda: icon.heard)
    assert icon.heard == [[]]


WAIT_TIMEOUT = 0x102


def test_the_first_siphon_keeps_going_and_lets_go_when_it_quits():
    released = []
    first = SimpleNamespace(CreateMutexW=lambda attrs, owned, name: 0x77 if owned and name == wintray.MUTEX_NAME
                            else 0, last_error=lambda: 0, ReleaseMutex=lambda handle: released.append(handle) or 1)
    assert not wintray.hand_over(["x"], api=first)
    assert wintray._owned  # its main thread owns the mutex: one that starts later waits on it
    wintray.release()
    wintray.release()  # once only
    assert released == [0x77] and not wintray._owned


def test_a_second_siphon_runs_by_itself_when_the_first_quits_while_it_waits():
    waits = iter([WAIT_TIMEOUT, WAIT_TIMEOUT, wintray.WAIT_ABANDONED])  # the first ended without letting go
    second = SimpleNamespace(CreateMutexW=lambda *a: 0x77, last_error=lambda: wintray.ERROR_ALREADY_EXISTS,
                             WaitForSingleObject=lambda handle, ms: next(waits), FindWindowW=lambda cls, title: 0)
    started = time.monotonic()
    assert not wintray.hand_over(["x"], api=second, wait=5)
    assert time.monotonic() - started < 1 and wintray._owned


def test_a_quitting_siphon_refuses_links_and_the_second_one_runs_by_itself(icon):
    released, sent = [], []
    wintray._api, wintray._mutex, wintray._owned = SimpleNamespace(ReleaseMutex=released.append), 0x77, True
    held = [True]

    def send_timeout(hwnd, message, wparam, lparam, flags, timeout, result):
        sent.append(message)
        result._obj.value = icon.backend._handle(message, wparam, lparam)
        return 1

    second = SimpleNamespace(CreateMutexW=lambda *a: 0x78, last_error=lambda: wintray.ERROR_ALREADY_EXISTS,
                             WaitForSingleObject=lambda handle, ms: WAIT_TIMEOUT if held[0] else wintray.WAIT_OBJECT_0,
                             FindWindowW=lambda cls, title: icon.api.windows.get(cls, 0),
                             GetWindowThreadProcessId=lambda hwnd, pid: 99, AllowSetForegroundWindow=lambda pid: 1,
                             SendMessageTimeoutW=send_timeout)
    icon.tray.quitting()  # the first's main loop is over: what it would take now would be dropped
    assert released == [0x77] and not wintray._owned
    # the first's window still takes the message, and refuses it; then the second one gets the mutex
    threading.Timer(0.25, lambda: held.__setitem__(0, False)).start()
    assert not wintray.hand_over(["https://youtu.be/x"], api=second, wait=5)
    assert sent and set(sent) == {wintray.WM_COPYDATA} and wintray._owned
    linger()
    assert icon.heard == []


def test_a_second_siphon_that_cannot_ask_runs_by_itself():
    """SendMessageTimeout fails: the first hangs, or runs as administrator (UIPI)."""
    second = SimpleNamespace(CreateMutexW=lambda *a: 0x77, last_error=lambda: wintray.ERROR_ALREADY_EXISTS,
                             WaitForSingleObject=lambda handle, ms: WAIT_TIMEOUT, FindWindowW=lambda cls, title: 0x99,
                             GetWindowThreadProcessId=lambda hwnd, pid: 99, AllowSetForegroundWindow=lambda pid: 1,
                             SendMessageTimeoutW=lambda *a: 0)
    assert wintray.hand_over(["x"], api=second, wait=5) is False and not wintray._owned


def test_a_first_siphon_that_never_shows_its_window_is_not_waited_for_forever(capfd):
    stuck = SimpleNamespace(CreateMutexW=lambda *a: 0x77, last_error=lambda: wintray.ERROR_ALREADY_EXISTS,
                            WaitForSingleObject=lambda handle, ms: WAIT_TIMEOUT, FindWindowW=lambda cls, title: 0)
    started = time.monotonic()
    assert not wintray.hand_over([], api=stuck, wait=0.3)
    assert 0.3 <= time.monotonic() - started < 2 and "another Siphon" in capfd.readouterr().err


def test_the_session_ending_quits_siphon_before_windows_goes_on(icon):
    """WM_QUERYENDSESSION marks it at once (no installer then); WM_ENDSESSION waits until Siphon has shut down."""
    backend = icon.backend
    assert backend._handle(wintray.WM_QUERYENDSESSION, 0, 0) == 1 and icon.tray.session_ending
    assert backend._handle(wintray.WM_ENDSESSION, 0, 0) == 0 and not icon.tray.session_ending  # called off
    returned = []
    ending = threading.Thread(target=lambda: returned.append(backend._handle(wintray.WM_ENDSESSION, 1, 0)))
    icon.tray.connect("session-end", lambda owner: owner.close())  # as the app's do_shutdown does, in the end
    started = time.monotonic()
    ending.start()
    pump(lambda: returned, timeout=6)
    assert returned == [0] and "session-end" in icon.heard and time.monotonic() - started < 3
    assert icon.tray.session_ending
    pump(lambda: icon.api.icons == [])


def test_quitting_removes_the_icon_and_ends_the_thread(icon):
    thread = icon.backend._thread
    icon.tray.close()
    assert not thread.is_alive()
    assert icon.api.icons == [] and ("DestroyWindow", HWND_TRAY) in icon.api.calls
    assert ("UnregisterClassW", wintray.CLASS_NAME) in icon.api.calls
    assert {c[1] for c in icon.api.calls if c[0] == "DestroyIcon"} == {0x1C10, 0x1C20}
    assert "siphon: tray icon removed" in icon.capfd.readouterr().err
    icon.tray.close()  # and again, from atexit: nothing more


def test_the_icon_goes_even_when_the_thread_is_stuck(capfd):
    api = FakeApi()
    owner = tray.Tray(Player(), lambda o: wintray.NotifyIcon(o, api=api, start=False))
    backend = owner._backend
    backend._hwnd = HWND_TRAY
    backend._add()
    assert api.icons == [1]
    owner.close()  # no thread to post to: removed from here
    assert api.icons == []


def test_an_exception_in_the_window_procedure_stays_in_python(icon, monkeypatch):
    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(icon.backend, "_handle", boom)
    assert icon.backend._on_message(HWND_TRAY, 0x1234, 0, 0) == 0
    assert ("DefWindowProcW", 0x1234) in icon.api.calls
    assert "RuntimeError: bug" in icon.capfd.readouterr().err


# ---------------------------------------------------------------- the entry point


def test_a_second_windows_siphon_exits_before_loading_gtk_or_touching_the_log(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["Siphon.exe", "https://youtu.be/x"])
    monkeypatch.setattr(sys, "stderr", None)  # the windowed build
    monkeypatch.setattr("siphon.logfile.redirect", lambda: pytest.fail("the running Siphon's log was emptied"))
    monkeypatch.setattr(entry, "hold_checkout", lambda: None)
    monkeypatch.setattr(entry, "setup_environment", lambda: None)
    handed = []
    monkeypatch.setattr(wintray, "hand_over", lambda args: handed.append(args) or True)
    monkeypatch.setitem(sys.modules, "siphon.app", None)  # importing it would fail the test
    assert entry.run() == 0 and handed == [["https://youtu.be/x"]]


def test_linux_never_takes_the_windows_lock(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["siphon"])
    monkeypatch.setattr(entry, "hold_checkout", lambda: None)
    monkeypatch.setattr(entry, "setup_environment", lambda: None)
    monkeypatch.setattr(wintray, "hand_over", lambda args: pytest.fail("hand_over on Linux"))
    fake_app = SimpleNamespace(main=lambda argv: 7)
    monkeypatch.setitem(sys.modules, "siphon.app", fake_app)
    assert entry.run() == 7


def test_copydata_is_the_documented_json():
    data, _buffer = copydata(wintray.COPYDATA_ARGS, json.dumps(["a"]).encode())
    assert (data.dwData, ctypes.string_at(data.lpData, data.cbData)) == (0x53495048, b'["a"]')


@pytest.mark.parametrize("command", entry.COMMANDS)
def test_the_command_line_tools_run_alongside_a_running_siphon(monkeypatch, command):
    monkeypatch.setattr(entry, "windows", lambda: True)
    monkeypatch.setattr(sys, "argv", ["siphon-cli.exe", command])
    monkeypatch.setattr(wintray, "hand_over", lambda args: pytest.fail("a tool handed over to the window"))
    monkeypatch.setattr(entry, "hold_checkout", lambda: None)
    monkeypatch.setattr(entry, "setup_environment", lambda: None)
    for module, name in (("siphon.cli", "main"), ("siphon.selftest", "main"), ("siphon.updater", "cli")):
        monkeypatch.setattr(f"{module}.{name}", lambda argv: 0)
    monkeypatch.setattr("siphon.core.engine_version", lambda: "test")
    assert entry.run() == 0
