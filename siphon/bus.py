"""Linux: publishing an object on the session bus, for MPRIS and the tray icon."""

from collections.abc import Callable

from gi.repository import Gio


def export(bus: Gio.DBusConnection, path: str, interface: Gio.DBusInterfaceInfo, on_call: Callable,
           on_get: Callable, on_set: Callable | None) -> int:
    """Serve `interface` at `path`; the registration id, for bus.unregister_object.

    GLib 2.84 and newer get register_object_with_closures2. The register_object that Python sees
    (register_object_with_closures, deprecated since 2.84) hands each method call's invocation to the binding with a
    reference Python never drops, so every call a bar or media key made stayed in memory (measured: about 1.5 KB per
    call, 2026-09-28, GLib 2.88). An older GLib has only that one.
    """
    register = getattr(bus, "register_object_with_closures2", None) or bus.register_object
    return register(path, interface, on_call, on_get, on_set)
