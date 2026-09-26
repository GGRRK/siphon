#!/usr/bin/env bash
# Install Siphon for the current user (no sudo). Safe to run again.
#   install.sh             install or repair
#   install.sh update      upgrade yt-dlp only
#   install.sh uninstall   remove what install.sh added (never your downloads)
#   --dry-run              print the actions instead of running them
set -euo pipefail

repo="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
share="$HOME/.local/share"
venv="$share/siphon/venv"
link="$HOME/.local/bin/siphon"
app_id="io.github.ggrrk.Siphon"
desktop_dir="$share/applications"
icon_theme="$share/icons/hicolor"
icon_dir="$icon_theme/scalable/apps"

action=install
dry=0
for arg in "$@"; do
    case "$arg" in
        install | update | uninstall) action="$arg" ;;
        --dry-run) dry=1 ;;
        -h | --help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install.sh: unknown argument '$arg' (try --help)" >&2; exit 2 ;;
    esac
done

run() {
    if ((dry)); then
        printf '+'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

check_tools() {
    local missing=()
    command -v ffmpeg >/dev/null || missing+=(ffmpeg)
    command -v node >/dev/null || missing+=(nodejs)
    python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' \
        2>/dev/null || missing+=(python-gobject gtk4 libadwaita)
    # the player loads libmpv the way python-mpv does; on Arch it comes with the mpv package
    python3 -c 'import ctypes.util, sys; sys.exit(ctypes.util.find_library("mpv") is None)' || missing+=(mpv)
    if ((${#missing[@]})); then
        echo "Missing: ${missing[*]} - install with: sudo pacman -S --needed ${missing[*]}"
    fi
}

refresh_caches() {
    if command -v update-desktop-database >/dev/null; then
        run update-desktop-database -q "$desktop_dir" || true
    fi
    if command -v gtk-update-icon-cache >/dev/null && [[ -d "$icon_theme" ]]; then
        run gtk-update-icon-cache -q -t -f "$icon_theme" || true
    fi
}

install_data() {  # source file, target dir, what
    if [[ -f "$repo/data/$1" ]]; then
        run install -Dm644 "$repo/data/$1" "$2/$1"
    else
        echo "Skipped the $3: data/$1 is not in the repo yet."
    fi
}

engine_version() {
    "$venv/bin/python" -c 'from yt_dlp.version import __version__; print(__version__)'
}

case "$action" in
install)
    check_tools
    if [[ ! -x "$venv/bin/python" ]]; then
        run mkdir -p "$(dirname "$venv")"
        # System site-packages give the venv the distro's PyGObject (GTK 4 + libadwaita).
        run python3 -m venv --system-site-packages "$venv"
    fi
    run "$venv/bin/python" -m pip install --quiet --disable-pip-version-check "yt-dlp[default]" mutagen mpv
    run mkdir -p "$(dirname "$link")"
    run ln -sfn "$repo/bin/siphon" "$link"
    install_data "$app_id.desktop" "$desktop_dir" "desktop entry"
    # the entry names the launcher by full path: a desktop session's PATH often lacks ~/.local/bin (measured 2026-09-25 on
    # Hyprland/uwsm), and then the launcher entry silently does nothing
    [[ -f "$desktop_dir/$app_id.desktop" ]] && run sed -i "s|^Exec=siphon |Exec=$link |" "$desktop_dir/$app_id.desktop"
    install_data "$app_id.svg" "$icon_dir" "icon"
    refresh_caches
    ((dry)) || echo "Siphon is installed (yt-dlp $(engine_version)). Run: siphon"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) echo "Note: $HOME/.local/bin is not on your PATH, so type $link or add it to PATH." ;;
    esac
    ;;
update)
    if [[ ! -x "$venv/bin/python" ]]; then
        echo "Siphon is not installed - run install.sh first." >&2
        exit 1
    fi
    run "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "yt-dlp[default]"
    ((dry)) || echo "yt-dlp is now $(engine_version)."
    ;;
uninstall)
    if [[ -L "$link" && "$(readlink -f "$link")" == "$repo/bin/siphon" ]]; then
        run rm -f "$link"
    fi
    for file in "$desktop_dir/$app_id.desktop" "$icon_dir/$app_id.svg"; do
        if [[ -e "$file" ]]; then run rm -f "$file"; fi
    done
    if [[ -d "$venv" ]]; then
        run rm -rf "$venv"
        run rmdir --ignore-fail-on-non-empty "$(dirname "$venv")"
    fi
    refresh_caches
    ((dry)) || echo "Siphon is uninstalled. Downloaded music was left where it is."
    ;;
esac
