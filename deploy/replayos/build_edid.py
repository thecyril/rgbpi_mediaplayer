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


def _neutralize_cea(ext: bytes):
    """Strip every video mode from a CEA-861 extension, keep audio.

    Returns a rebuilt 128-byte block with all detailed timings and Video Data
    Blocks (VICs) removed but Audio/Speaker/Vendor data blocks preserved (so
    the sink still advertises HDMI audio), or ``None`` if ``ext`` is not a CEA
    block. Native-DTD count is zeroed; the base-block header keeps the
    underscan / basic-audio / YCbCr flags.
    """
    if len(ext) < 128 or ext[0] != 0x02:
        return None
    dtd_off = ext[2]
    out = bytearray(128)
    out[0] = 0x02
    out[1] = ext[1]  # revision
    out[3] = ext[3] & 0xF0  # keep flags, native-DTD count nibble -> 0
    w = 4
    if 4 <= dtd_off <= 127:
        p = 4
        while p < dtd_off:
            length = ext[p] & 0x1F
            tag = ext[p] >> 5
            block = ext[p : p + 1 + length]
            p += 1 + length
            if tag == 2:  # Video Data Block (VIC list) -> drop
                continue
            out[w : w + len(block)] = block  # audio(1)/vendor(3)/speaker(4)/ext(7)
            w += len(block)
    out[2] = w  # DTD offset = end of data blocks; no detailed timings follow
    out[127] = (256 - sum(out[0:127])) & 0xFF
    return out


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

    # --- 15 kHz ONLY -------------------------------------------------------
    # A 15 kHz CRT can be DAMAGED by a >15.7 kHz horizontal signal. So the
    # override must expose NOTHING but the two wide 15 kHz modes — every other
    # timing the stock EDID advertised (640x480, 1024x768, 1280x720, 1080p...)
    # is a mode a client could pick and send to the tube. We strip them all:
    #   - established timings (bytes 35-37) -> 0
    #   - the 8 standard timings (bytes 38-53) -> unused (0x01 0x01)
    #   - both base detailed timings (54, 72) -> our 2560x240 / 2560x288
    #   - descriptor 3 (90) -> display range limits clamped to 15 kHz, so even
    #     a hand-rolled mode is out of range
    #   - descriptor 4 (108) -> keep the monitor name (DAC detection)
    #   - the CEA extension video modes (VICs + its detailed timings) removed,
    #     audio data blocks preserved so HDMI audio still works
    d[35] = d[36] = d[37] = 0x00
    for i in range(38, 54, 2):
        d[i], d[i + 1] = 0x01, 0x01
    d[54:72] = build_dtd(5208, 2560, 2664, 2910, 3326, 240, 242, 245, 261)
    d[72:90] = build_dtd(5208, 2560, 2664, 2910, 3326, 288, 290, 293, 313)
    # Display range limits (tag 0xFD): V 47-62 Hz, H 15-16 kHz, max clock
    # 60 MHz, byte 10 = 0x01 ("no timing formula" — forbids GTF/CVT mode
    # inference), then the spec-mandated 0x0A + 0x20 padding.
    d[90:108] = bytes(
        [0x00, 0x00, 0x00, 0xFD, 0x00, 47, 62, 15, 16, 6, 0x01, 0x0A, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20]
    )
    d[127] = (256 - sum(d[0:127])) & 0xFF

    if d[126] >= 1 and len(d) >= 256:
        cleaned = _neutralize_cea(d[128:256])
        if cleaned is not None:
            d[128:256] = cleaned
            d = d[:256]  # keep exactly one extension
            d[126] = 1
        else:
            d = d[:128]
            d[126] = 0
            d[127] = (256 - sum(d[0:127])) & 0xFF
    else:
        d = d[:128]
        d[126] = 0
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
