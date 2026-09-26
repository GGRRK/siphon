# Siphon

Paste a link, get the audio - then listen to it: a library of your music folder, playlists and a built-in player. A small GTK4/libadwaita app for Arch + Hyprland (works on any GNOME-ish Linux desktop) and for Windows 10/11.

- **YouTube, YouTube Music, SoundCloud** and anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) reads: the audio track is downloaded directly.
- **Spotify tracks, albums and playlists**: Spotify's own audio is DRM-protected, so Siphon reads the song details from the link (title, artist, album, cover) and downloads the same song from YouTube Music / YouTube, rejecting remixes, live versions, covers and wrong lengths. Files get Spotify's tags and cover.
- **Apple Music, Deezer, Tidal share links**: same idea, from the page's title tags.

Formats: Opus (the default: YouTube's own audio, no re-encode), M4A (AAC, no re-encode when the source is AAC), MP3 (VBR V0), FLAC (lossless container - no quality gain over the source), or Original. Every file is tagged (title, artist, album, track number) with a square cover. Files are named `Artist - Title.ext` (on Windows, characters and device names it forbids are replaced). Default folder: `~/Music` (Windows: your Music folder).

## Install (Linux)

```bash
bash install.sh            # venv in ~/.local/share/siphon/venv (yt-dlp + mutagen + mpv), ~/.local/bin/siphon, app menu entry + icon
bash install.sh update     # upgrade yt-dlp (also: the app's menu -> Update Engine)
bash install.sh uninstall  # remove all of that (never your downloads)
```

Needs `ffmpeg`, `nodejs` (yt-dlp's JavaScript runtime for YouTube), `python-gobject`, `gtk4`, `libadwaita`, `mpv` (libmpv plays the music); the installer names any that are missing.

## Windows

Download `Siphon-<version>-Setup.exe` (about 45 MB) from the repository's **Releases** page and run it. It installs for your user only (no administrator prompt) into `%LOCALAPPDATA%\Programs\Siphon`, adds Siphon to the Start menu (a desktop shortcut is optional) and an uninstaller to Settings > Apps. `Siphon-<version>-Windows-portable.zip` is the same app without installing: unzip it anywhere and run `Siphon.exe`.

Everything comes in the box (about 145 MB installed): Python, GTK 4 and libadwaita, yt-dlp, ffmpeg and ffprobe, libmpv and QuickJS (the JavaScript engine yt-dlp needs for YouTube). Nothing else to install. `siphon-cli.exe`, next to `Siphon.exe`, is the command line: `siphon-cli get URL... [-f FORMAT] [-o DIR]`, `siphon-cli selftest [--net]`, `siphon-cli --version`.

| what | where |
|---|---|
| downloads and playlists | your Music folder (wherever Windows has it, OneDrive included) |
| settings | `%APPDATA%\Siphon` |
| library cache, cover cache | `%LOCALAPPDATA%\Siphon\cache` |
| engine updates | `%LOCALAPPDATA%\Siphon\engine` |
| log of the last session | `%LOCALAPPDATA%\Siphon\siphon.log` |

Uninstalling leaves all of these. What differs from Linux:

- **Update Engine** (in the menu) downloads the newest yt-dlp from PyPI, checks its sha256 and uses it from the next start; a newer Siphon installer brings a newer yt-dlp of its own.
- **Media keys**: Windows has no MPRIS, so media keys and the Windows media overlay don't reach Siphon.
- **YouTube's JavaScript challenges** are solved by the bundled QuickJS rather than Node.js: 2 MB instead of 90 and always the same, but it can add a few seconds to a YouTube download.
- **One window per start**: `Siphon.exe URL` opens a new window rather than queueing the link in the running one.

The Windows build is made by `.github/workflows/windows.yml` on GitHub Actions: MSYS2 (UCRT64) with PyInstaller, ffmpeg and libmpv built from source for audio only (`packaging/windows/build-av.sh`), Inno Setup for the installer, then the whole app smoke-tested with MSYS2 moved out of the way: selftest with a real download, a Spotify track, an engine update, install, the window (light and dark), uninstall.

## Use

- `siphon` opens the window (or "Siphon" in the app launcher). Paste a link, press Enter. A link on the clipboard is offered when the window gets focus; dropping a link on the window queues it too. Two downloads run at once, the rest wait.
- A playlist link (Spotify, YouTube, YouTube Music, SoundCloud…) becomes a playlist of the same name, with the playlist's picture, as soon as it is read; each song joins it as it finishes, in the playlist's order. Pasting the link again (in any form: `spotify:` URI, `?si=`, music.youtube.com) adds only what is new to the same playlist.
- `siphon URL...` queues links in the running window (or opens it).
- `siphon get URL... [-f mp3|m4a|opus|flac|best] [-o DIR]` downloads in the terminal; a playlist link also writes its playlist into `DIR/Playlists`, the same way.
- `siphon selftest [--net]` checks ffmpeg, ffprobe, the JavaScript runtime and libmpv, then makes, tags, scans, lists and silently plays a 2 s tone (`--net` also downloads a YouTube music video and has the JavaScript runtime solve its challenge); exit status 0 when everything works. `siphon --version` prints Siphon's and yt-dlp's versions.

## Listen

The music folder is the download folder (`~/Music` unless you change it under Save To).

- **Library**: every song in the music folder and its subfolders, the files you already had included. It is remembered between runs (`~/.cache/siphon/library.json`; Windows: `%LOCALAPPDATA%\Siphon\cache`), so it shows at once when Siphon opens, while a rescan picks up what changed. New downloads appear as they finish. Search matches title, artist and album, ignoring case and accents; sort by Recently Added, Title or Artist. F5 (or Refresh Library in the menu) rescans.
- **Song menu** (the ⋮ button or a right-click): Play Next, Add to Queue, Add to Playlist, Show in Folder, Move to Trash (also takes the song out of every playlist).
- **Playlists** live in `~/Music/Playlists` as `.m3u8` files with paths relative to that folder, so other players (mpv, VLC, Strawberry…) open them, and playlists they save there show up in Siphon. Create, rename, add songs, reorder (drag, or Move Up/Down), remove, delete (to the Trash). A song whose file is gone stays listed, dimmed, and is skipped. A downloaded album has a **Save as Playlist** button, which keeps the album cover as the playlist's picture: `Name.jpg` beside `Name.m3u8`, named in its `#EXTIMG` line.
- **Player** (libmpv, gapless): the now-playing bar has seek, shuffle, repeat (off, all, one), volume and Up Next. Space plays or pauses, Ctrl+Left/Right skip, Ctrl+F searches the library. A file that can't play shows a message and the next song starts. Volume, shuffle, repeat, sort and the last page are remembered.
- **Media keys and bars**: Siphon speaks MPRIS on the session bus as `siphon`, so media keys that run `playerctl` and bar widgets show and control it, cover art included (`playerctl --player=siphon play-pause`). Windows has no MPRIS, so media keys don't reach Siphon there yet.

When downloads start failing with "YouTube refused the download", run Update Engine: YouTube changes often and yt-dlp follows within days. From source it upgrades yt-dlp with pip; a packaged build downloads the newest yt-dlp wheel from PyPI, checks its sha256 and uses it from the next start. Siphon already retries refused downloads with a second YouTube client.

## Layout

| path | what |
|---|---|
| `siphon/core.py` | the contract the window uses: `resolve(url)`, `download(track, outdir, fmt, progress, cancel)`, tagging, engine update |
| `siphon/spotify.py` | Spotify link parsing + the public embed page (no API key) |
| `siphon/match.py` | finding the right YouTube upload for a song (duration, title/artist tokens, official "Topic" channels) |
| `siphon/library.py`, `siphon/playlists.py`, `siphon/covers.py` | the music folder: cached tag scan and search, M3U8 playlists, embedded covers |
| `siphon/player.py`, `siphon/mpris.py` | playback through libmpv (queue, shuffle, repeat) and its MPRIS face |
| `siphon/app.py`, `siphon/ui/` | the libadwaita window |
| `siphon/paths.py`, `siphon/names.py` | per-platform folders and start-up (a packaged build's `bin/`), Windows file-name rules |
| `siphon/engine.py` | engine updates for packaged builds: verified yt-dlp wheels from PyPI |
| `siphon/cli.py`, `siphon/selftest.py` | `siphon get`, `siphon selftest` |
| `packaging/windows/` | the Windows build: PyInstaller spec, audio-stack build script, Inno Setup script, smoke test |
| `tests/` | `pytest` (offline, fixtures) and `tests/live_check.py` (real downloads, ffprobe checks) |

Dev: `python -m venv --system-site-packages .venv && .venv/bin/pip install "yt-dlp[default]" mutagen mpv pytest`, then `.venv/bin/python -m pytest -q`. `SIPHON_FAKE_CORE=1` runs the window against a fake engine and a generated library, `SIPHON_FAKE_PLAYER=1` against a silent timer-driven player, `SIPHON_NO_MPRIS=1` without MPRIS; `SIPHON_AO=null` gives libmpv a silent audio output (the player tests set it).

For personal use: download only what you have the right to keep.
