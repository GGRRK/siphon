# PyInstaller spec of the Windows build, run from an MSYS2 UCRT64 shell (.github/workflows/windows.yml)
# once packaging/windows/build-av.sh has built ffmpeg and libmpv into build/av and QuickJS is in build/tools:
#   PATH="build/av/bin:$PATH" pyinstaller --noconfirm packaging/windows/siphon.spec
# Siphon.exe (the window) and siphon-cli.exe (its console twin) share one folder. Everything else lands
# in bin/, the folder Siphon puts first on PATH (siphon/paths.py): ffmpeg, ffprobe, qjs and libmpv then
# share one copy of every DLL with GTK and Python, and no MSYS2 path is needed at run time.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

HERE = Path(SPECPATH)
ROOT = HERE.parent.parent
AV = ROOT / "build" / "av" / "bin"  # ffmpeg, ffprobe and libmpv from build-av.sh
QJS = ROOT / "build" / "tools" / "qjs.exe"  # QuickJS, which solves YouTube's challenges for yt-dlp
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "siphon" / "__init__.py").read_text())[1]


def tool(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise SystemExit(f"siphon.spec: {path} is missing")
    return str(path), "."


def wanted(dest: str) -> bool:
    """Drop what a Windows build never uses: X cursors (Windows draws its own) and translations."""
    dest = dest.replace("\\", "/")
    return not (dest.startswith("share/icons/Adwaita/cursors/") or dest.startswith("share/locale/"))


def version_info(description: str, filename: str) -> VSVersionInfo:
    numbers = tuple(int(n) for n in VERSION.split(".")) + (0,)
    strings = {"CompanyName": "GGRRK", "FileDescription": description, "FileVersion": VERSION,
               "InternalName": filename, "OriginalFilename": filename, "ProductName": "Siphon",
               "ProductVersion": VERSION}
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings.items()])]),
              VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])


a = Analysis(
    [str(HERE / "siphon_main.py")],
    pathex=[str(ROOT)],
    binaries=[tool(AV / "ffmpeg.exe"), tool(AV / "ffprobe.exe"), tool(AV / "libmpv-2.dll"), tool(QJS)],
    datas=[(str(ROOT / "siphon" / "ui" / "style.css"), "siphon/ui"),
           (str(ROOT / "data" / "io.github.ggrrk.Siphon.svg"), "data"),
           (str(HERE / "siphon.ico"), "data"),  # the tray icon (siphon/wintray.py)
           *copy_metadata("yt-dlp"),  # engine.py compares a downloaded engine with the bundled version
           *collect_data_files("yt_dlp_ejs")],  # the YouTube challenge solver's JavaScript
    hiddenimports=["siphon.cli", "siphon.selftest", "siphon.updater", "siphon.app", "siphon.wintray"],
    # yt-dlp as .pyc files beside the rest, not inside the archive each of the two .exe files carries: 7 MB less
    module_collection_mode={"yt_dlp": "pyc"},
    hooksconfig={"gi": {"module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0"},
                        "icons": ["Adwaita", "hicolor"], "themes": [], "languages": []}},
    excludes=["tkinter", "test", "idlelib", "lib2to3", "pydoc_data", "pytest", "_pytest", "pip"],
)
a.datas = [entry for entry in a.datas if wanted(entry[0])]
pyz = PYZ(a.pure)

# UTF-8 mode, as on Linux: text files, pipes and the console all speak UTF-8 instead of the ANSI code page.
utf8 = [("X utf8", None, "OPTION")]
common = dict(exclude_binaries=True, contents_directory="bin", icon=str(HERE / "siphon.ico"), upx=False)
window = EXE(pyz, a.scripts, utf8, name="Siphon", console=False,
             version=version_info("Siphon", "Siphon.exe"), **common)
console = EXE(pyz, a.scripts, utf8, name="siphon-cli", console=True,
              version=version_info("Siphon command line", "siphon-cli.exe"), **common)
COLLECT(window, console, a.binaries, a.datas, name="Siphon", upx=False)
