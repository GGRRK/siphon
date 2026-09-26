import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from siphon import covers, paths
from siphon.covers import cover_file

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("media")
    files = {}
    for name in ("cover.jpg", "cover.png", "back.png"):
        color = "blue" if name.startswith("back") else "red"
        ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=8x8", "-frames:v", "1", str(out / name))
        files[name] = out / name
    files["cover.gif"] = out / "cover.gif"  # written by hand: the Windows build's ffmpeg has no GIF encoder
    files["cover.gif"].write_bytes(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"))
    for ext in ("mp3", "m4a", "flac", "opus", "ogg", "wav"):
        codec = ["-c:a", "libvorbis"] if ext == "ogg" else []
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ac", "1", *codec,
               str(out / f"tone.{ext}"))
        files[ext] = out / f"tone.{ext}"
    return files


def copy(media: dict[str, Path], ext: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"song.{ext}"
    shutil.copy2(media[ext], path)
    return path


def picture(kind: int, mime: str, data: bytes) -> Picture:
    pic = Picture()
    pic.type, pic.mime, pic.data = kind, mime, data
    return pic


def embed(path: Path, pictures: list[tuple[int, str, bytes]]) -> None:
    """pictures: (picture type, mime, bytes), written the way each format stores covers."""
    ext = path.suffix[1:]
    apics = [APIC(encoding=3, mime=mime, type=kind, desc=str(kind), data=data)
             for kind, mime, data in pictures]
    if ext == "mp3":
        tags = ID3()
        for frame in apics:
            tags.add(frame)
        tags.save(path)
    elif ext == "wav":
        audio = WAVE(path)
        audio.add_tags()
        for frame in apics:
            audio.tags.add(frame)
        audio.save()
    elif ext == "m4a":
        audio = MP4(path)
        audio["covr"] = [MP4Cover(data, MP4Cover.FORMAT_PNG if mime.endswith("png")
                                  else MP4Cover.FORMAT_JPEG) for _, mime, data in pictures]
        audio.save()
    elif ext == "flac":
        audio = FLAC(path)
        for p in pictures:
            audio.add_picture(picture(*p))
        audio.save()
    else:
        audio = OggOpus(path) if ext == "opus" else OggVorbis(path)
        audio["metadata_block_picture"] = [base64.b64encode(picture(*p).write()).decode()
                                           for p in pictures]
        audio.save()


@pytest.mark.parametrize("ext,image", [("mp3", "cover.jpg"), ("wav", "cover.png"),
                                       ("m4a", "cover.png"), ("flac", "cover.jpg"),
                                       ("opus", "cover.jpg"), ("ogg", "cover.png")])
def test_extracts_the_embedded_cover(tmp_path, media, ext, image):
    data = media[image].read_bytes()
    path = copy(media, ext, tmp_path / "Music")
    embed(path, [(3, "image/" + image[-3:].replace("jpg", "jpeg"), data)])
    out = cover_file(path)
    assert out is not None and out.suffix == "." + image[-3:]
    assert out.read_bytes() == data
    assert out.parent == paths.cache_dir() / "covers"


@pytest.mark.parametrize("ext", ["mp3", "flac", "opus"])
def test_prefers_the_front_cover(tmp_path, media, ext):
    back, front = media["back.png"].read_bytes(), media["cover.jpg"].read_bytes()
    path = copy(media, ext, tmp_path / "Music")
    embed(path, [(4, "image/png", back), (3, "image/jpeg", front)])
    assert cover_file(path).read_bytes() == front


def test_falls_back_to_the_first_picture(tmp_path, media):
    path = copy(media, "flac", tmp_path / "Music")
    back = media["back.png"].read_bytes()
    embed(path, [(4, "image/png", back)])
    assert cover_file(path).read_bytes() == back


def test_repeat_lookups_are_cache_hits(tmp_path, media, monkeypatch):
    with_cover = copy(media, "mp3", tmp_path / "A")
    embed(with_cover, [(3, "image/jpeg", media["cover.jpg"].read_bytes())])
    without = copy(media, "opus", tmp_path / "B")
    parses = []
    real = covers._embedded
    monkeypatch.setattr(covers, "_embedded", lambda p: parses.append(p) or real(p))
    first = [cover_file(with_cover), cover_file(without)]
    assert first[1] is None and parses == [with_cover, without]
    assert [cover_file(with_cover), cover_file(without)] == first
    assert parses == [with_cover, without]


def test_a_retagged_file_gets_a_fresh_cover(tmp_path, media):
    path = copy(media, "mp3", tmp_path / "Music")
    embed(path, [(3, "image/jpeg", media["cover.jpg"].read_bytes())])
    old = cover_file(path)
    embed(path, [(3, "image/png", media["cover.png"].read_bytes())])
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    new = cover_file(path)
    assert new != old and new.suffix == ".png"


def test_no_cover_unknown_image_damaged_or_missing_file(tmp_path, media):
    plain = copy(media, "m4a", tmp_path / "plain")
    gif = copy(media, "flac", tmp_path / "gif")
    embed(gif, [(3, "image/gif", media["cover.gif"].read_bytes())])
    damaged = tmp_path / "damaged.mp3"
    damaged.write_bytes(b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f garbage")
    for path in (plain, gif, damaged, tmp_path / "missing.mp3"):
        assert cover_file(path) is None, path
    names = [p.suffix for p in (paths.cache_dir() / "covers").iterdir()]
    assert sorted(names) == [".none"] * 3  # one marker per existing file, no temp files
