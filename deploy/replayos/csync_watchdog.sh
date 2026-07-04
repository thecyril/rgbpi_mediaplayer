#!/bin/sh
# System-wide CH7101 csync watchdog (covers RePlay sessions and the console).
#
# The DAC's output sync config can degrade after modeset sequences: the
# status register (page 0, reg 0x61) reads != 0xff and the CRT picture
# shimmers even though it still displays. Rewriting the csync mode (page 4,
# reg 0xB5 — the same write the replay binary does at video init) re-latches
# the output and 0x61 returns to a steady 0xff. RePlay only writes it at its
# own init, so a later drift goes uncorrected without this watchdog.
#
# The player launcher (replay_launch.sh) stops this service for the duration
# of a player session (it runs its own, faster watchdog keyed to mpv
# modesets) and restarts it on exit.
find_bus() {
    for dev in /dev/i2c-*; do
        [ -e "$dev" ] || continue
        b="${dev#/dev/i2c-}"
        i2cget -y -a "$b" 0x78 >/dev/null 2>&1 && { echo "$b"; return 0; }
    done
    return 1
}
# At early boot i2c-dev may not be loaded yet and /media/sd may not be
# mounted — keep retrying instead of exiting (systemd would rate-limit us).
modprobe -q i2c-dev 2>/dev/null
BUS="$(find_bus || true)"
while [ -z "$BUS" ]; do
    sleep 5
    modprobe -q i2c-dev 2>/dev/null
    BUS="$(find_bus || true)"
done

# Follow the user's RePlay csync setting, RE-READ at every apply so a live
# menu change (AND/XOR/separated) is honoured immediately:
# 0 = AND (0x06), 1 = XOR (0x0C), 2 = separated H/V (0x00).
CFG=/media/sd/config/replay.cfg
csync_val() {
    v=0x06
    if [ -r "$CFG" ]; then
        case "$(sed -n 's/^video_crt_csync_mode *= *"\([0-9]\)".*/\1/p' "$CFG" | head -1)" in
            1) v=0x0C ;;
            2) v=0x00 ;;
        esac
    fi
    echo "$v"
}

apply() {
    VAL="$(csync_val)"
    i2cset -y -a "$BUS" 0x78 0x00 0x04 2>/dev/null
    i2cset -y -a "$BUS" 0x78 0xB5 "$VAL" 2>/dev/null
    i2cset -y -a "$BUS" 0x78 0x00 0x00 2>/dev/null
}

current_mode() {
    grep 'mode:' /sys/kernel/debug/dri/*/state 2>/dev/null | grep -v '""' | head -1
}

logger -t rgbpi-csync "watchdog started (bus $BUS)"
LAST_MODE="$(current_mode)"
SETTLE=0
while :; do
    # Modeset-aware backoff: right after a mode change RePlay performs its
    # OWN page-select + csync write sequence. Touching the bus then could
    # interleave with it (the chip's page register is global), so leave the
    # bus alone for a couple of cycles and verify afterwards.
    m="$(current_mode)"
    if [ "$m" != "$LAST_MODE" ]; then
        LAST_MODE="$m"
        SETTLE=2
    fi
    if [ "$SETTLE" -gt 0 ]; then
        SETTLE=$((SETTLE - 1))
        sleep 1
        continue
    fi
    i2cset -y -a "$BUS" 0x78 0x00 0x00 2>/dev/null
    v="$(i2cget -y -a "$BUS" 0x78 0x61 2>/dev/null)"
    if [ -n "$v" ] && [ "$v" != "0xff" ]; then
        # Mid-modeset blips read 0xef for ~300 ms; give the link a moment to
        # settle, then re-latch. The write is idempotent (same value RePlay
        # itself uses).
        sleep 0.5
        apply
        logger -t rgbpi-csync "re-applied csync (0x61 was $v)"
    fi
    sleep 1
done
