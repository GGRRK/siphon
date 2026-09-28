"""The Format row: each format's sound on a quality meter and what it gets you (core.FORMAT_QUALITY, FORMAT_NOTES),
on the row and in its list, and the words screen readers get.

GTK needs a display, so the row itself is checked in a child process against a private gtk4-broadwayd (the display
fixture of test_hidden_window.py): nothing reaches the desktop."""

import json
import subprocess
import sys
from pathlib import Path

from siphon import core
from siphon.ui.format_row import spoken
from test_hidden_window import display  # noqa: F401 (the fixture)

ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = ROOT / "siphon" / "ui" / "style.css"


def test_screen_readers_hear_the_sound_then_the_note():
    assert spoken(core, "flac") == "Best sound. Sounds like Original, no better, in files often 10 times bigger."
    assert spoken(core, "opus") == "Excellent sound. As YouTube sends it, converted from others; not for Apple Music."


def test_every_level_has_its_colour_in_the_stylesheet():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for blocks in {blocks for blocks, _word in core.FORMAT_QUALITY.values()}:
        assert f".quality.level-{blocks} levelbar > trough > block.filled {{" in css


_ROW = """
import gc, json, sys, time, weakref
sys.path.insert(0, sys.argv[1])
from siphon import core
from siphon.app import _STYLE
from siphon.ui.format_row import FormatRow, FormatView, QualityMeter
from gi.repository import Adw, Gdk, GLib, Gtk

def run_until(condition):
    end = time.monotonic() + 10
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

def walk(widget, kind):
    found, child = [widget] if isinstance(widget, kind) else [], widget.get_first_child()
    while child:
        found += walk(child, kind)
        child = child.get_next_sibling()
    return found

def view(v):
    meter = walk(v, QualityMeter)[0]
    return {"name": v.name.get_label(), "blocks": meter.bar.get_value(), "of": meter.bar.get_max_value(),
            "word": meter.word.get_label(), "level": [c for c in meter.get_css_classes() if c.startswith("level-")],
            "note": v.note.get_label() if v.note.get_visible() else None,
            "check": v.check.get_opacity() if v.check.get_visible() else None}

Adw.init()
css = Gtk.CssProvider()
css.load_from_path(str(_STYLE))
Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
row = FormatRow(core, "mp3")
made = []  # a weak reference to every view the row sets up for itself
row.get_factory().connect("setup", lambda _factory, item: made.append(weakref.ref(item.get_child())))
heard = []  # each list item's words for screen readers, read once the row's own bind has set them
row.get_list_factory().connect("bind", lambda _factory, item: heard.append(
    [item.get_accessible_label(), item.get_accessible_description()]))
rows = Gtk.ListBox(css_classes=["boxed-list"])
rows.append(row)
window = Gtk.Window(child=rows, default_width=820)
window.present()
seen = {"shown": run_until(row.get_mapped), "format": row.format, "subtitle": row.get_subtitle(),
        "classes": row.get_css_classes()}
seen["on the row"] = [view(v) for v in walk(row, FormatView) if v.get_ancestor(Gtk.Popover) is None]
row.activate()
popover = next(p for p in walk(row, Gtk.Popover))
seen["opened"] = run_until(lambda: popover.get_mapped() and len(walk(popover, FormatView)) == len(core.FORMATS))
views = walk(popover, FormatView)
seen["listed"] = [view(v) for v in views]
seen["heard"] = heard[:]
seen["scrolls"] = walk(popover, Gtk.ScrolledWindow)[0].get_vadjustment().get_upper() > \\
    walk(popover, Gtk.ScrolledWindow)[0].get_vadjustment().get_page_size() + 0.5
popover.popdown()
for _round in range(10):  # chosen again and again
    for n in range(len(core.FORMATS)):
        row.set_selected(n)
        run_until(lambda: row.get_subtitle() == core.FORMAT_NOTES[core.FORMATS[n]])
gc.collect()
seen["row views"] = sum(1 for view in made if view() is not None)  # left alive: not one more with every choice
# what screen readers get for the row: its own description (the meter's word, then the note), not its subtitle's
seen["described"] = {"by subtitle": Gtk.test_accessible_has_relation(row, Gtk.AccessibleRelation.DESCRIBED_BY),
                     "own": Gtk.test_accessible_has_property(row, Gtk.AccessibleProperty.DESCRIPTION)}
narrow = Gtk.ListBox(css_classes=["boxed-list"], margin_start=12, margin_end=12)
narrow_row = FormatRow(core, "m4a")  # the widest name and word beside the note
narrow.append(narrow_row)
narrow_window = Gtk.Window(child=narrow, default_width=360)  # the window's narrowest
narrow_window.present()
run_until(lambda: narrow_row.get_mapped() and any(v.get_width() for v in walk(narrow_row, FormatView)))
chosen = [v for v in walk(narrow_row, FormatView) if v.get_mapped()][0]
seen["narrowest"] = {"word": chosen.meter.word.get_mapped() and chosen.meter.word.get_label(),
                     "cut": [label.get_label() for label in walk(narrow_row, Gtk.Label) if label.get_mapped()
                             and label.get_layout().is_ellipsized()]}
row.set_selected(core.FORMATS.index("flac"))
run_until(lambda: row.get_subtitle() == core.FORMAT_NOTES["flac"])
seen["after choosing flac"] = {"format": row.format, "subtitle": row.get_subtitle(),
                               "on the row": [view(v) for v in walk(row, FormatView)
                                              if v.get_ancestor(Gtk.Popover) is None],
                               "checks": [v.check.get_opacity() for v in views]}
print(json.dumps(seen))
"""


def test_the_row_and_its_list_show_every_formats_sound_and_note(display):  # noqa: F811
    # GTK's test accessibility backend: it keeps what the row tells screen readers, on no bus (GTK_A11Y=none keeps none)
    done = subprocess.run([sys.executable, "-c", _ROW, str(ROOT)], env={**display, "GTK_A11Y": "test"},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    assert seen["shown"] and seen["opened"]
    assert "combo" in seen["classes"] and "format" in seen["classes"]  # the row keeps libadwaita's own style
    assert seen["format"] == "mp3" and seen["subtitle"] == core.FORMAT_NOTES["mp3"]
    assert seen["on the row"] == [{"name": "MP3", "blocks": 3, "of": core.QUALITY_BLOCKS, "word": "Excellent",
                                   "level": ["level-3"], "note": None, "check": None}]
    assert seen["listed"] == [
        {"name": core.FORMAT_LABELS[fmt], "blocks": core.FORMAT_QUALITY[fmt][0], "of": core.QUALITY_BLOCKS,
         "word": core.FORMAT_QUALITY[fmt][1], "level": [f"level-{core.FORMAT_QUALITY[fmt][0]}"],
         "note": core.FORMAT_NOTES[fmt], "check": 1.0 if fmt == "mp3" else 0.0} for fmt in core.FORMATS]
    assert seen["heard"] == [[core.FORMAT_LABELS[fmt], spoken(core, fmt)] for fmt in core.FORMATS]
    assert not seen["scrolls"]  # all five compare at a glance
    assert seen["row views"] <= 2
    assert seen["described"] == {"by subtitle": False, "own": True}
    # the narrowest window: the row's meter keeps its word (not colours alone), and the note wraps, never cut
    assert seen["narrowest"] == {"word": "Excellent", "cut": []}
    after = seen["after choosing flac"]
    assert after["format"] == "flac" and after["subtitle"] == core.FORMAT_NOTES["flac"]
    assert [(v["name"], v["blocks"], v["word"]) for v in after["on the row"]] == [("FLAC", 4, "Best")]
    assert after["checks"] == [1.0 if fmt == "flac" else 0.0 for fmt in core.FORMATS]
