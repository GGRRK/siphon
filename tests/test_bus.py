"""siphon/bus.py: objects published on the session bus answer calls without keeping each one in memory."""

import gc
import time
from types import SimpleNamespace

from gi.repository import Gio, GLib

from siphon.bus import export

XML = '<node><interface name="io.github.ggrrk.Test"><method name="Ping"/></interface></node>'


def connect(address: str) -> Gio.DBusConnection:
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    return Gio.DBusConnection.new_for_address_sync(address, flags, None, None)


def spin(done, seconds: float = 3.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)


def test_a_method_call_is_let_go_once_answered(bus):
    server, client = connect(bus), connect(bus)
    freed, replies = [], []

    def on_call(_bus, _sender, _path, _iface, _method, _args, invocation):
        invocation.weak_ref(lambda *_: freed.append(True))
        invocation.return_value(None)

    registration = export(server, "/t", Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], on_call, None, None)
    try:
        for _ in range(3):
            client.call(server.get_unique_name(), "/t", "io.github.ggrrk.Test", "Ping", None, None,
                        Gio.DBusCallFlags.NONE, 2000, None, lambda c, result: replies.append(c.call_finish(result)))
        spin(lambda: len(replies) == 3)
        gc.collect()
        spin(lambda: len(freed) == 3, 1.0)
        assert len(replies) == 3
        assert len(freed) == 3  # the deprecated register_object kept every one (about 1.5 KB a call)
    finally:
        server.unregister_object(registration)
        server.close_sync(None)
        client.close_sync(None)


def test_an_older_glib_publishes_through_register_object():
    calls = []
    old = SimpleNamespace(register_object=lambda *args: calls.append(("register_object", *args)) or 7)
    assert export(old, "/p", "iface", "call", "get", None) == 7
    new = SimpleNamespace(register_object=lambda *args: calls.append(("register_object", *args)) or 7,
                          register_object_with_closures2=lambda *args: calls.append(("closures2", *args)) or 8)
    assert export(new, "/p", "iface", "call", "get", "set") == 8
    assert calls == [("register_object", "/p", "iface", "call", "get", None),
                     ("closures2", "/p", "iface", "call", "get", "set")]
