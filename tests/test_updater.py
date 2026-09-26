"""Siphon's own updates, offline: GitHub is a fixture or a fake urlopen, and "GitHub" for a git clone is a
bare repository on disk that https://github.com/GGRRK/siphon.git is rewritten to (git's url.insteadOf)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

import siphon
from siphon import updater

ROOT = Path(__file__).resolve().parent.parent


class Response(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class FakeWeb:
    """urlopen for the updater: url -> bytes or an exception, and a log of what was fetched."""

    def __init__(self, monkeypatch) -> None:
        self.answers: dict[str, bytes | Exception] = {}
        self.fetched: list[str] = []
        monkeypatch.setattr(updater.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout: float) -> Response:
        url = request.full_url
        self.fetched.append(url)
        answer = self.answers.get(url, urllib.error.HTTPError(url, 404, "Not Found", None, None))
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    return FakeWeb(monkeypatch)


def release_json(fixture_text, version: str = "0.1.1", **changes) -> dict:
    data = json.loads(fixture_text("github_release.json"))
    data["tag_name"] = f"v{version}"
    for asset in data["assets"]:
        asset["name"] = asset["name"].replace("0.1.1", version)
    return data | changes


# ---------------------------------------------------------------- versions and releases


@pytest.mark.parametrize("version, than, expected", [
    ("0.1.2", "0.1.1", True), ("0.1.10", "0.1.9", True), ("0.2.0", "0.1.99", True), ("1.0.0", "0.9.9", True),
    ("v0.1.2", "0.1.1", True), ("0.1.1", "0.1.1", False), ("0.1.0", "0.1.1", False), ("0.1.9", "0.1.10", False),
    ("latest", "0.1.1", False), ("0.1", "0.0.1", False), ("0.1.1-rc1", "0.1.0", False),
])
def test_versions_compare_as_numbers(version, than, expected):
    assert updater.newer(version, than) is expected


def test_the_real_release_parses(fixture_text):
    release = updater.parse_release(json.loads(fixture_text("github_release.json")))
    assert (release.version, release.tag) == ("0.1.1", "v0.1.1")
    assert release.page == "https://github.com/GGRRK/siphon/releases/tag/v0.1.1"
    assert release.installer == updater.Asset(
        "Siphon-0.1.1-Setup.exe", "https://github.com/GGRRK/siphon/releases/download/v0.1.1/Siphon-0.1.1-Setup.exe",
        "d37de4741e158838a41c1fe15b4d09c331df69c1cb6f1e42aff8dc8949b9dad0", 45134960)


def test_the_installer_is_the_one_of_the_tagged_version(fixture_text):
    data = release_json(fixture_text, "0.2.0")
    data["assets"][0]["name"] = "Siphon-0.1.9-Setup.exe"  # a stray file: not this release's installer
    assert updater.parse_release(data).installer is None
    assert updater.parse_release(release_json(fixture_text, "0.2.0", assets=[])).installer is None


@pytest.mark.parametrize("digest", [None, "", "md5:abc", "sha256:xyz", "sha256:" + "a" * 63])
def test_an_installer_without_a_usable_digest_has_none(fixture_text, digest):
    data = release_json(fixture_text)
    data["assets"][0]["digest"] = digest
    assert updater.parse_release(data).installer.sha256 == ""


@pytest.mark.parametrize("changes, message", [
    ({"draft": True}, "draft"), ({"prerelease": True}, "pre-release"),
    ({"tag_name": "nightly"}, "not a version"), ({"tag_name": None}, "not a version"),
    ({"html_url": ...}, "unexpected"), ({"tag_name": ...}, "unexpected"), ({"assets": "none"}, "unexpected"),
])
def test_releases_that_are_not_updates_are_refused(fixture_text, changes, message):
    data = {k: v for k, v in (release_json(fixture_text) | changes).items() if v is not ...}  # ...: left out
    with pytest.raises(updater.UpdateError, match=message):
        updater.parse_release(data)


@pytest.mark.parametrize("data", [[], "text", None, 3])
def test_json_that_is_not_a_release_is_refused(data):
    with pytest.raises(updater.UpdateError, match="unexpected"):
        updater.parse_release(data)


def test_latest_release_asks_github(web, fixture_text):
    web.answers[updater.RELEASES] = fixture_text("github_release.json").encode()
    assert updater.latest_release().version == "0.1.1"
    assert web.fetched == ["https://api.github.com/repos/GGRRK/siphon/releases/latest"]


@pytest.mark.parametrize("failure, message", [
    (urllib.error.HTTPError("u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"}, None), "rate limit"),
    (urllib.error.HTTPError("u", 403, "rate limit exceeded", {}, None), "rate limit"),
    (urllib.error.HTTPError("u", 429, "Too Many Requests", None, None), "rate limit"),
    (urllib.error.HTTPError("u", 502, "Bad Gateway", None, None), "GitHub answered 502 Bad Gateway"),
    (urllib.error.URLError("Name or service not known"), "Couldn't reach GitHub"),
    (TimeoutError("timed out"), "Couldn't reach GitHub"),
])
def test_github_failures_are_sentences(web, failure, message):
    web.answers[updater.RELEASES] = failure
    with pytest.raises(updater.UpdateError, match=message):
        updater.latest_release()


def test_a_broken_answer_is_a_sentence(web):
    web.answers[updater.RELEASES] = b"<html>"
    with pytest.raises(updater.UpdateError, match="unexpected"):
        updater.latest_release()


def test_a_test_release_replaces_github(monkeypatch, tmp_path, fixture_text):
    source = tmp_path / "release.json"  # read by the real urlopen, as packaging/windows/smoke.ps1 has it
    source.write_text(json.dumps(release_json(fixture_text, "99.0.0")))
    monkeypatch.setenv(updater.SOURCE_VARIABLE, source.as_uri())
    assert updater.latest_release().version == "99.0.0"
    source.unlink()
    with pytest.raises(updater.UpdateError, match="Couldn't reach GitHub"):
        updater.latest_release()


# ---------------------------------------------------------------- the Windows installer's download


INSTALLER = b"MZ a Windows installer" * 1000


def asset(*, sha256: str | None = None, url: str = "https://github.example/Siphon-0.2.0-Setup.exe",
          size: int | None = None) -> updater.Asset:
    return updater.Asset("Siphon-0.2.0-Setup.exe", url,
                         hashlib.sha256(INSTALLER).hexdigest() if sha256 is None else sha256,
                         len(INSTALLER) if size is None else size)


def update_files() -> set[str]:
    return {p.name for p in updater.update_dir().iterdir()} if updater.update_dir().is_dir() else set()


@pytest.mark.parametrize("size", [None, 0])  # the size GitHub gives, or the download's Content-Length
def test_a_checked_installer_is_kept(web, size):
    web.answers[asset().url] = INSTALLER
    fractions = []
    path = updater.download_installer(asset(size=size), fractions.append)
    assert path == updater.update_dir() / "Siphon-0.2.0-Setup.exe" and path.read_bytes() == INSTALLER
    assert fractions == sorted(fractions) and fractions[-1] == 1.0 and len(fractions) == 1  # 22 kB: one read
    assert update_files() == {"Siphon-0.2.0-Setup.exe"}


def test_a_wrong_checksum_leaves_nothing(web):
    web.answers[asset().url] = INSTALLER
    with pytest.raises(updater.UpdateError, match="failed its checksum"):
        updater.download_installer(asset(sha256="0" * 64), lambda _f: None)
    assert update_files() == set()


def test_a_missing_checksum_is_never_downloaded(web):
    with pytest.raises(updater.UpdateError, match="no checksum"):
        updater.download_installer(asset(sha256=""), lambda _f: None)
    assert web.fetched == [] and update_files() == set()


def test_only_secure_links(web, tmp_path):
    local = tmp_path / "Siphon-0.2.0-Setup.exe"
    local.write_bytes(INSTALLER)
    for url in ("http://github.example/Siphon-0.2.0-Setup.exe", local.as_uri()):
        with pytest.raises(updater.UpdateError, match="secure link"):
            updater.download_installer(asset(url=url), lambda _f: None)
    assert web.fetched == [] and update_files() == set()


def test_a_test_release_may_have_a_local_installer(monkeypatch, tmp_path):
    local = tmp_path / "Siphon-0.2.0-Setup.exe"  # read by the real urlopen
    local.write_bytes(INSTALLER)
    monkeypatch.setenv(updater.SOURCE_VARIABLE, (tmp_path / "release.json").as_uri())
    assert updater.download_installer(asset(url=local.as_uri()), lambda _f: None).read_bytes() == INSTALLER


def test_an_earlier_download_is_used_again_and_old_ones_go(web):
    folder = updater.update_dir()
    folder.mkdir(parents=True)
    (folder / "Siphon-0.2.0-Setup.exe").write_bytes(INSTALLER)
    (folder / "Siphon-0.1.9-Setup.exe").write_bytes(b"an older update")
    (folder / ".Siphon-0.2.0-Setup.exe-x1y2.part").write_bytes(b"half")
    (folder / "setup.log").write_text("the last install\n")
    assert updater.download_installer(asset(), lambda _f: None) == folder / "Siphon-0.2.0-Setup.exe"
    assert web.fetched == []
    assert update_files() == {"Siphon-0.2.0-Setup.exe", "setup.log"}
    updater.forget_downloads()  # up to date: the installer has done its job
    assert update_files() == {"setup.log"}


def test_a_damaged_earlier_download_is_replaced(web):
    updater.update_dir().mkdir(parents=True)
    (updater.update_dir() / "Siphon-0.2.0-Setup.exe").write_bytes(b"cut short")
    web.answers[asset().url] = INSTALLER
    assert updater.download_installer(asset(), lambda _f: None).read_bytes() == INSTALLER


def test_a_failed_download_is_a_sentence(web):
    web.answers[asset().url] = urllib.error.URLError("connection reset")
    with pytest.raises(updater.UpdateError, match="Couldn't download Siphon-0.2.0-Setup.exe"):
        updater.download_installer(asset(), lambda _f: None)
    assert update_files() == set()


# ---------------------------------------------------------------- what prepare() does for each kind of copy


RELEASE = updater.Release("0.2.0", "v0.2.0", "https://github.com/GGRRK/siphon/releases/tag/v0.2.0", asset())


def test_prepare_follows_the_kind_of_copy(monkeypatch):
    got = []
    monkeypatch.setattr(updater, "download_installer", lambda a, progress: got.append(a) or Path("setup.exe"))
    monkeypatch.setattr(updater, "stage", lambda repo, release: got.append((repo, release.tag)))
    for kind, expected in (("installer", ("ready", Path("setup.exe"))), ("portable", ("available", None)),
                           ("git", ("ready", None))):
        monkeypatch.setattr(updater, "install_kind", lambda: kind)
        assert updater.prepare(RELEASE, lambda _f: None) == expected
    assert got == [RELEASE.installer, (updater.checkout(), "v0.2.0")]
    monkeypatch.setattr(updater, "install_kind", lambda: "")
    with pytest.raises(updater.Unavailable, match="neither from the installer nor from a git clone"):
        updater.prepare(RELEASE, lambda _f: None)


def test_a_release_without_an_installer_fails_on_an_installed_copy(monkeypatch):
    monkeypatch.setattr(updater, "install_kind", lambda: "installer")
    with pytest.raises(updater.UpdateError, match="no Windows installer"):
        updater.prepare(updater.Release("0.2.0", "v0.2.0", "page", None), lambda _f: None)


def test_this_checkout_is_a_git_clone():
    assert updater.checkout() == ROOT
    assert updater.install_kind() == ("git" if (ROOT / ".git").exists() and os.name != "nt" else "")


# ---------------------------------------------------------------- siphon update


@pytest.fixture
def cli_release(monkeypatch):
    def use(version: str, kind: str = "portable") -> list:
        started = []
        release = updater.Release(version, f"v{version}", f"https://github.com/GGRRK/siphon/releases/tag/v{version}",
                                  asset())
        monkeypatch.setattr(updater, "latest_release", lambda: release)
        monkeypatch.setattr(updater, "install_kind", lambda: kind)
        monkeypatch.setattr(updater, "download_installer", lambda a, progress: Path("Setup.exe"))
        monkeypatch.setattr(updater, "stage", lambda repo, release: None)
        monkeypatch.setattr(updater, "run_installer", lambda path, relaunch: started.append((path, relaunch)))
        return started

    return use


def test_cli_up_to_date(cli_release, capsys):
    cli_release(siphon.__version__)
    assert updater.cli([]) == 0
    version = siphon.__version__
    assert capsys.readouterr().out == f"Siphon {version} is up to date (latest release {version}).\n"


def test_cli_portable_points_at_the_release(cli_release, capsys):
    cli_release("99.0.0")
    assert updater.cli([]) == 0
    assert "Siphon 99.0.0 is available: https://github.com/GGRRK/siphon/releases/tag/v99.0.0" in capsys.readouterr().out


def test_cli_installed_runs_the_installer_which_opens_siphon(cli_release, capsys):
    started = cli_release("99.0.0", "installer")
    assert updater.cli([]) == 0
    assert started == [(Path("Setup.exe"), True)]
    assert "Siphon 99.0.0 is being installed; it opens when the installer is done." in capsys.readouterr().out


def test_cli_git_installs_at_the_next_start(cli_release, capsys):
    cli_release("99.0.0", "git")
    assert updater.cli([]) == 0
    assert "Siphon 99.0.0 installs the next time Siphon starts." in capsys.readouterr().out


def test_cli_failures(monkeypatch, capsys):
    def offline():
        raise updater.UpdateError("Couldn't reach GitHub (offline) - check your connection.")

    monkeypatch.setattr(updater, "latest_release", offline)
    assert updater.cli([]) == 1 and "Couldn't reach GitHub" in capsys.readouterr().err
    assert updater.cli(["--now"]) == 2


# ---------------------------------------------------------------- a git clone


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GitHub:
    """A bare repository standing in for github.com/GGRRK/siphon, and a working copy that publishes to it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bare = root / "github.git"
        self.work = root / "upstream"
        git(root, "init", "-q", "--bare", str(self.bare))
        git(root, "init", "-q", str(self.work))

    def publish(self, version: str, files: dict[str, str]) -> str:
        """Commit files, tag the commit v<version> (annotated, as a release is) and push both."""
        for name, text in files.items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"Siphon {version}")
        git(self.work, "tag", "-a", f"v{version}", "-m", f"Siphon {version}")
        git(self.work, "push", "-q", str(self.bare), "master", f"v{version}")
        return git(self.work, "rev-parse", "HEAD")

    def clone(self, name: str = "clone") -> Path:
        git(self.root, "clone", "-q", updater.CLONE_URL, name)
        return self.root / name


@pytest.fixture
def github(tmp_path, monkeypatch) -> GitHub:
    if shutil.which("git") is None:
        pytest.skip("needs git")
    root = tmp_path / "git"
    root.mkdir()
    config = tmp_path / "gitconfig"  # never the user's own: no signing, hooks or other defaults
    config.write_text(f'[user]\n\tname = Siphon Tests\n\temail = tests@example.invalid\n'
                      f'[init]\n\tdefaultBranch = master\n'
                      f'[url "{root / "github.git"}"]\n\tinsteadOf = {updater.CLONE_URL}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return GitHub(root)


def release(version: str) -> updater.Release:
    return updater.Release(version, f"v{version}", "", None)


FIRST = {"install.sh": "echo installing\n", "siphon/__init__.py": '__version__ = "0.1.1"\n'}
SECOND = {"siphon/__init__.py": '__version__ = "0.1.2"\n', "siphon/new.py": "NEW = True\n"}


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def marker(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir")) / updater.MARKER


@pytest.mark.linux
def test_a_clean_master_behind_the_release_installs_it_at_the_next_start(github):
    old = github.publish("0.1.1", FIRST)
    clone = github.clone()
    (clone / "__pycache__").mkdir()  # untracked files are the running Siphon's, not changes
    new = github.publish("0.1.2", SECOND)
    updater.stage(clone, release("0.1.2"))
    # fetched, noted, and nothing under the running Siphon changed
    assert head(clone) == old and not (clone / "siphon" / "new.py").exists()
    assert json.loads(marker(clone).read_text()) == {"version": "0.1.2", "commit": new}
    assert git(clone, "tag") == "v0.1.1"  # no ref of the clone's own was touched
    assert updater.apply(clone) == "installed Siphon 0.1.2"
    assert head(clone) == new and (clone / "siphon" / "new.py").exists()
    assert git(clone, "status", "--porcelain", "--untracked-files=no") == "" and not marker(clone).exists()


@pytest.mark.linux
@pytest.mark.parametrize("ssh", ["git@github.com:GGRRK/siphon.git", "ssh://git@github.com/ggrrk/siphon",
                                 "https://github.com/GGRRK/siphon"])
def test_every_address_of_the_repository_counts(github, ssh):
    github.publish("0.1.1", FIRST)
    clone = github.clone()
    git(clone, "remote", "set-url", "origin", ssh)
    new = github.publish("0.1.2", SECOND)
    assert updater.git_blocker(clone) == ""
    updater.stage(clone, release("0.1.2"))  # fetched from GitHub's https address, never with an SSH key
    assert json.loads(marker(clone).read_text())["commit"] == new


def dirty(clone: Path) -> None:
    (clone / "install.sh").write_text("echo changed by hand\n")


def other_branch(clone: Path) -> None:
    git(clone, "switch", "-q", "-c", "feature/updater")


def detached(clone: Path) -> None:
    git(clone, "switch", "-q", "--detach")


def foreign_origin(clone: Path) -> None:
    git(clone, "remote", "set-url", "origin", "https://github.com/someone/siphon.git")


def no_origin(clone: Path) -> None:
    git(clone, "remote", "remove", "origin")


def local_commit(clone: Path) -> None:
    (clone / "mine.txt").write_text("my own change\n")
    git(clone, "add", "mine.txt")
    git(clone, "commit", "-q", "-m", "mine")


@pytest.mark.linux
@pytest.mark.parametrize("change, reason", [
    (dirty, "it has uncommitted changes"),
    (other_branch, "it is on branch feature/updater, not master"),
    (detached, "it is on a detached HEAD, not master"),
    (foreign_origin, "its origin is https://github.com/someone/siphon.git, not github.com/GGRRK/siphon"),
    (no_origin, "its origin is not set"),
    (local_commit, "it has commits the release does not"),
])
def test_anything_but_a_clean_master_is_left_alone(github, change, reason):
    github.publish("0.1.1", FIRST)
    clone = github.clone()
    change(clone)
    before = head(clone)
    github.publish("0.1.2", SECOND)
    with pytest.raises(updater.Unavailable, match=f"Siphon 0.1.2 is out, but this copy is left as it is: {reason}"):
        updater.stage(clone, release("0.1.2"))
    assert head(clone) == before and not marker(clone).exists()


@pytest.mark.linux
@pytest.mark.parametrize("change, reason", [(dirty, "uncommitted changes"), (other_branch, "on branch"),
                                            (local_commit, "commits the release does not")])
def test_the_next_start_checks_again(github, change, reason):
    github.publish("0.1.1", FIRST)
    clone = github.clone()
    github.publish("0.1.2", SECOND)
    updater.stage(clone, release("0.1.2"))
    change(clone)  # between the run that fetched the release and the next start
    before = head(clone)
    assert reason in updater.apply(clone)
    assert head(clone) == before and not marker(clone).exists()


@pytest.mark.linux
def test_nothing_is_installed_while_another_siphon_runs_from_the_clone(github):
    github.publish("0.1.1", FIRST)
    clone = github.clone()
    new = github.publish("0.1.2", SECOND)
    updater.stage(clone, release("0.1.2"))
    # a Siphon from this clone, still running: hold_checkout() as __main__ calls it
    running = subprocess.Popen([sys.executable, "-c", "import sys; from siphon import updater; "
                                "updater.checkout = lambda: __import__('pathlib').Path(sys.argv[1]); "
                                "updater.hold_checkout(); print('held', flush=True); sys.stdin.read()", str(clone)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        assert running.stdout.readline() == "held\n"
        assert updater.apply(clone) == "Siphon 0.1.2 waits for a start when no other Siphon is running"
        assert head(clone) != new and marker(clone).exists()
    finally:
        running.communicate("", timeout=30)  # that Siphon quits
    assert updater.apply(clone) == "installed Siphon 0.1.2" and head(clone) == new


@pytest.mark.linux
def test_a_damaged_note_is_dropped(github):
    github.publish("0.1.1", FIRST)
    clone = github.clone()
    marker(clone).write_text("{")
    assert updater.apply(clone) == "dropped an unreadable update note" and not marker(clone).exists()


@pytest.mark.linux
@pytest.mark.parametrize("works", [True, False])
def test_a_changed_install_sh_runs_before_siphon_does(github, tmp_path, works):
    old = github.publish("0.1.1", FIRST)
    clone = github.clone()
    ran = tmp_path / "install-ran"
    script = f'echo "$@" > "{ran}"\n' + ("" if works else 'echo "pip: no network" >&2; exit 1\n')
    new = github.publish("0.1.2", SECOND | {"install.sh": script})
    updater.stage(clone, release("0.1.2"))
    outcome = updater.apply(clone)
    assert ran.read_text() == "\n"  # install.sh, plain: install or repair
    if works:
        assert outcome == "installed Siphon 0.1.2" and head(clone) == new
    else:  # the new code would need what install.sh failed to install: stay on the old version
        assert outcome == "Siphon 0.1.2 was not installed: its install.sh failed (pip: no network)"
        assert head(clone) == old and not (clone / "siphon" / "new.py").exists()
        assert git(clone, "status", "--porcelain", "--untracked-files=no") == ""


@pytest.mark.linux
def test_an_unchanged_install_sh_does_not_run(github, tmp_path):
    ran = tmp_path / "install-ran"
    github.publish("0.1.1", FIRST | {"install.sh": f'touch "{ran}"\n'})
    clone = github.clone()
    github.publish("0.1.2", SECOND)
    updater.stage(clone, release("0.1.2"))
    assert updater.apply(clone) == "installed Siphon 0.1.2" and not ran.exists()


@pytest.mark.linux
def test_bin_siphon_installs_the_fetched_release_before_python_starts(github, tmp_path):
    code = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in (ROOT / "siphon").rglob("*.py")}
    code |= {"bin/siphon": (ROOT / "bin" / "siphon").read_text(encoding="utf-8"), "install.sh": "exit 0\n"}
    github.publish("0.1.1", code | {"siphon/__init__.py": '__version__ = "0.1.1"\n'})
    clone = github.clone()
    # Checked out an hour ago, as a real clone was: Python's bytecode cache goes by the source's mtime in whole
    # seconds and size, and 0.1.1 and 0.1.2 have the same size.
    an_hour_ago = time.time() - 3600
    os.utime(clone / "siphon" / "__init__.py", (an_hour_ago, an_hour_ago))
    new = github.publish("0.1.2", {"siphon/__init__.py": '__version__ = "0.1.2"\n'})
    env = {**os.environ, "SIPHON_VENV": sys.prefix, "HOME": str(tmp_path / "home")}

    def version_of_siphon() -> subprocess.CompletedProcess:
        # from a folder holding another siphon package: the clone's own code must run all the same
        return subprocess.run(["bash", str(clone / "bin" / "siphon"), "--version"], capture_output=True, text=True,
                              env=env, timeout=120, cwd=ROOT)

    before = version_of_siphon()
    assert before.stdout.startswith("Siphon 0.1.1\n") and "siphon:" not in before.stderr  # nothing noted yet
    updater.stage(clone, release("0.1.2"))
    after = version_of_siphon()
    assert after.returncode == 0 and after.stdout.startswith("Siphon 0.1.2\n"), after.stderr
    assert "siphon: installed Siphon 0.1.2" in after.stderr
    assert head(clone) == new and not marker(clone).exists()
