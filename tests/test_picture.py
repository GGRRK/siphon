"""A playlist's own picture: any image the user picks becomes an upright, centred square of at most 1200 px, a JPEG
or a PNG (siphon/picture.py), kept beside the playlist; and the playlist page that sets, replaces and removes it.

tests/fixtures/pictures holds one 60 x 40 scene per format: blue sides a centred square crops away, and in the
square red at the top left, green elsewhere. turned.jpg stores it turned a quarter left, with an EXIF orientation
that turns it back."""

import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from test_hidden_window import display  # noqa: F401 (the fixture)
from siphon import picture
from siphon.picture import PictureError
from siphon.playlists import Playlists

from gi.repository import GdkPixbuf, GLib  # noqa: E402  (after siphon.picture, which pins GdkPixbuf 2.0)

ROOT = Path(__file__).resolve().parent.parent
PICTURES = ROOT / "tests" / "fixtures" / "pictures"
RED, GREEN = (255, 0, 0), (0, 160, 0)
FORMATS = ["scene.jpg", "scene.png", "scene.webp", "scene.gif", "scene.bmp", "scene.tiff", "scene.heic",
           "scene.avif"]


def decoded(data: bytes) -> tuple[str, GdkPixbuf.Pixbuf]:
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    return loader.get_format().get_name(), loader.get_pixbuf()


def pixel(pixbuf: GdkPixbuf.Pixbuf, x: int, y: int) -> tuple[int, ...]:
    pixels, channels = pixbuf.read_pixel_bytes().get_data(), pixbuf.get_n_channels()
    at = y * pixbuf.get_rowstride() + x * channels
    return tuple(pixels[at:at + channels])


def near(colour: tuple[int, ...], expected: tuple[int, int, int]) -> bool:
    """Within what lossy formats (JPEG, WebP, HEIC, AVIF at quality 95) do to a flat colour."""
    return all(abs(a - b) <= 48 for a, b in zip(colour, expected))


def assert_the_scene_squared(data: bytes, side: int = 40) -> GdkPixbuf.Pixbuf:
    _fmt, pixbuf = decoded(data)
    assert (pixbuf.get_width(), pixbuf.get_height()) == (side, side)
    q = side // 4  # the middle of each quarter: clear of the colours' edges, which lossy formats smear
    assert near(pixel(pixbuf, q, q), RED)
    for x, y in ((3 * q, q), (q, 3 * q), (3 * q, 3 * q), (1, 3 * q), (side - 2, 2 * q)):
        assert near(pixel(pixbuf, x, y), GREEN), (x, y, pixel(pixbuf, x, y))  # the blue sides are cropped away
    return pixbuf


def square_where_readable(name: str) -> bytes | None:
    """The scene's picture; None for an AVIF on Windows, which must then say it can't read it: the Windows build has
    no GdkPixbuf loader for AVIF, and its ffmpeg no software AV1 decoder (no dav1d)."""
    try:
        return picture.square(PICTURES / name)
    except PictureError as error:
        if name.endswith(".avif") and sys.platform == "win32" and str(error) == picture.UNREADABLE:
            return None
        raise


@pytest.mark.parametrize("name", FORMATS)
def test_every_common_format_becomes_a_centred_square(name):
    if (data := square_where_readable(name)) is not None:
        assert decoded(data)[0] == "jpeg"
        assert_the_scene_squared(data)


@pytest.fixture
def without_gdkpixbuf_loaders(monkeypatch):
    """As on Windows for WebP and HEIC: GdkPixbuf refuses the file, then decodes the PNG ffmpeg made of it."""
    decode, seen = picture._decode, []

    def refuse_the_file(data: bytes) -> GdkPixbuf.Pixbuf:
        seen.append("ffmpeg's PNG" if len(seen) % 2 else "the file")
        if seen[-1] == "the file":
            raise GLib.Error("no loader for this format")
        return decode(data)

    monkeypatch.setattr(picture, "_decode", refuse_the_file)
    return seen


@pytest.mark.parametrize("name", FORMATS + ["turned.jpg"])
def test_what_gdkpixbuf_cant_read_goes_through_ffmpeg(name, without_gdkpixbuf_loaders):
    if (data := square_where_readable(name)) is not None:
        assert_the_scene_squared(data)
        assert without_gdkpixbuf_loaders == ["the file", "ffmpeg's PNG"]


def test_a_photo_is_turned_upright_by_its_exif_orientation():
    assert_the_scene_squared(picture.square(PICTURES / "turned.jpg"))


def test_the_photos_metadata_is_left_behind():
    assert b"Camera Maker" in (PICTURES / "turned.jpg").read_bytes()
    data = picture.square(PICTURES / "turned.jpg")
    assert b"Exif" not in data and b"Camera Maker" not in data


def test_an_animation_keeps_its_first_frame():
    assert_the_scene_squared(picture.square(PICTURES / "scene.gif"))  # the second frame is all yellow


def test_see_through_parts_keep_a_png_and_an_unused_alpha_channel_makes_a_jpeg():
    fmt, pixbuf = decoded(picture.square(PICTURES / "see-through.png"))
    assert fmt == "png" and pixbuf.get_has_alpha()
    assert pixel(pixbuf, 10, 10) == (*RED, 255) and pixel(pixbuf, 30, 30)[3] == 0
    data = picture.square(PICTURES / "opaque-rgba.png")
    assert decoded(data)[0] == "jpeg"
    assert_the_scene_squared(data)


def test_a_big_picture_shrinks_to_1200_and_a_small_one_keeps_its_size(tmp_path):
    big = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 3000, 2000)
    big.fill(0x0000ffff)  # blue sides
    big.new_subpixbuf(500, 0, 2000, 2000).fill(0x00a000ff)  # the square
    big.new_subpixbuf(500, 0, 1000, 1000).fill(0xff0000ff)
    big.savev(str(tmp_path / "big.png"), "png", [], [])
    assert_the_scene_squared(picture.square(tmp_path / "big.png"), side=picture.SIZE)
    assert_the_scene_squared(picture.square(PICTURES / "scene.png"), side=40)  # never blown up


def test_an_images_bytes_work_like_its_file():
    assert_the_scene_squared(picture.square((PICTURES / "scene.png").read_bytes()))


@pytest.mark.parametrize("name", ["not-a-picture.jpg", "corrupt.png"])
def test_a_file_that_is_no_picture_says_so(name):
    with pytest.raises(PictureError, match="That file is not a picture Siphon can read."):
        picture.square(PICTURES / name)


def test_empty_missing_and_unreadable_bytes_say_so(tmp_path):
    (tmp_path / "empty.png").write_bytes(b"")
    with pytest.raises(PictureError, match="not a picture Siphon can read"):
        picture.square(tmp_path / "empty.png")
    with pytest.raises(PictureError, match="Could not read that file."):
        picture.square(tmp_path / "gone.jpg")
    with pytest.raises(PictureError, match="not a picture Siphon can read"):
        picture.square(b"<html>not a picture</html>")


def test_a_huge_file_is_refused_before_it_is_read(monkeypatch, tmp_path):
    monkeypatch.setattr(picture, "MAX_BYTES", 5 * 1024 * 1024)
    huge = tmp_path / "huge.jpg"
    with huge.open("wb") as file:
        file.truncate(6 * 1024 * 1024)
    monkeypatch.setattr(Path, "read_bytes", lambda _self: pytest.fail("read a file over the limit"))
    with pytest.raises(PictureError, match=r"That picture is too large \(6 MB; up to 5 MB\)."):
        picture.square(huge)


def test_too_many_pixels_are_refused_without_decoding_them_all(monkeypatch):
    monkeypatch.setattr(picture, "MAX_PIXELS", 2000)  # the scene has 2400
    with pytest.raises(PictureError, match=r"That picture is too large \(60 × 40; up to "):
        picture.square(PICTURES / "scene.png")


_BOMB = """
import sys
sys.path.insert(0, sys.argv[1])
if sys.platform != "win32":  # a computer without the 4.8 GB the bomb takes decoded
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
from pathlib import Path
from siphon import picture
try:
    picture.square(Path(sys.argv[2]))
except picture.PictureError as error:
    print(error)
print(len(picture.square(Path(sys.argv[3]))) > 0)  # and a picture still decodes within those 3 GB
"""


def test_a_picture_bomb_is_refused_from_its_header_before_it_is_decoded(tmp_path):
    """A 190 KB PNG of 40000 x 40000 (1.6 gigapixels of one colour): GdkPixbuf's glycin module (Linux) decoded all of
    it before size-prepared could refuse it, and with 3 GB to use Siphon ended ("memory allocation of 4800000000
    bytes failed"). On Windows only the refusal is checked: its GdkPixbuf loaders stop at size 0."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    side, packer = 40000, zlib.compressobj(9)
    rows = (b"\0" * (1 + side // 8)) * 1000  # a filter byte and 5000 bytes of black, 1 bit a pixel
    idat = b"".join(packer.compress(rows) for _ in range(side // 1000)) + packer.flush()
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 1, 0, 0, 0, 0))
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    done = subprocess.run([sys.executable, "-c", _BOMB, str(ROOT), str(bomb), str(PICTURES / "scene.jpg")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["That picture is too large (40000 × 40000; up to 64 megapixels).", "True"]


def test_ffmpeg_refuses_too_many_pixels_too(monkeypatch, without_gdkpixbuf_loaders):
    monkeypatch.setattr(picture, "MAX_PIXELS", 2000)
    monkeypatch.setattr(picture, "_check_header", lambda _data: None)  # as without glycin
    with pytest.raises(PictureError, match=r"That picture is too large \(60 × 40; up to "):
        picture.square(PICTURES / "scene.webp")


def test_without_ffmpeg_what_gdkpixbuf_cant_read_is_no_picture(monkeypatch, without_gdkpixbuf_loaders):
    def no_ffmpeg(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", no_ffmpeg)
    with pytest.raises(PictureError, match="not a picture Siphon can read"):
        picture.square(PICTURES / "scene.webp")


# -- kept beside the playlist


@pytest.fixture
def lists(tmp_path):
    (tmp_path / "Music" / "Playlists").mkdir(parents=True)
    return Playlists(tmp_path / "Music" / "Playlists")


def test_a_chosen_picture_is_kept_renamed_and_trashed_with_its_playlist(lists, monkeypatch):
    trashed = []
    monkeypatch.setattr("siphon.playlists.trash_file", trashed.append)
    pl = lists.create("Road Trip")
    assert lists.set_cover(pl, picture.square(PICTURES / "scene.webp"))
    assert pl.cover == lists.folder / "Road Trip.jpg"
    lists.rename(pl, "Long Drive")
    again = lists.all()[0]
    assert again.cover == lists.folder / "Long Drive.jpg" and again.cover.is_file()
    assert sorted(p.name for p in lists.folder.iterdir()) == ["Long Drive.jpg", "Long Drive.m3u8"]
    lists.delete(again)
    assert trashed == [lists.folder / "Long Drive.m3u8", lists.folder / "Long Drive.jpg"]


def test_a_see_through_picture_is_kept_as_png_and_replacing_it_drops_the_old_file(lists):
    pl = lists.create("Mix")
    lists.set_cover(pl, picture.square(PICTURES / "see-through.png"))
    assert pl.cover.name == "Mix.png"
    lists.set_cover(pl, picture.square(PICTURES / "scene.jpg"))
    assert sorted(p.name for p in lists.folder.iterdir()) == ["Mix.jpg", "Mix.m3u8"]


def test_removing_the_picture_forgets_it_and_deletes_the_file_siphon_kept(lists):
    pl = lists.create("Mix")
    lists.set_cover(pl, picture.square(PICTURES / "scene.jpg"))
    lists.remove_cover(pl)
    assert pl.cover is None and lists.all()[0].cover is None
    assert "#EXTIMG" not in pl.file.read_text()
    assert sorted(p.name for p in lists.folder.iterdir()) == ["Mix.m3u8"]
    lists.remove_cover(pl)  # nothing left to remove: fine


def test_removing_another_players_picture_only_stops_naming_it(lists, tmp_path):
    art = tmp_path / "Music" / "Art" / "cover.png"
    art.parent.mkdir()
    art.write_bytes((PICTURES / "scene.png").read_bytes())
    (lists.folder / "Theirs.m3u8").write_text("#EXTM3U\n#EXTIMG:../Art/cover.png\n")
    pl = lists.all()[0]
    assert pl.cover == art
    lists.remove_cover(pl)
    assert lists.all()[0].cover is None and art.is_file()


# -- the playlist page


_PAGE = """
import json, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from pathlib import Path
import siphon.ui
from siphon import picture
from siphon.playlists import Playlists
from siphon.ui.art import Cover, CoverArt
from siphon.ui.music import Music
from siphon.ui.playlists_page import PlaylistsPage
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk
import fake_library as fake, fake_player

ROOT, pictures, root = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

def run_until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False) or time.sleep(0.01)
    return condition()

Adw.init()
toasts = []
def toast(message, button=None, on_button=None):
    toasts.append((message, button, on_button))
    return Adw.Toast(title=message)

(root / "Playlists").mkdir(parents=True)
music = Music(fake.Library(root), Playlists, fake_player.Player(), lambda path: None, fake.Song)
page = PlaylistsPage(music, CoverArt(lambda path: None), toast)
window = Adw.Window(content=page, default_width=900, default_height=700)
window.present()
playlist = music.change_playlists(music.playlists.create, "Road Trip")
page.open(playlist)
view = page._detail
run_until(view.get_mapped)
seen = {}
remove = view._actions.lookup_action("remove-picture")
change = view._actions.lookup_action("change-picture")

def covers(widget):
    child = widget.get_first_child()
    while child is not None:
        yield from ([child] if isinstance(child, Cover) else covers(child))
        child = child.get_next_sibling()

def state():
    pl = music.find_playlist(view._playlist.file)
    row_cover = next(covers(page._rows.get_row_at_index(0)))
    return {"cover": pl.cover and pl.cover.name, "remove shown": remove.get_enabled(),
            "header shows it": pl.cover is not None and view._cover._shown[1] == pl.cover,
            "row shows it": pl.cover is not None and row_cover._shown[1] == pl.cover,
            "files": sorted(p.name for p in (root / "Playlists").iterdir())}

def pick(source):
    view._set_picture(source)
    busy = not change.get_enabled()  # the spinner shows only after 250 ms: most pictures are ready before
    run_until(lambda: not view._working)
    run_until(lambda: view._cover._image.get_paintable() is not None)
    return busy

seen["new"] = state()
seen["picked busy"] = pick(pictures / "scene.webp")
seen["picked"] = state()
seen["header painted"] = not view._cover.has_css_class("placeholder")
view._show_working(True)
seen["spinner at once"] = view._busy.get_visible()
run_until(view._busy.get_visible, seconds=1)
seen["spinner after 250 ms"] = view._busy.get_visible()
view._show_working(False)
seen["spinner when done"] = view._busy.get_visible(), change.get_enabled()
seen["toasts after a first pick"] = [t[0] for t in toasts]

pick(pictures / "see-through.png")
seen["replaced"] = state()
seen["replace toast"] = toasts[-1][:2]
toasts[-1][2]()  # Undo
seen["undone"] = state()

view._actions.activate_action("remove-picture", None)
seen["removed"] = state()
seen["remove toast"] = toasts[-1][:2]
seen["header back to placeholder"] = view._cover.has_css_class("placeholder")
toasts[-1][2]()
seen["remove undone"] = state()

before = len(toasts)
pick(pictures / "not-a-picture.jpg")
seen["not a picture"] = [t[0] for t in toasts[before:]], state()["cover"], change.get_enabled()
square = picture.square
picture.square = lambda _source: 1 / 0  # a bug, not a bad file
before = len(toasts)
pick(pictures / "scene.png")
seen["a bug"] = [t[0] for t in toasts[before:]], change.get_enabled()
picture.square = square

def copy(gtype, value):
    view.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(GObject.Value(gtype, value)))

def set_by(act, sets=True):  # from a playlist without a picture: whether act took the key or drop, what it set
    view._actions.activate_action("remove-picture", None)
    taken = act()
    run_until(lambda: state()["cover"] is not None, seconds=5 if sets else 0.5)
    return taken, state()["cover"]

copy(str, "https://example.com/a-link")
seen["paste text"] = set_by(lambda: view._on_paste(view, None), sets=False)
copy(Gdk.Texture, Gdk.Texture.new_from_filename(str(pictures / "see-through.png")))
seen["paste picture"] = set_by(lambda: view._on_paste(view, None))
copy(Gdk.FileList, Gdk.FileList.new_from_list([Gio.File.new_for_path(str(pictures / "scene.jpg"))]))
seen["paste file"] = set_by(lambda: view._on_paste(view, None))
copy(Gdk.FileList, Gdk.FileList.new_from_list([Gio.File.new_for_path(str(ROOT / "README.md"))]))
seen["paste another file"] = set_by(lambda: view._on_paste(view, None), sets=False)
files = Gdk.FileList.new_from_list([Gio.File.new_for_path(str(pictures / "scene.gif"))])
seen["drop"] = set_by(lambda: view._on_picture_drop(None, files, 0, 0))
dialog = Adw.AlertDialog(heading="Delete?")
dialog.present(window)
run_until(lambda: window.get_visible_dialog() is not None)
copy(Gdk.Texture, Gdk.Texture.new_from_filename(str(pictures / "scene.png")))
seen["paste under a dialog"] = view._on_paste(view, None)
dialog.force_close()

music.change_playlists(music.playlists.rename, view._playlist, "Long Drive")  # as Rename… does
seen["renamed"] = state()
page.nav.pop()
run_until(lambda: page._detail is None)
fresh = Playlists(root / "Playlists").all()[0]
seen["after a restart"] = fresh.name, fresh.cover and fresh.cover.name
print(json.dumps(seen))
"""


def test_the_page_sets_replaces_removes_and_pastes_the_picture(display, tmp_path):
    done = subprocess.run([sys.executable, "-c", _PAGE, str(ROOT), str(PICTURES), str(tmp_path / "Music")],
                          env=display | {"SIPHON_FAKE_PLAYLISTS": "0"}, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    assert seen["new"] == {"cover": None, "remove shown": False, "header shows it": False, "row shows it": False,
                           "files": ["Road Trip.m3u8"]}
    assert seen["picked busy"]
    assert seen["picked"] == {"cover": "Road Trip.jpg", "remove shown": True, "header shows it": True,
                              "row shows it": True, "files": ["Road Trip.jpg", "Road Trip.m3u8"]}
    assert seen["header painted"]
    assert (seen["spinner at once"], seen["spinner after 250 ms"], seen["spinner when done"]) == (False, True,
                                                                                                [False, True])
    assert seen["toasts after a first pick"] == []  # nothing replaced: nothing to undo
    assert seen["replaced"]["cover"] == "Road Trip.png" and seen["replaced"]["files"] == ["Road Trip.m3u8",
                                                                                         "Road Trip.png"]
    assert seen["replace toast"] == ["Changed the picture of “Road Trip”", "Undo"]
    assert seen["undone"]["cover"] == "Road Trip.jpg"
    assert seen["removed"] == {"cover": None, "remove shown": False, "header shows it": False,
                               "row shows it": False, "files": ["Road Trip.m3u8"]}
    assert seen["remove toast"] == ["Removed the picture of “Road Trip”", "Undo"]
    assert seen["header back to placeholder"]
    assert seen["remove undone"]["cover"] == "Road Trip.jpg"
    assert seen["not a picture"] == [["That file is not a picture Siphon can read."], "Road Trip.jpg", True]
    assert seen["a bug"] == [["Could not use that picture."], True]  # and the page takes pictures again
    assert seen["paste text"] == [False, None]  # a link on the clipboard is left alone
    assert seen["paste picture"] == [True, "Road Trip.png"]  # an image copied in a browser, see-through
    assert seen["paste file"] == [True, "Road Trip.jpg"]  # a picture's file copied in a file manager
    assert seen["paste another file"] == [True, None]  # a song's file, say: no picture, and no complaint
    assert seen["drop"] == [True, "Road Trip.jpg"]
    assert seen["paste under a dialog"] is False
    assert seen["renamed"]["cover"] == "Long Drive.jpg" and seen["renamed"]["header shows it"]
    assert seen["after a restart"] == ["Long Drive", "Long Drive.jpg"]
