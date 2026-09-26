"""The two small questions the music pages ask: a playlist name, and "are you sure?"."""

from collections.abc import Callable

from gi.repository import Adw, Gtk


def ask_name(parent: Gtk.Widget, heading: str, confirm: str, done: Callable[[str], None], initial: str = "") -> None:
    """Asks for a playlist name; `done` gets the trimmed name, never an empty one."""
    entry = Gtk.Entry(text=initial, activates_default=True, placeholder_text="Playlist name")
    entry.update_property([Gtk.AccessibleProperty.LABEL], ["Playlist name"])
    dialog = Adw.AlertDialog(heading=heading, extra_child=entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", confirm)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def sync(*_args) -> None:
        dialog.set_response_enabled("ok", bool(entry.get_text().strip()))

    def respond(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "ok" and entry.get_text().strip():
            done(entry.get_text().strip())

    entry.connect("changed", sync)
    dialog.connect("response", respond)
    sync()
    dialog.present(parent)
    entry.grab_focus()
    entry.select_region(0, -1)


def confirm(parent: Gtk.Widget, heading: str, body: str, action: str, done: Callable[[], None]) -> None:
    """A destructive yes/no; Cancel is the default so Enter never destroys anything."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: done() if response == "ok" else None)
    dialog.present(parent)
