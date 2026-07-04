#!/bin/bash
# Stop RePlay, run the rgbpi media player on the display (full-KMS), relaunch RePlay on exit.
exec >>/var/log/rgbpi_mediaplayer_launch.log 2>&1
echo "=== $(date -Iseconds) launch requested (pid $$) ==="
systemctl stop replay.service || true
sleep 1
# Wipe the text console (tty1) so no stale output (a prior failed-launch
# error, or boot text) bleeds through during the black window before the
# player grabs the KMS display.
printf '\033c' > /dev/tty1 2>/dev/null || true
# Full-KMS: steer SDL to the DRM/KMS backend so the pygame UI reaches the CRT (avoid dummy fallback)
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-kmsdrm}"
export SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-alsa}"
# UI mode: the wide 240p super-resolution the PVM (15 kHz) can display; the
# EDID firmware override (drm.edid_firmware in cmdline.txt) advertises it.
export DVDPLAYER_DISPLAY_W=2560
export DVDPLAYER_DISPLAY_H=240
# mpv playback: RGB-Pi 2 is on HDMI (not the OS4 VGA-1 DPI connector), and
# video must go to the same wide 240p mode (no 15 kHz 720-wide modes exist).
export DVDPLAYER_DRM_CONNECTOR=HDMI-A-1
export DVDPLAYER_MPV_DRM_MODE=2560x240
# PAL content (25 fps) plays on the PAL-land raster (2560x288@50, vtot=313,
# added to the EDID override): exact 2-vsync-per-frame cadence AND the same
# 92% active height as the 240p@60 mode (240 lines in a 313-line scan would
# show squashed). Matched by NAME — unique in the list, no refresh needed
# (mpv 0.32's "@50" would fail against the real 50.03 Hz).
export DVDPLAYER_MPV_DRM_MODE_PAL=2560x288
# Keep the bundled bullseye rootfs libs away from the system python/pygame
# (SDL + glibc clashes on trixie) — the start script exports them to the mpv
# child only (DVDPLAYER_MPV_LD_LIBRARY_PATH consumed by _child_env).
export DVDPLAYER_RUNTIME_LIBS=mpv-only
# Audio: card 0 is the vc4-hdmi PCM (RGB-Pi 2 extracts it to its jack). Raw
# hw:0,0 only takes IEC958 subframes — go through the ALSA card profile which
# does the linear→IEC958 conversion. (Not the OS4 bcm2835 hw:0,0 s16 tuning.)
export DVDPLAYER_ALSA_DEVICE="default:CARD=vc4hdmi0"

# --- CH7101 csync maintenance -------------------------------------------------
# The RGB-Pi 2 DAC (CH7101, i2c addr 0x78 on the HDMI0 DDC bus) rebuilds its
# output config on every HDMI mode set and falls back to separated H/V sync →
# rolling picture. Re-apply the csync mode after each modeset, exactly like
# the replay binary does at video init (page 4, reg 0xB5).
# The mode value follows the user's RePlay setting (video_crt_csync_mode in
# replay.cfg): 0 = AND (0x06, PVM-style csync input), 1 = XOR (0x0C),
# 2 = separated H/V (0x00).
# Two triggers, because a modeset to the SAME mode leaves the KMS mode line
# unchanged: (1) the mode line text changes, (2) the chip's page0/0x61 status
# blips away from 0xff (observed 0xef for ~200-300 ms at every HDMI retrain).
# The DAC sits on the HDMI0 DDC bus, whose number depends on the kernel
# (13 on 6.12.62, 20 on 6.12.75...). Scan for the first bus that answers a
# READ at 0x78 (write-probes phantom-ACK on the brcmstb DDC controllers).
modprobe -q i2c-dev 2>/dev/null
CSYNC_BUS=""
for dev in /dev/i2c-*; do
    b="${dev#/dev/i2c-}"
    if i2cget -y -a "$b" 0x78 >/dev/null 2>&1; then CSYNC_BUS="$b"; break; fi
done
[ -n "$CSYNC_BUS" ] || CSYNC_BUS=20
echo "$(date -Iseconds) CH7101 on i2c bus $CSYNC_BUS"
CSYNC_VAL=0x06
REPLAY_CFG=/media/sd/config/replay.cfg
if [ -r "$REPLAY_CFG" ]; then
    case "$(sed -n 's/^video_crt_csync_mode *= *"\([0-9]\)".*/\1/p' "$REPLAY_CFG" | head -1)" in
        1) CSYNC_VAL=0x0C ;;
        2) CSYNC_VAL=0x00 ;;
    esac
fi
csync_apply() {
    i2cset -y -a "$CSYNC_BUS" 0x78 0x00 0x04 2>/dev/null
    i2cset -y -a "$CSYNC_BUS" 0x78 0xB5 "$CSYNC_VAL" 2>/dev/null
    i2cset -y -a "$CSYNC_BUS" 0x78 0x00 0x00 2>/dev/null
}
current_mode() {
    grep "mode:" /sys/kernel/debug/dri/1/state 2>/dev/null | grep -v '""' | head -1
}
csync_watchdog() {
    local last="" v m
    # keep page 0 selected so reading 0x61 is meaningful
    i2cset -y -a "$CSYNC_BUS" 0x78 0x00 0x00 2>/dev/null
    while true; do
        m=$(current_mode)
        if [ -n "$m" ] && [ "$m" != "$last" ]; then
            sleep 0.3   # let the HDMI link settle after the modeset
            csync_apply
            echo "$(date -Iseconds) csync re-applied after modeset: $m"
            last="$m"
            continue
        fi
        v=$(i2cget -y -a "$CSYNC_BUS" 0x78 0x61 2>/dev/null)
        if [ -n "$v" ] && [ "$v" != "0xff" ]; then
            sleep 0.5   # retrain in progress; wait for the link to come back
            csync_apply
            echo "$(date -Iseconds) csync re-applied after 0x61 blip ($v)"
        fi
        sleep 0.1
    done
}
# The system-wide csync watchdog (rgbpi-csync.service) is paused for the
# session: this launcher runs its own, faster one keyed to mpv modesets.
systemctl stop rgbpi-csync 2>/dev/null || true
csync_watchdog &
WATCHDOG_PID=$!
# ------------------------------------------------------------------------------

cd /opt/rgbpi_mediaplayer || { echo "no app dir"; kill $WATCHDOG_PID; systemctl start replay.service; exit 1; }
./start_rgbpi_dvdplayer_python.sh
rc=$?
kill $WATCHDOG_PID 2>/dev/null
systemctl start rgbpi-csync 2>/dev/null || true
echo "=== $(date -Iseconds) player exited rc=$rc — relaunching RePlay ==="
systemctl start replay.service
