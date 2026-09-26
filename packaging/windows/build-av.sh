#!/usr/bin/env bash
# The Windows build's audio stack, built from source into <prefix>/bin: ffmpeg and ffprobe (shared
# libraries with FFmpeg's own codecs plus Opus, LAME and Vorbis, no video encoders or hardware APIs) and
# libmpv-2.dll (audio only: no video outputs, libplacebo linked in).
# MSYS2's ready-made ffmpeg and mpv packages would bring ~400 MB of video and AI libraries (x265, aom,
# rav1e, whisper, OpenBLAS, shaderc...) that a music app never loads. Runs in an MSYS2 UCRT64 shell;
# the workflow caches the prefix.
#   packaging/windows/build-av.sh <prefix> <scratch dir>
set -euo pipefail

FFMPEG=9.0.2
FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
MPV=0.41.0
MPV_SHA256=ee21092a5ee427353392360929dc64645c54479aefdb5babc5cfbb5fad626209
LIBPLACEBO=v7.360.1
LIBPLACEBO_COMMIT=cee9b076f2c63104ccfd497fa79c39a867293ec4

prefix="$(realpath -m "$1")"
work="$(realpath -m "$2")"
mkdir -p "$prefix" "$work"

unpack() {  # url sha256
    local archive="$work/$(basename "$1")"
    curl -fsSL -o "$archive" "$1"
    echo "$2  $archive" | sha256sum -c --quiet -
    tar -xf "$archive" -C "$work"
}

unpack "https://ffmpeg.org/releases/ffmpeg-$FFMPEG.tar.xz" "$FFMPEG_SHA256"
(
    cd "$work/ffmpeg-$FFMPEG"
    # Encoders: what yt-dlp's conversions, Siphon's cover crop and the selftest's tone use.
    ./configure --prefix="$prefix" --enable-shared --disable-static --disable-autodetect \
        --enable-w32threads --enable-zlib --enable-schannel \
        --enable-libopus --enable-libmp3lame --enable-libvorbis \
        --disable-doc --disable-ffplay --disable-debug \
        --disable-encoders \
        --enable-encoder=aac,alac,flac,libmp3lame,libopus,libvorbis,mjpeg,png,pcm_s16le,pcm_s24le,wrapped_avframe \
        --disable-indevs --enable-indev=lavfi --disable-outdevs
    make -j"$(nproc)"
    make install
)

unpack "https://github.com/mpv-player/mpv/archive/refs/tags/v$MPV.tar.gz" "$MPV_SHA256"
placebo="$work/mpv-$MPV/subprojects/libplacebo"
git clone -q --depth 1 --branch "$LIBPLACEBO" --recurse-submodules --shallow-submodules \
    https://code.videolan.org/videolan/libplacebo.git "$placebo"
[[ "$(git -C "$placebo" rev-parse HEAD)" == "$LIBPLACEBO_COMMIT" ]]
(
    cd "$work/mpv-$MPV"
    # Every optional feature off, then back on only what an audio player on Windows needs (Lua only because
    # Siphon's player turns mpv's youtube-dl hook off by name, and that option exists only with Lua).
    PKG_CONFIG_PATH="$prefix/lib/pkgconfig" meson setup build --prefix="$prefix" --buildtype=release --strip \
        --force-fallback-for=libplacebo -Dauto_features=disabled -Dgl=disabled \
        -Dlibmpv=true -Dcplayer=false -Dtests=false \
        -Dlua=enabled -Dlibavdevice=enabled -Dwasapi=enabled -Dwin32-threads=enabled -Dzlib=enabled
    meson compile -C build
    meson install -C build
)
ls -l "$prefix/bin"
