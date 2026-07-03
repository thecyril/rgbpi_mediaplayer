#!/usr/bin/env python3
"""Build the EDID firmware override for RePlayOS + RGB-Pi 2 on a 15 kHz CRT.

Takes the dongle's own EDID as a base (read it live from the connector) and
replaces the two base-block detailed timings with the wide 15 kHz modes the
player needs, keeping vendor/product/name bytes intact so RePlay's DAC
detection keeps working:

  DTD1 (preferred): 2560x240 @ 59.99 Hz  — UI + NTSC/film content
  DTD2:             2560x288 @ 50.03 Hz  — PAL content (25 fps = 2 vsyncs)

Both share the exact horizontal timing RePlay's own UI mode uses
(52.08 MHz pixel clock, 3326 px total -> H = 15.658 kHz), so the CRT never
sees a horizontal rate change. 288 active lines at 50 Hz give the same 92 %
active-height as 240 lines at 60 Hz (240 lines in a 313-line scan would
display vertically squashed).

Usage (on the Pi):
  python3 build_edid.py /sys/class/drm/card1-HDMI-A-1/edid /lib/firmware/edid/mortaca_240p.bin
Then append to /boot/firmware/cmdline.txt (same line):
  drm.edid_firmware=HDMI-A-1:edid/mortaca_240p.bin
and reboot.
"""

import sys


def build_dtd(clk, hact, hso_start, hso_end, htot, vact, vso_start, vso_end, vtot):
    """Encode one EDID detailed timing descriptor (clk in 10 kHz units)."""
    hbl = htot - hact
    hso = hso_start - hact
    hsw = hso_end - hso_start
    vbl = vtot - vact
    vso = vso_start - vact
    vsw = vso_end - vso_start
    b = bytearray(18)
    b[0] = clk & 0xFF
    b[1] = clk >> 8
    b[2] = hact & 0xFF
    b[3] = hbl & 0xFF
    b[4] = ((hact >> 8) << 4) | (hbl >> 8)
    b[5] = vact & 0xFF
    b[6] = vbl & 0xFF
    b[7] = ((vact >> 8) << 4) | (vbl >> 8)
    b[8] = hso & 0xFF
    b[9] = hsw & 0xFF
    b[10] = ((vso & 0xF) << 4) | (vsw & 0xF)
    b[11] = ((hso >> 8) << 6) | ((hsw >> 8) << 4) | ((vso >> 4) << 2) | (vsw >> 4)
    # Physical image size in mm (a 14" 4:3 CRT); cosmetic only.
    b[12] = 270 & 0xFF
    b[13] = 200 & 0xFF
    b[14] = ((270 >> 8) << 4) | (200 >> 8)
    b[17] = 0x18  # digital, separate sync, negative H/V polarities
    return b


# Monitor-name prefixes of known RGB-Pi DAC EDIDs (the same list the replay
# binary uses for DAC detection, plus the mortaca custom EDIDs). Refusing
# anything else prevents wiring a 15 kHz override for a plain TV/monitor
# plugged into HDMI0 — which would leave that display unable to sync at boot.
KNOWN_NAMES = ("MORTACA", "VGA DISP", "HDMI-VGA", "TV DISP", "LRTX", "RPI-DPI")


def _monitor_name(d: bytes) -> str:
    for off in (54, 72, 90, 108):
        b = d[off : off + 18]
        if b[0] == 0 and b[1] == 0 and b[3] == 0xFC:
            return b[5:18].decode("ascii", "replace").strip()
    return ""


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    if len(args) != 2:
        sys.exit(__doc__)
    src, dst = args
    d = bytearray(open(src, "rb").read())
    if len(d) < 128 or d[:2] != b"\x00\xff":
        sys.exit("source does not look like an EDID")
    name = _monitor_name(d)
    if not force and not any(name.upper().startswith(k) for k in KNOWN_NAMES):
        sys.exit(
            f"EDID monitor name is {name!r} — not a known RGB-Pi DAC.\n"
            "Is the RGB-Pi 2 really plugged into HDMI0? Building a 15 kHz\n"
            "override for a regular display would leave it unable to sync.\n"
            "Re-run with --force to override."
        )

    # DTD1 @54 (preferred) and DTD2 @72; descriptors @90/@108 (range/name) kept.
    d[54:72] = build_dtd(5208, 2560, 2664, 2910, 3326, 240, 242, 245, 261)
    d[72:90] = build_dtd(5208, 2560, 2664, 2910, 3326, 288, 290, 293, 313)
    d[127] = (256 - sum(d[0:127])) & 0xFF

    open(dst, "wb").write(d)
    for off, label in ((54, "DTD1"), (72, "DTD2")):
        b = d[off : off + 18]
        clk = (b[0] | b[1] << 8) * 10
        hact = b[2] | ((b[4] & 0xF0) << 4)
        hbl = b[3] | ((b[4] & 0x0F) << 8)
        vact = b[5] | ((b[7] & 0xF0) << 4)
        vbl = b[6] | ((b[7] & 0x0F) << 8)
        htot, vtot = hact + hbl, vact + vbl
        print(
            f"{label}: {hact}x{vact} clock={clk}kHz htot={htot} vtot={vtot} "
            f"-> H={clk/htot:.3f}kHz V={clk*1000/htot/vtot:.3f}Hz"
        )
    print(f"written: {dst}")


if __name__ == "__main__":
    main()
