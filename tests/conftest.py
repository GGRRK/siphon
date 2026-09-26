import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Where each platform keeps a user's settings, cache and data (siphon/paths.py).
USER_DIRS = ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")


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
