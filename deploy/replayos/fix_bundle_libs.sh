#!/bin/sh
# Prepare the bundled bullseye rootfs libs for the bundled mpv on RePlayOS.
#
# The bundle ships .so.X.Y.Z files without their .so.X SONAME symlinks, so
# bin/mpv fails to load (libuchardet.so.0, libavcodec.so.58, ...). Running
# ldconfig creates them — but it ALSO creates symlinks for the bundled core
# runtime (glibc, libstdc++, libasound), and those must NOT exist:
#  - anything running with these dirs in LD_LIBRARY_PATH would load the OLD
#    bullseye glibc and crash on a trixie host (bash: GLIBC_2.3x not found);
#  - the bundled libasound reads the HOST ALSA configs and cannot (symbol
#    version check fails) -> mpv must use the host libasound instead.
#
# libSDL2 must STAY exposed in the bundle: Debian's mpv links libSDL2-2.0,
# and without the bundle copy it resolves the HOST SDL2 (trixie), which pulls
# host libdecor-0 needing wl_proxy_marshal_flags from wayland >= 1.20 — while
# LD_LIBRARY_PATH serves the bundle's wayland 1.18 -> mpv dies with a symbol
# lookup error before its IPC comes up. The bundle SDL is harmless to the
# system python/pygame since the rootfs dirs are exported to the mpv child
# only (DVDPLAYER_RUNTIME_LIBS=mpv-only in the launcher).
#
# Run once after deploying /opt/rgbpi_mediaplayer. Idempotent.
set -eu
APP_DIR="${1:-/opt/rgbpi_mediaplayer}"
R="$APP_DIR/runtime/linux-arm64-rootfs/usr/lib/aarch64-linux-gnu"
[ -d "$R" ] || { echo "no bundle rootfs at $R" >&2; exit 1; }

ldconfig -n "$R" "$R/pulseaudio" "$R/samba" 2>/dev/null || true

cd "$R"
# Remove the SONAME symlinks that would shadow the host core runtime.
for name in libc.so libm.so libmvec.so libpthread.so libdl.so libutil.so \
            librt.so libanl.so libresolv.so libnsl.so libnss_ \
            libthread_db.so libBrokenLocale.so libcrypt.so ld-linux \
            libgcc_s.so libstdc++.so; do
  find . -maxdepth 1 -type l -name "${name}*" -delete
done
# mpv must use the HOST libasound (coherent with the host ALSA configs).
for f in libasound.so.2 libasound.so.2.0.0; do
  [ -e "$f" ] && mv -f "$f" "$f.disabled"
done
echo "bundle libs fixed for mpv-only use"
