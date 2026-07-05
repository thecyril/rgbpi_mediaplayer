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
CONFIG_TXT="/boot/firmware/config.txt"
CORES_CFG="/opt/replay/cores/cores.cfg"
# Firmware early-boot HDMI timing: 1920x240 @ 39.15 MHz / 2500 total = 15.66 kHz
# H, 60 Hz V. Native 1920 width (the firmware clamps the boot framebuffer to
# 1920) so the scanned raster equals the framebuffer — simplefb=1920x240 in
# dmesg is direct proof of the 15.66 kHz output.
FW_HDMI_TIMINGS="1920 0 80 200 300 240 0 3 3 15 0 0 0 60 0 39150000 1"

say() { printf '\n=== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

# ---------------------------------------------------------------------------
# install.sh --check : post-reboot verification (read-only + one i2c probe)
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--check" ]; then
    ok=1
    check() { # <label> <command...>
        label="$1"; shift
        if "$@" >/dev/null 2>&1; then printf 'OK   %s\n' "$label"; else printf 'FAIL %s\n' "$label"; ok=0; fi
    }
    modes="$(cat /sys/class/drm/card*-HDMI-A-1/modes 2>/dev/null || true)"
    check "EDID mode 2560x240 (UI / 60 Hz)"          sh -c "echo '$modes' | grep -q 2560x240"
    check "EDID mode 2560x288 (PAL / 50 Hz)"          sh -c "echo '$modes' | grep -q 2560x288"
    # SAFETY: the kernel must expose NOTHING but the two 15 kHz modes — any
    # other mode is a >15.7 kHz signal a client could send to the CRT.
    hi="$(echo "$modes" | grep -vE '^(2560x240|2560x288)?$' | grep -v '^$' || true)"
    check "kernel exposes ONLY 15 kHz modes"          sh -c "test -z \"$hi\""
    [ -n "$hi" ] && printf '     !! extra modes present: %s\n' "$(echo "$hi" | tr '\n' ' ')"
    # SAFETY: the firmware early boot must be forced to 15 kHz too.
    check "firmware boot forced to 15 kHz (config.txt)" grep -q '15 kHz CRT SAFETY' "$CONFIG_TXT"
    fbmode="$(dmesg 2>/dev/null | grep -oE 'simple-framebuffer.*mode=[0-9]+x[0-9]+' | grep -oE '[0-9]+x[0-9]+' | tail -1)"
    check "firmware boot framebuffer = 1920x240"      sh -c "test '$fbmode' = '1920x240'"
    [ -n "$fbmode" ] && [ "$fbmode" != "1920x240" ] && printf '     !! firmware boot mode is %s (expected 1920x240)\n' "$fbmode"
    check "cmdline.txt EDID override"                 grep -q 'drm\.edid_firmware=' "$CMDLINE"
    # GROUND TRUTH: the H-frequency of what is being scanned out RIGHT NOW
    # (clock/htotal from the live KMS state) — catches ANY misconfiguration,
    # including RePlay itself being switched to an LCD/31 kHz profile.
    live="$(grep 'mode:' /sys/kernel/debug/dri/*/state 2>/dev/null | grep -v '\"\"' | head -1)"
    livekhz="$(echo "$live" | awk '{for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) {clk=$(i+1); htot=$(i+5); break}} END {if (htot>0) printf "%.2f", clk/htot; else print "none"}')"
    if [ "$livekhz" = "none" ] || [ -z "$livekhz" ]; then
        printf 'OK   LIVE output: none active (no signal at all)\n'
    else
        check "LIVE output H-freq = $livekhz kHz (15-16 kHz)" awk "BEGIN{exit !($livekhz <= 16.05 && $livekhz >= 15.0)}"
    fi
    check "launcher installed"                        test -x "$APP_TARGET/replay_launch.sh"
    check "stub core loads (libretro API)"            python3 -c "import ctypes; assert ctypes.CDLL('/opt/replay/cores/rgbpi_mediaplayer_libretro.so').retro_api_version()==1"
    # Loads mpv with the exact library environment the player gives it —
    # catches any bundle-lib regression (missing SONAME, host/bundle mixing).
    RT="$APP_TARGET/runtime/linux-arm64-rootfs"
    MPV_LDP="$RT/lib/aarch64-linux-gnu:$RT/usr/lib/aarch64-linux-gnu:$RT/usr/lib/aarch64-linux-gnu/pulseaudio:$RT/usr/lib/aarch64-linux-gnu/samba:$APP_TARGET/lib"
    check "bundled mpv loads (bundle libs)"           env LD_LIBRARY_PATH="$MPV_LDP" "$APP_TARGET/bin/mpv" --version
    check "cores.cfg [alpha_player] -> stub"          sh -c "grep -A3 '\[alpha_player\]' '$CORES_CFG' | grep -q rgbpi_mediaplayer_libretro"
    check "Alpha Player launcher entry"               test -e "/media/sd/roms/alpha_player/MEDIA PLAYER.mkv"
    check "Alpha Player tile enabled (view_player)"   grep -q "view_player *= *\"true\"" /media/sd/config/replay.cfg
    check "vc4-hdmi sound card"                       grep -q vc4hdmi /proc/asound/cards
    # During a player session the launcher intentionally pauses the system
    # service and runs its own watchdog (a subshell of replay_launch.sh).
    if pgrep -f "[d]vdplayer_python.main" >/dev/null 2>&1; then
        check "csync watchdog (launcher, player session)" pgrep -f "[r]eplay_launch.sh"
    else
        check "csync watchdog service active"         systemctl is-active --quiet rgbpi-csync
    fi
    modprobe -q i2c-dev 2>/dev/null || true
    dac=""
    for dev in /dev/i2c-*; do
        b="${dev#/dev/i2c-}"
        i2cget -y -a "$b" 0x78 >/dev/null 2>&1 && { dac="$b"; break; }
    done
    check "RGB-Pi 2 DAC answers on i2c (bus ${dac:-?})" test -n "$dac"
    # LOAD-BEARING: the csync MODE (page 4, 0xB5) and STATUS (page 0, 0x61)
    # decide whether the picture holds — a wrong mode scrolls the CRT even at a
    # correct 15 kHz H-freq. Status 0xff means "locked", NOT "right mode", so
    # verify both. Desired mode follows replay.cfg video_crt_csync_mode
    # (0=AND 0x06, 1=XOR 0x0C, 2=separated 0x00).
    if [ -n "$dac" ]; then
        want=0x06
        case "$(sed -n 's/^video_crt_csync_mode *= *"\([0-9]\)".*/\1/p' /media/sd/config/replay.cfg 2>/dev/null | head -1)" in
            1) want=0x0c ;; 2) want=0x00 ;;
        esac
        i2cset -y -a "$dac" 0x78 0x00 0x04 2>/dev/null
        b5="$(i2cget -y -a "$dac" 0x78 0xB5 2>/dev/null)"
        i2cset -y -a "$dac" 0x78 0x00 0x00 2>/dev/null
        s61="$(i2cget -y -a "$dac" 0x78 0x61 2>/dev/null)"
        check "csync mode 0xB5 = $b5 (want $want)"    sh -c "test \"$(echo $b5 | tr A-F a-f)\" = \"$(echo $want | tr A-F a-f)\""
        check "csync status 0x61 = $s61 (locked 0xff)" sh -c "test \"$s61\" = \"0xff\""
    fi
    # Opt-in core-option filters: if enabled, verify the RePlay binary is patched.
    if [ -f "$APP_TARGET/state/.show_all_core_options" ]; then
        check "core-option filters revealed (RePlay patched)" \
            python3 - /opt/replay/replay <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
# should_show_core_option @ 0x2c86e0; patched entry = "mov w0,#1; ret".
e_phoff = struct.unpack("<Q", d[0x20:0x28])[0]
sz = struct.unpack("<H", d[0x36:0x38])[0]; n = struct.unpack("<H", d[0x38:0x3a])[0]
off = None
for i in range(n):
    p = d[e_phoff+i*sz:e_phoff+(i+1)*sz]
    if struct.unpack("<I", p[:4])[0] == 1:
        o, v, _, fs = struct.unpack("<QQQQ", p[8:40])
        if v <= 0x2c86e0 < v+fs: off = o + (0x2c86e0 - v)
assert off is not None and d[off:off+8] == bytes.fromhex("20008052c0035fd6")
PY
    fi
    if [ "$ok" = 1 ]; then
        echo; echo "All good — open \"Alpha Player\" in RePlay's main menu and pick MEDIA PLAYER."
    else
        echo; echo "Some checks failed — see deploy/replayos/README.md (Diagnostics)."; exit 1
    fi
    exit 0
fi

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
if [ ! -e /opt/replay/replay ] && [ "$FORCE" != 1 ]; then
    die "/opt/replay/replay not found — this does not look like RePlayOS.
       Running elsewhere would wire a 15 kHz EDID into cmdline.txt and break
       a normal display. Re-run with --force only if you know what you do."
fi

say "1/9 apt dependencies"
# Appliance OS: never upgrade anything. Only touch apt when a package is
# actually missing, and even then forbid upgrades of what is already there
# (--no-upgrade fails loudly instead of silently bumping system libs — a
# previous kernel bump through apt is what renumbered the i2c buses...).
MISSING=""
for pkg in python3-pygame python3-requests i2c-tools gcc; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
    echo "installing:$MISSING"
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y --no-upgrade --no-install-recommends $MISSING >/dev/null
else
    echo "all present — apt untouched"
fi

say "2/9 app files -> $APP_TARGET"
if [ "$REPO_DIR" = "$APP_TARGET" ]; then
    echo "repo already lives at $APP_TARGET"
else
    # Copy with tar to preserve the bundle's 238 .so symlinks; keep any
    # existing state/ (prefs, credentials) and skip the git metadata.
    mkdir -p "$APP_TARGET"
    tar -C "$REPO_DIR" --exclude=.git --exclude=state -cf - . | tar -C "$APP_TARGET" -xf -
    echo "copied from $REPO_DIR"
fi

say "3/9 bundle libs (SONAME symlinks for mpv, host runtime kept clean)"
sh "$APP_TARGET/deploy/replayos/fix_bundle_libs.sh" "$APP_TARGET"

say "4/9 launcher"
install -m 0755 "$APP_TARGET/deploy/replayos/replay_launch.sh" "$APP_TARGET/replay_launch.sh"
echo "ok: $APP_TARGET/replay_launch.sh"

say "5/9 15 kHz display safety (EDID override + firmware boot timing)"
# --- 5a: cache the STOCK EDID so re-runs never re-derive from our own
# override (once drm.edid_firmware is active, /sys serves the modified EDID).
STOCK_EDID="$APP_TARGET/state/.stock_edid.bin"
mkdir -p "$APP_TARGET/state"
if [ ! -s "$STOCK_EDID" ]; then
    for f in /sys/class/drm/card*-HDMI-A-1/edid; do
        # sysfs attributes stat as size 0 — probe by actually reading.
        if [ -n "$(head -c 8 "$f" 2>/dev/null | tr -d '\0')" ]; then
            cat "$f" > "$STOCK_EDID"; break
        fi
    done
fi
[ -s "$STOCK_EDID" ] || die "no EDID on HDMI-A-1 — is the RGB-Pi 2 plugged into HDMI0?"
# --- 5b: build the 15 kHz-ONLY EDID (strips every >15.7 kHz mode) and wire it in.
EDID_NAME="$(grep -o 'drm\.edid_firmware=HDMI-A-1:edid/[^ ]*' "$CMDLINE" 2>/dev/null | sed 's|.*edid/||' || true)"
[ -n "$EDID_NAME" ] || EDID_NAME="rgbpi_crt.bin"
mkdir -p "$EDID_DIR"
python3 "$APP_TARGET/deploy/replayos/build_edid.py" "$STOCK_EDID" "$EDID_DIR/$EDID_NAME"
if ! grep -q 'drm\.edid_firmware=' "$CMDLINE"; then
    cp "$CMDLINE" "$CMDLINE.pre-rgbpi-mediaplayer"
    sed -i "s|\$| drm.edid_firmware=HDMI-A-1:edid/$EDID_NAME|" "$CMDLINE"
    echo "cmdline.txt updated (backup: $CMDLINE.pre-rgbpi-mediaplayer)"
else
    echo "cmdline.txt already set"
fi
# --- 5c: force the FIRMWARE early-boot HDMI output to 15 kHz too. Before KMS
# loads (~5 s) the firmware drives the dongle EEPROM's preferred 1024x768
# (48 kHz) mode — proven dangerous for a 15 kHz CRT. hdmi_mode=87 +
# hdmi_timings makes it output our 15.66 kHz raster from power-on instead.
if ! grep -q '15 kHz CRT SAFETY' "$CONFIG_TXT" 2>/dev/null; then
    cp "$CONFIG_TXT" "$CONFIG_TXT.pre-rgbpi-mediaplayer"
    cat >> "$CONFIG_TXT" <<EOF

# --- RGB-Pi 2 / 15 kHz CRT SAFETY -------------------------------------------
# Force the firmware early-boot HDMI output to a 15 kHz raster so a CRT never
# sees the dongle EEPROM 1024x768 (48 kHz) mode during the ~5 s before KMS.
# simplefb=1920x240 in dmesg is direct proof of the 15.66 kHz output. The
# kernel uses the 2560x240 15 kHz EDID override (drm.edid_firmware).
hdmi_force_hotplug=1
hdmi_ignore_edid=0xa5000080
hdmi_group=2
hdmi_mode=87
hdmi_timings=$FW_HDMI_TIMINGS
EOF
    echo "config.txt firmware 15 kHz timing added (backup: $CONFIG_TXT.pre-rgbpi-mediaplayer)"
else
    echo "config.txt firmware 15 kHz timing already set"
fi

say "6/9 RePlay main-menu entry (Alpha Player slot)"
gcc -O2 -shared -fPIC -o /opt/replay/cores/rgbpi_mediaplayer_libretro.so \
    "$APP_TARGET/deploy/replayos/rgbpi_mediaplayer_libretro.c"
[ -f "$CORES_CFG.orig" ] || cp "$CORES_CFG" "$CORES_CFG.orig"
# "Alpha Player" is a first-class main-menu system: its tile lists the media
# files of /media/sd/roms/alpha_player/ and launches them through the core
# mapped in cores.cfg [alpha_player]. Point that core at our stub and drop a
# 0-byte launcher file in the folder (only its NAME shows in the list; the
# stub ignores the path). The stock alpha player core stays on disk;
# revert = restore cores.cfg.orig.
python3 - "$CORES_CFG" <<'EOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
STUB = "rgbpi_mediaplayer_libretro.so"
def repl(match):
    body = re.sub(r'"(?:[^"]*)"', f'"{STUB}"', match.group(2))
    return match.group(1) + body
new, n = re.subn(r'(\[alpha_player\]\n)((?:\s*\w+\s*=\s*"[^"]*"\n)+)', repl, text)
if n == 0:
    # No [alpha_player] section in this RePlayOS version: append one.
    new = text.rstrip("\n") + f'\n\n[alpha_player]\nlow = "{STUB}"\nmid = "{STUB}"\nhi = "{STUB}"\n'
# Migration: early installs hijacked the [avtest] diagnostics slot — give it
# back to the stock audio/video test core.
def restore_avtest(match):
    if STUB not in match.group(2):
        return match.group(0)
    print("cores.cfg: [avtest] restored to stock avtest_libretro.so")
    return match.group(1) + re.sub(r'"(?:[^"]*)"', '"avtest_libretro.so"', match.group(2))
new = re.sub(r'(\[avtest\]\n)((?:\s*\w+\s*=\s*"[^"]*"\n)+)', restore_avtest, new)
if new != text:
    open(path, "w").write(new)
# Verify: [alpha_player] must reference the stub, [avtest] must not.
final = open(path).read()
section = re.search(r'\[alpha_player\]\n(?:.*\n)*?(?=\[|\Z)', final)
assert section and STUB in section.group(0), "cores.cfg patch failed"
avtest = re.search(r'\[avtest\]\n(?:.*\n)*?(?=\[|\Z)', final)
assert not (avtest and STUB in avtest.group(0)), "avtest still hijacked"
print(f"cores.cfg: [alpha_player] -> {STUB}" + ("" if n else " (section added)"))
EOF
mkdir -p /media/sd/roms/alpha_player
: > "/media/sd/roms/alpha_player/MEDIA PLAYER.mkv"
echo "menu entry: Alpha Player -> MEDIA PLAYER"
if ! grep -q 'view_player *= *"true"' /media/sd/config/replay.cfg 2>/dev/null; then
    echo "NOTE: enable the tile in RePlay: SETTINGS -> VIEW -> SHOW ALPHA PLAYER"
fi

say "7/9 csync watchdog service (CRT sync maintenance during RePlay sessions)"
# RePlay writes the DAC's csync mode only at its own video init; the CH7101
# can drift into a marginal sync state later (status 0x61 != 0xff -> visible
# shimmer). This always-on watchdog re-latches it; the player launcher pauses
# it during player sessions (it runs its own).
chmod 0755 "$APP_TARGET/deploy/replayos/csync_watchdog.sh"
cp "$APP_TARGET/deploy/replayos/rgbpi-csync.service" /etc/systemd/system/rgbpi-csync.service
systemctl daemon-reload
systemctl enable rgbpi-csync >/dev/null 2>&1
systemctl restart rgbpi-csync
echo "rgbpi-csync.service enabled + started"

say "8/9 CRT core-option filters (opt-in: Blargg NTSC composite/RF/S-Video)"
# RePlay hides most libretro core options from its in-game menu — including the
# per-core Blargg NTSC filters. This patch reveals them all (see
# show_all_core_options.py). It modifies /opt/replay/replay (trips anti-tamper
# -> RePlay's own csync off, already covered by rgbpi-csync.service), so it is
# OPT-IN: enabled by the marker file, which survives OS updates in state/.
# Enable:  touch /opt/rgbpi_mediaplayer/state/.show_all_core_options
# Disable: rm the marker, then  cp /opt/replay/replay.orig /opt/replay/replay
FILTER_MARKER="$APP_TARGET/state/.show_all_core_options"
REPLAY_BIN="/opt/replay/replay"
if [ -f "$FILTER_MARKER" ] && [ -f "$REPLAY_BIN" ]; then
    cp -f "$REPLAY_BIN" "$REPLAY_BIN.patch.tmp"
    if python3 "$APP_TARGET/deploy/replayos/show_all_core_options.py" "$REPLAY_BIN.patch.tmp" | grep -q "patched should_show"; then
        # We just patched a *stock* binary (the script refuses a non-stock one),
        # so the live binary is still stock right now -> refresh the backup.
        cp -f "$REPLAY_BIN" "$REPLAY_BIN.orig"
        chmod --reference="$REPLAY_BIN" "$REPLAY_BIN.patch.tmp"
        mv -f "$REPLAY_BIN.patch.tmp" "$REPLAY_BIN"
        echo "core options revealed (backup: $REPLAY_BIN.orig)"
    else
        rm -f "$REPLAY_BIN.patch.tmp"
        echo "already patched — RePlay binary untouched"
    fi
else
    echo "not enabled (touch $FILTER_MARKER to reveal all core options)"
fi

say "9/9 default player prefs (audio out = HDMI / RGB-Pi 2 jack)"
PREFS="$APP_TARGET/state/playback_prefs.json"
if [ -f "$PREFS" ]; then
    echo "existing prefs kept (switch in-app: SETTINGS -> AUDIO OUTPUT -> HDMI)"
else
    mkdir -p "$APP_TARGET/state"
    printf '{\n  "audio_output": "hdmi"\n}\n' > "$PREFS"
    echo "seeded $PREFS"
fi

say "DONE — reboot now, then verify with:
    $APP_TARGET/deploy/replayos/install.sh --check
and open \"Alpha Player\" in RePlay's main menu (entry: MEDIA PLAYER)."
