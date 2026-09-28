import select
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Where each platform keeps a user's settings, cache and data (siphon/paths.py).
USER_DIRS = ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")
_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:abstract={name}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def pytest_configure(config):
    config.addinivalue_line("markers", "linux: Linux-only behaviour (XDG folders, POSIX file names); "
                                       "skipped on Windows, where tests/test_windows.py covers the difference")


def pytest_collection_modifyitems(config, items):
    if sys.platform == "win32":
        for item in items:
            if "linux" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Linux-only behaviour"))


@pytest.fixture(autouse=True)
def private_dirs(tmp_path, monkeypatch):
    """No test reads or writes the real user's folders, on either platform."""
    for name in USER_DIRS:
        monkeypatch.setenv(name, str(tmp_path / "xdg" / name))
    monkeypatch.delenv("SIPHON_BUNDLE", raising=False)


@pytest.fixture
def fixture_text():
    return lambda name: (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def x_display():
    """The name of a private X display nobody sees (Xvfb, or gamescope's headless backend), for the XEmbed tray;
    skips without python-xlib (a test dependency only) or without either server."""
    if sys.platform == "win32":
        pytest.skip("X11 only")
    pytest.importorskip("Xlib")
    from xtray import x_server

    with x_server() as name:
        if name is None:
            pytest.skip("needs Xvfb or gamescope for an invisible X server")
        yield name


@pytest.fixture(scope="module")
def bus(tmp_path_factory):
    """The address of a private session bus: our own dbus-daemon with no service directories, so nothing can be
    auto-started on it and nothing touches the desktop's bus (an abstract socket: no file to leave behind)."""
    if shutil.which("dbus-daemon") is None:
        pytest.skip("needs dbus-daemon")
    config = tmp_path_factory.mktemp("bus") / "session.conf"
    config.write_text(_BUS_CONFIG.format(name=f"siphon-test-{uuid.uuid4().hex}"))
    daemon = subprocess.Popen(["dbus-daemon", "--nofork", f"--config-file={config}", "--print-address=1"],
                              stdout=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([daemon.stdout], [], [], 5)
        assert ready, "dbus-daemon did not start"
        yield daemon.stdout.readline().strip()
    finally:
        daemon.terminate()
        daemon.wait(5)
