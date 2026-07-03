#!/bin/bash
# One-shot installer for rgbpi_mediaplayer on RePlayOS (RGB-Pi 2 + 15 kHz CRT).
#
# Usage (as root, on the Pi):
#   git clone --depth 1 https://github.com/thecyril/rgbpi_mediaplayer.git /opt/rgbpi_mediaplayer
#   /opt/rgbpi_mediaplayer/deploy/replayos/install.sh
#   reboot
#
# Idempotent: safe to re-run after a git pull. Never touches state/ (prefs,
# credentials, bookmarks) on an existing install. A reboot is required after
# the first run (EDID firmware override is read at boot).
set -eu

APP_TARGET="/opt/rgbpi_mediaplayer"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
EDID_DIR="/lib/firmware/edid"
CMDLINE="/boot/firmware/cmdline.txt"
CORES_CFG="/opt/replay/cores/cores.cfg"

say() { printf '\n=== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -e /opt/replay/replay ] || echo "WARNING: /opt/replay/replay not found — is this RePlayOS? Continuing anyway."

say "1/7 apt dependencies"
apt-get install -y --no-install-recommends python3-pygame python3-requests i2c-tools gcc >/dev/null
echo "ok"

say "2/7 app files -> $APP_TARGET"
if [ "$REPO_DIR" = "$APP_TARGET" ]; then
    echo "repo already lives at $APP_TARGET"
else
    # Copy with tar to preserve the bundle's 238 .so symlinks; keep any
    # existing state/ (prefs, credentials) and skip the git metadata.
    mkdir -p "$APP_TARGET"
    tar -C "$REPO_DIR" --exclude=.git --exclude=state -cf - . | tar -C "$APP_TARGET" -xf -
    echo "copied from $REPO_DIR"
fi

say "3/7 bundle libs (SONAME symlinks for mpv, host runtime kept clean)"
sh "$APP_TARGET/deploy/replayos/fix_bundle_libs.sh" "$APP_TARGET"

say "4/7 launcher"
install -m 0755 "$APP_TARGET/deploy/replayos/replay_launch.sh" "$APP_TARGET/replay_launch.sh"
echo "ok: $APP_TARGET/replay_launch.sh"

say "5/7 EDID firmware override (wide 15 kHz modes)"
EDID_SRC=""
for f in /sys/class/drm/card*-HDMI-A-1/edid; do
    # sysfs attributes stat as size 0 — probe by actually reading.
    if [ -n "$(head -c 8 "$f" 2>/dev/null | tr -d '\0')" ]; then EDID_SRC="$f"; break; fi
done
[ -n "$EDID_SRC" ] || die "no EDID on HDMI-A-1 — is the RGB-Pi 2 plugged into HDMI0?"
# Reuse the filename already referenced in cmdline.txt (idempotent), else add one.
EDID_NAME="$(grep -o 'drm\.edid_firmware=HDMI-A-1:edid/[^ ]*' "$CMDLINE" 2>/dev/null | sed 's|.*edid/||' || true)"
[ -n "$EDID_NAME" ] || EDID_NAME="rgbpi_crt.bin"
mkdir -p "$EDID_DIR"
python3 "$APP_TARGET/deploy/replayos/build_edid.py" "$EDID_SRC" "$EDID_DIR/$EDID_NAME"
if ! grep -q 'drm\.edid_firmware=' "$CMDLINE"; then
    cp "$CMDLINE" "$CMDLINE.pre-rgbpi-mediaplayer"
    sed -i "s|\$| drm.edid_firmware=HDMI-A-1:edid/$EDID_NAME|" "$CMDLINE"
    echo "cmdline.txt updated (backup: $CMDLINE.pre-rgbpi-mediaplayer)"
else
    echo "cmdline.txt already set"
fi

say "6/7 RePlay menu entry (stub core on the audio_video_test slot)"
gcc -O2 -shared -fPIC -o /opt/replay/cores/rgbpi_mediaplayer_libretro.so \
    "$APP_TARGET/deploy/replayos/rgbpi_mediaplayer_libretro.c"
[ -f "$CORES_CFG.orig" ] || cp "$CORES_CFG" "$CORES_CFG.orig"
# The Extra entry itself is the stock 0-byte audio_video_test.lr (only the
# hardcoded NAME matters to replay's game_launcher); recreate it if absent.
[ -e /opt/replay/extra/audio_video_test.lr ] || : > /opt/replay/extra/audio_video_test.lr
python3 - "$CORES_CFG" <<'EOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
STUB = "rgbpi_mediaplayer_libretro.so"
def repl(match):
    body = re.sub(r'"(?:[^"]*)"', f'"{STUB}"', match.group(2))
    return match.group(1) + body
new, n = re.subn(r'(\[avtest\]\n)((?:\s*\w+\s*=\s*"[^"]*"\n)+)', repl, text)
if n == 0:
    # No [avtest] section in this RePlayOS version: append one.
    new = text.rstrip("\n") + f'\n\n[avtest]\nlow = "{STUB}"\nmid = "{STUB}"\nhi = "{STUB}"\n'
if new != text:
    open(path, "w").write(new)
# Verify: the [avtest] section must now reference the stub.
section = re.search(r'\[avtest\]\n(?:.*\n)*?(?=\[|\Z)', open(path).read())
assert section and STUB in section.group(0), "cores.cfg patch failed"
print(f"cores.cfg: [avtest] -> {STUB}" + ("" if n else " (section added)"))
EOF

say "7/7 default player prefs (audio out = HDMI / RGB-Pi 2 jack)"
PREFS="$APP_TARGET/state/playback_prefs.json"
if [ -f "$PREFS" ]; then
    echo "existing prefs kept (switch in-app: SETTINGS -> AUDIO OUTPUT -> HDMI)"
else
    mkdir -p "$APP_TARGET/state"
    printf '{\n  "audio_output": "hdmi"\n}\n' > "$PREFS"
    echo "seeded $PREFS"
fi

say "DONE — reboot now. Then select \"audio_video_test\" in RePlay's Extra menu to start the player."
