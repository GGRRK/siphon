"""Siphon's own updates, from its GitHub releases.

latest_release() reads GitHub's latest release (never a draft or a pre-release) and newer() compares its
vX.Y.Z tag with this Siphon's version. prepare() then gets a newer one ready the way this copy was
installed (install_kind()):

- "installer", the Windows setup: Siphon-<version>-Setup.exe is downloaded and checked against the sha256
  GitHub publishes for it, then run silently once Siphon has quit (run_installer). The installer is the only
  step that replaces files, and it waits for Siphon's process to end first (packaging/windows/siphon.iss).
- "portable", the Windows zip: never changed; the release page is offered instead.
- "git", a clone run by bin/siphon: the release's commit is fetched without touching the working tree and
  noted in the clone's git folder; bin/siphon fast-forwards to it at the next start (apply), before any of
  Siphon's code loads. Siphon imports some modules lazily, so changing files under a running Siphon could
  mix two versions in one process. Only a clone of GGRRK/siphon on a clean master that the release extends
  is touched; anything else, such as a developer's branch, is left as it is.
"""

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import __version__, paths

REPO = "GGRRK/siphon"
RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"
CLONE_URL = f"https://github.com/{REPO}.git"
# For tests (packaging/windows/smoke.ps1): the URL of a release in GitHub's JSON to use instead of the latest
# one; its installer may then be a file: URL.
SOURCE_VARIABLE = "SIPHON_UPDATE_SOURCE"
APP_ID = "{A7D6F0BC-783B-4BCF-AAD1-CC4448D09A32}"  # the AppId of packaging/windows/siphon.iss
BRANCH = "master"
MARKER = "siphon-update"  # in a clone's git folder: the release bin/siphon installs at the next start
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)ggrrk/siphon(?:\.git)?/?",
                     re.IGNORECASE)
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives Siphon, no console
_held: int | None = None  # hold_checkout()'s folder, open (and locked) for as long as Siphon runs


class UpdateError(Exception):
    """A check or a download that failed, as one user-readable sentence."""


class Unavailable(UpdateError):
    """This copy of Siphon can't install the release itself, as one sentence saying why."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str  # "" when GitHub gave no digest: such a file is never run
    size: int


@dataclass(frozen=True)
class Release:
    version: str  # "0.1.2"
    tag: str  # "v0.1.2"
    page: str  # the release on github.com
    installer: Asset | None  # Siphon-<version>-Setup.exe


def version_parts(version: str) -> tuple[int, ...]:
    """'v0.1.10' -> (0, 1, 10); () when it isn't a version."""
    m = _VERSION.fullmatch(version.strip())
    return tuple(int(n) for n in m.groups()) if m else ()


def newer(version: str, than: str = __version__) -> bool:
    """Whether version is a later release than `than`, compared as numbers: 0.1.10 comes after 0.1.9."""
    return version_parts(version) > version_parts(than)


def parse_release(data: dict) -> Release:
    """A release from GitHub's JSON. Raises UpdateError unless it is a published release with a version tag."""
    try:
        if data.get("draft") or data.get("prerelease"):
            raise UpdateError("GitHub's latest release is a draft or a pre-release.")
        tag = str(data["tag_name"])
        if not version_parts(tag):
            raise UpdateError(f"GitHub's latest release is tagged {tag}, which is not a version.")
        version = tag.removeprefix("v")
        name = f"Siphon-{version}-Setup.exe"  # OutputBaseFilename in siphon.iss
        found = next((a for a in data.get("assets") or [] if a.get("name") == name), None)
        installer = None
        if found is not None:
            digest = _DIGEST.fullmatch(str(found.get("digest") or "").lower())
            installer = Asset(name, str(found["browser_download_url"]), digest[1] if digest else "",
                              int(found.get("size") or 0))
        return Release(version, tag, str(data["html_url"]), installer)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise UpdateError("GitHub sent something unexpected - try again later.") from None


def latest_release() -> Release:
    """GitHub's latest release of Siphon. Raises UpdateError."""
    try:
        with urllib.request.urlopen(_request(os.environ.get(SOURCE_VARIABLE) or RELEASES), timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        # 60 requests an hour per address without an account: a shared address runs out
        spent = e.headers is not None and e.headers.get("X-RateLimit-Remaining") == "0"
        if e.code == 429 or (e.code == 403 and (spent or "rate limit" in str(e.reason).lower())):
            raise UpdateError("GitHub's rate limit was reached - try again later.") from None
        raise UpdateError(f"GitHub answered {e.code} {e.reason} - try again later.") from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
        raise UpdateError(f"Couldn't reach GitHub ({getattr(e, 'reason', e)}) - check your connection.") from None
    except ValueError:
        raise UpdateError("GitHub sent something unexpected - try again later.") from None
    return parse_release(data)


def install_kind() -> str:
    """"installer", "portable", "git", or "" for anything else (an unpacked source archive, or a Windows
    checkout, which has no bin/siphon to install an update at the next start)."""
    if getattr(sys, "frozen", False):
        where, bundle = _registered_location(), str(paths.bundle_dir())
        # PureWindowsPath compares as Windows does, ignoring case and a trailing backslash; samefile also sees
        # through a short 8.3 name such as C:\Users\RUNNER~1
        same = bool(where) and (PureWindowsPath(where) == PureWindowsPath(bundle) or _same_folder(where, bundle))
        return "installer" if same else "portable"
    return "git" if not paths.windows() and (checkout() / ".git").exists() else ""


def checkout() -> Path:
    """The folder Siphon's code is in: the repository, when it runs from a clone."""
    return Path(__file__).resolve().parent.parent


def prepare(release: Release, progress: Callable[[float], None]) -> tuple[str, Path | None]:
    """Get a newer release ready the way this copy installs it: ("ready", the checked installer) on Windows,
    ("ready", None) once a git clone has fetched it, ("available", None) for the portable zip, which is never
    changed. progress gets the downloaded fraction. Raises Unavailable where Siphon can't update itself and
    UpdateError when a step failed."""
    kind = install_kind()
    if kind == "installer":
        if release.installer is None:
            raise UpdateError(f"Siphon {release.version} has no Windows installer on GitHub.")
        return "ready", download_installer(release.installer, progress)
    if kind == "portable":
        return "available", None
    if kind == "git":
        stage(checkout(), release)
        return "ready", None
    raise Unavailable(_left_alone(release.version, "it came neither from the installer nor from a git clone"))


# ---------------------------------------------------------------- Windows


def update_dir() -> Path:
    return paths.data_dir() / "update"


def download_installer(asset: Asset, progress: Callable[[float], None]) -> Path:
    """The installer, downloaded into update_dir() and checked against GitHub's sha256; returns its path.
    Raises UpdateError, and then leaves no unchecked file behind."""
    if not asset.sha256:
        raise UpdateError(f"GitHub gives no checksum for {asset.name}, so it is not installed.")
    local = bool(os.environ.get(SOURCE_VARIABLE)) and asset.url.startswith("file:")
    if not (asset.url.startswith("https://") or local):
        raise UpdateError(f"{asset.name} is not on a secure link, so it is not installed.")
    folder = update_dir()
    target = folder / asset.name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _remove_old(folder, keep=target.name)
        if target.is_file() and _sha256(target) == asset.sha256:
            return target  # downloaded by an earlier run
        target.unlink(missing_ok=True)
        fd, part = tempfile.mkstemp(prefix=f".{asset.name}-", suffix=".part", dir=folder)
        try:
            digest, done = hashlib.sha256(), 0
            with (os.fdopen(fd, "wb") as out,
                  urllib.request.urlopen(_request(asset.url, "application/octet-stream"), timeout=60) as response):
                total = asset.size or int(response.headers.get("Content-Length") or 0)
                while chunk := response.read(1 << 16):
                    digest.update(chunk)
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        progress(min(done / total, 1.0))
            if digest.hexdigest() != asset.sha256:
                raise UpdateError(f"{asset.name} failed its checksum, so it is not installed.")
            os.replace(part, target)
        finally:
            Path(part).unlink(missing_ok=True)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
        raise UpdateError(f"Couldn't download {asset.name} ({getattr(e, 'reason', e)}) - check your connection.") \
            from None
    except OSError as e:
        raise UpdateError(f"Couldn't save {asset.name}: {e.strerror or e}.") from None
    return target


def forget_downloads() -> None:
    """Remove the installers of earlier updates, once Siphon is up to date."""
    if update_dir().is_dir():
        _remove_old(update_dir())


def installer_command(installer: Path, relaunch: bool, pid: int) -> list[str]:
    """The installer's silent command line: no window or message box; it waits for process pid to end,
    closes any other Siphon using its files (without starting it again), logs to update_dir()/setup.log and,
    with relaunch, starts Siphon when it is done (/WAITPID and /RELAUNCH are siphon.iss's own)."""
    command = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
               "/NORESTARTAPPLICATIONS", f"/WAITPID={pid}", f"/LOG={update_dir() / 'setup.log'}"]
    return command + ["/RELAUNCH"] if relaunch else command


def run_installer(installer: Path, relaunch: bool) -> None:
    """Start the installer to replace Siphon once this process has ended. Raises OSError."""
    subprocess.Popen(installer_command(installer, relaunch, os.getpid()), creationflags=_DETACHED, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- git clone


def git_blocker(repo: Path) -> str:
    """Why this clone must be left as it is, or "" when it is a clean master of GGRRK/siphon."""
    if shutil.which("git") is None:
        return "git is not installed"
    origin = _git(repo, "config", "--get", "remote.origin.url").stdout.strip()
    if not _ORIGIN.fullmatch(origin):
        return f"its origin is {origin or 'not set'}, not github.com/{REPO}"
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != BRANCH:
        return f"it is on {f'branch {branch}' if branch else 'a detached HEAD'}, not {BRANCH}"
    if _git_ok(repo, "status", "--porcelain", "--untracked-files=no"):
        return "it has uncommitted changes"
    return ""


def stage(repo: Path, release: Release) -> None:
    """Fetch the release's commit (the working tree stays as it is) and note it for bin/siphon, which
    fast-forwards to it at the next start. Raises Unavailable or UpdateError."""
    if reason := git_blocker(repo):
        raise Unavailable(_left_alone(release.version, reason))
    # From GitHub rather than origin: an SSH origin could ask for a key's passphrase with nobody there.
    # A tag fetched by name lands in FETCH_HEAD only, so none of the clone's own refs change.
    _git_ok(repo, "fetch", "--quiet", "--no-tags", CLONE_URL, f"refs/tags/{release.tag}")
    commit = _git_ok(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
    if not _is_ancestor(repo, "HEAD", commit):
        raise Unavailable(_left_alone(release.version, "it has commits the release does not"))
    marker = _marker(repo)
    part = marker.with_name(f".{MARKER}.part")
    part.write_text(json.dumps({"version": release.version, "commit": commit}), encoding="utf-8")
    os.replace(part, marker)


def apply(repo: Path) -> str:
    """Before Siphon starts (bin/siphon): fast-forward to the release the last run fetched, and run install.sh
    when the release changed it (a new dependency, say). Returns what happened, for the log."""
    marker = _marker(repo)
    try:
        note = json.loads(marker.read_text(encoding="utf-8"))
        version, commit = str(note["version"]), str(note["commit"])
    except (OSError, ValueError, KeyError, TypeError):
        marker.unlink(missing_ok=True)
        return "dropped an unreadable update note"
    import fcntl

    folder = os.open(repo, os.O_RDONLY)
    try:
        try:
            fcntl.flock(folder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # the note stays for a start with no other Siphon about
            return f"Siphon {version} waits for a start when no other Siphon is running"
        try:
            return _fast_forward(repo, version, commit)
        except UpdateError as e:
            return f"Siphon {version} was not installed: {e}"
        finally:
            marker.unlink(missing_ok=True)
    finally:
        os.close(folder)


def hold_checkout() -> None:
    """Keep apply() away while this Siphon runs from a clone: a shared lock on the clone's folder, which apply()
    must take whole. A `siphon get` in a terminal would otherwise update the files of the window still open."""
    global _held
    if paths.windows() or getattr(sys, "frozen", False):
        return
    import fcntl

    try:
        _held = os.open(checkout(), os.O_RDONLY)  # a folder: git replaces files, never the folder itself
        fcntl.flock(_held, fcntl.LOCK_SH)  # waits while an update is being installed
    except OSError:
        pass


def relaunch_command(pid: int) -> list[str]:
    """Start Siphon again through bin/siphon, which installs the update, once process pid has ended: a
    Siphon still running would take the new start over (GApplication's single instance). An ended process
    its parent has not reaped yet (a zombie, state Z) counts as ended."""
    wait = 'while kill -0 "$1" 2>/dev/null && ! ps -o stat= -p "$1" | grep -q "^Z"; do sleep 0.1; done'
    return ["sh", "-c", f'{wait}; exec "$2"', "sh", str(pid), str(checkout() / "bin" / "siphon")]


def relaunch() -> None:
    """Restart to update, on Linux. Raises OSError."""
    subprocess.Popen(relaunch_command(os.getpid()), stdin=subprocess.DEVNULL, start_new_session=True)


# ---------------------------------------------------------------- command line


def cli(argv: list[str]) -> int:
    """`siphon update`: check GitHub and get a newer Siphon ready. On Windows the installer runs once this
    command has ended and opens Siphon when it is done; a git clone installs it at the next start."""
    if argv:
        print("usage: siphon update", file=sys.stderr)
        return 2
    try:
        release = latest_release()
        if not newer(release.version):
            print(f"Siphon {__version__} is up to date (latest release {release.version}).")
            return 0
        print(f"Getting Siphon {release.version} ready…")
        state, installer = prepare(release, lambda _fraction: None)
        if installer is not None:
            run_installer(installer, relaunch=True)
    except UpdateError as e:
        print(e, file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Couldn't start the installer: {e.strerror or e}.", file=sys.stderr)
        return 1
    if state == "available":
        print(f"Siphon {release.version} is available: {release.page}")
    elif installer is not None:
        print(f"Siphon {release.version} is being installed; it opens when the installer is done.")
    else:
        print(f"Siphon {release.version} installs the next time Siphon starts.")
    return 0


# ---------------------------------------------------------------- helpers


def _request(url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"Siphon/{__version__}",
                                                "X-GitHub-Api-Version": "2022-11-28"})


def _registered_location() -> str:
    """Where the Windows installer put Siphon, from its uninstall entry; "" when it never ran."""
    if not paths.windows():
        return ""
    import winreg

    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):  # per user, or for all users
        try:
            with winreg.OpenKey(root, key) as entry:
                return str(winreg.QueryValueEx(entry, "InstallLocation")[0])
        except OSError:
            continue
    return ""


def _same_folder(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _remove_old(folder: Path, keep: str = "") -> None:
    """Installers other than keep and unfinished downloads. One Windows still holds open (the installer
    running now) stays until the next time."""
    for old in folder.iterdir():
        if old.name != keep and (old.suffix == ".part" or old.name.startswith("Siphon-") and old.suffix == ".exe"):
            try:
                old.unlink()
            except OSError:
                pass


def _left_alone(version: str, reason: str) -> str:
    return f"Siphon {version} is out, but this copy is left as it is: {reason}."


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # GIT_DIR and the like would aim git at another repository; nobody is there to answer a prompt; and
    # without optional locks even `git status` leaves the index of a checkout someone works in alone.
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env |= {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env,
                              timeout=300, creationflags=paths.no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"git {args[0]} failed ({e})") from None


def _git_ok(repo: Path, *args: str) -> str:
    done = _git(repo, *args)
    if done.returncode:
        lines = done.stderr.strip().splitlines() or [f"exit status {done.returncode}"]
        raise UpdateError(f"git {args[0]} failed ({lines[-1]})")
    return done.stdout.strip()


def _is_ancestor(repo: Path, older: str, newer_commit: str) -> bool:
    done = _git(repo, "merge-base", "--is-ancestor", older, newer_commit)
    if done.returncode not in (0, 1):
        raise UpdateError(f"git merge-base failed ({done.stderr.strip()})")
    return done.returncode == 0


def _marker(repo: Path) -> Path:
    return Path(_git_ok(repo, "rev-parse", "--absolute-git-dir")) / MARKER


def _fast_forward(repo: Path, version: str, commit: str) -> str:
    # Checked again: the clone may have changed since the last run fetched the release.
    if reason := git_blocker(repo):
        return _left_alone(version, reason)
    old = _git_ok(repo, "rev-parse", "HEAD")
    if _is_ancestor(repo, commit, old):
        return f"Siphon {version} is already installed"
    if not _is_ancestor(repo, old, commit):
        return _left_alone(version, "it has commits the release does not")
    _git_ok(repo, "merge", "--ff-only", "--quiet", commit)
    if _git(repo, "diff", "--quiet", old, commit, "--", "install.sh").returncode:
        # install.sh is safe to run again; its pip line is the venv's dependency list
        try:
            done = subprocess.run(["bash", str(repo / "install.sh")], capture_output=True, text=True, timeout=900)
            lines = done.stderr.strip().splitlines() or [f"exit status {done.returncode}"]
            failure = lines[-1] if done.returncode else ""
        except (OSError, subprocess.TimeoutExpired) as e:
            failure = str(e)
        if failure:
            _git_ok(repo, "reset", "--quiet", "--keep", old)  # back to the code the venv still matches
            return f"Siphon {version} was not installed: its install.sh failed ({failure})"
    return f"installed Siphon {version}"


if __name__ == "__main__":
    # bin/siphon: `python -m siphon.updater apply` when the last run fetched an update, before Siphon starts
    if sys.argv[1:] != ["apply"]:
        sys.exit("usage: python -m siphon.updater apply")
    try:
        print(f"siphon: {apply(checkout())}", file=sys.stderr)
    except UpdateError as e:
        print(f"siphon: the update was not installed: {e}", file=sys.stderr)
