"""Engine updates for packaged builds, against a fake PyPI (urlopen is replaced; nothing leaves the machine)."""

import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import zipfile

import pytest

from siphon import core, engine

CURRENT = "2026.08.19"


@pytest.fixture(autouse=True)
def private_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))  # activate() edits it


def wheel(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return out.getvalue()


def ytdlp_wheel(version: str) -> bytes:
    return wheel({"yt_dlp/__init__.py": "", "yt_dlp/version.py": f"__version__ = '{version}'\n"})


class FakePyPI:
    """The JSON API and the file host: url -> bytes, and a log of what was fetched."""

    def __init__(self, monkeypatch) -> None:
        self.files: dict[str, bytes] = {}
        self.fetched: list[str] = []
        monkeypatch.setattr(engine.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout: float) -> io.BytesIO:
        url = getattr(request, "full_url", request)
        self.fetched.append(url)
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return io.BytesIO(self.files[url])

    def release(self, project: str, version: str, data: bytes, *, sha256: str = "", latest: bool = True,
                requires: tuple[str, ...] = (), python: str = ">=3.10") -> str:
        filename = f"{project.replace('-', '_')}-{version}-py3-none-any.whl"
        url = f"https://files.example/{filename}"
        info = {"info": {"name": project, "version": version, "requires_python": python,
                         "requires_dist": list(requires)},
                "urls": [{"packagetype": "sdist", "filename": f"{project}-{version}.tar.gz",
                          "url": "https://files.example/sdist", "digests": {"sha256": "0" * 64}},
                         {"packagetype": "bdist_wheel", "filename": filename, "url": url,
                          "digests": {"sha256": sha256 or hashlib.sha256(data).hexdigest()}}]}
        self.files[url] = data
        self.files[f"{engine.PYPI}/{project}/{version}/json"] = json.dumps(info).encode()
        if latest:
            self.files[f"{engine.PYPI}/{project}/json"] = json.dumps(info).encode()
        return filename

    def publish(self, version: str, **kwargs) -> str:
        """A yt-dlp release pinning yt-dlp-ejs 0.9.0; returns the yt-dlp wheel's file name."""
        self.release("yt-dlp-ejs", "0.9.0", wheel({"yt_dlp_ejs/__init__.py": ""}), latest=False)
        self.release("yt-dlp-ejs", "1.0.0", b"the newest solver, which this release does not pin")
        return self.release("yt-dlp", version, kwargs.pop("data", None) or ytdlp_wheel(version),
                            requires=('yt-dlp-ejs==0.9.0; extra == "default"',), **kwargs)


@pytest.fixture
def pypi(monkeypatch) -> FakePyPI:
    return FakePyPI(monkeypatch)


def wheels_on_disk() -> set[str]:
    return {p.name for p in engine.engine_dir().iterdir() if p.name != engine.MANIFEST}


def test_update_installs_checked_wheels_that_the_next_start_loads(pypi, monkeypatch):
    name = pypi.publish("2099.1.2", data=ytdlp_wheel("2099.01.02"))
    assert engine.update(CURRENT) == "2099.01.02"
    version, wheels = engine.installed()
    assert version == "2099.01.02"
    assert [w.name for w in wheels] == [name, "yt_dlp_ejs-0.9.0-py3-none-any.whl"]  # the pinned solver
    assert wheels_on_disk() == {w.name for w in wheels}

    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr(engine, "_bundled_version", lambda: "2026.8.19")
    assert engine.activate() == "2099.01.02"
    assert sys.path[:2] == [str(w) for w in wheels]
    # a pure-Python wheel imports straight from the zip file
    done = subprocess.run([sys.executable, "-S", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                           "from yt_dlp.version import __version__; print(__version__)", str(wheels[0])],
                          capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "2099.01.02"


def test_up_to_date_downloads_nothing(pypi):
    pypi.publish("2026.8.19")
    assert engine.update(CURRENT) == CURRENT
    assert pypi.fetched == [f"{engine.PYPI}/yt-dlp/json"]
    assert engine.installed() is None


def test_a_downloaded_update_is_not_fetched_again(pypi):
    pypi.publish("2099.1.2")
    engine.update(CURRENT)
    pypi.fetched.clear()
    assert engine.update(CURRENT) == "2099.1.2"  # waiting for a restart: say so, download nothing
    assert pypi.fetched == [f"{engine.PYPI}/yt-dlp/json"]


@pytest.mark.parametrize("breakage, message", [
    ({"sha256": "f" * 64}, "failed its checksum"),
    ({"data": wheel({"yt_dlp/__init__.py": ""})}, "is damaged"),
    ({"data": ytdlp_wheel("2099.01.03")}, "says it is 2099.01.03"),
    ({"python": ">=3.99"}, "needs Python 3.99"),
])
def test_a_bad_download_never_replaces_the_working_engine(pypi, breakage, message):
    pypi.publish("2099.1.2")
    engine.update(CURRENT)
    before = engine.installed()
    pypi.publish("2099.2.2", **breakage)
    with pytest.raises(engine.UpdateError, match=message):
        engine.update(CURRENT)
    assert engine.installed() == before
    assert not any(name.endswith(".part") for name in wheels_on_disk())


def test_network_and_pypi_failures_are_sentences(pypi, monkeypatch):
    with pytest.raises(engine.UpdateError, match="Couldn't reach PyPI"):
        engine.update(CURRENT)  # 404: nothing published
    pypi.files[f"{engine.PYPI}/yt-dlp/json"] = b"<html>maintenance</html>"
    with pytest.raises(engine.UpdateError, match="PyPI sent something unexpected"):
        engine.update(CURRENT)


def test_old_wheels_go_unless_this_process_imports_from_them(pypi):
    first = pypi.publish("2099.1.1")
    engine.update(CURRENT)
    sys.path.insert(0, str(engine.engine_dir() / first))  # running on it: yt-dlp loads extractors lazily
    second = pypi.publish("2099.1.2")
    engine.update(CURRENT)
    assert {first, second} <= wheels_on_disk()
    sys.path.pop(0)
    third = pypi.publish("2099.1.3")
    engine.update(CURRENT)
    assert wheels_on_disk() == {third, "yt_dlp_ejs-0.9.0-py3-none-any.whl"}


def test_activate_keeps_a_newer_bundled_engine(pypi, monkeypatch):
    pypi.publish("2026.1.1")
    engine.update("2025.12.31")
    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    path = list(sys.path)
    monkeypatch.setattr(engine, "_bundled_version", lambda: "2026.8.19")  # a newer Siphon install
    assert engine.activate() is None and sys.path == path


def test_activate_ignores_an_incomplete_engine(pypi, monkeypatch):
    first = pypi.publish("2099.1.2")
    engine.update(CURRENT)
    (engine.engine_dir() / first).unlink()
    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    assert engine.installed() is None and engine.activate() is None


def test_core_updates_a_packaged_build_through_the_engine(tmp_path, monkeypatch):
    def refuse(current: str) -> str:
        raise engine.UpdateError("No thanks.")

    monkeypatch.setenv("SIPHON_BUNDLE", str(tmp_path))
    monkeypatch.setattr(engine, "update", lambda current: f"after {current}")
    assert core.update_engine() == f"after {core.engine_version()}"
    monkeypatch.setattr(engine, "update", refuse)
    with pytest.raises(core.SiphonError, match="No thanks."):
        core.update_engine()


def test_versions_compare_as_dates():
    assert engine._parts("2026.08.19") == engine._parts("2026.8.19") < engine._parts("2026.8.19.1")
    assert engine._pin(["requests>=2", 'yt-dlp-ejs==0.9.0; extra == "default"'], "yt-dlp-ejs") == "0.9.0"
    assert engine._pin(["yt-dlp-ejs>=0.8"], "yt-dlp-ejs") == ""
