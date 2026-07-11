#!/usr/bin/env python3
"""Build the EDID firmware override for RePlayOS + RGB-Pi 2 on a 15 kHz CRT.

Takes the dongle's own EDID as a base (read it live from the connector) and
replaces the two base-block detailed timings with the wide 15 kHz modes the
player needs, keeping vendor/product/name bytes intact so RePlay's DAC
detection keeps working:

  DTD1 (preferred): 2560x240 @ 59.99 Hz  — UI + low-res progressive content
  DTD2:             2560x288 @ 50.03 Hz  — PAL progressive fallback
  CEA DTD3:         2560x480i @ 59.94    — NTSC video (full 480-line detail)
  CEA DTD4:         2560x576i @ 50.00    — PAL video (full 576-line detail)

All four share the exact horizontal geometry RePlay's own UI mode uses
(3326 px total). The progressive pair runs the UI clock (52.08 MHz ->
H = 15.658 kHz); the interlaced pair tunes the clock to the broadcast
standards (52.33 MHz -> H = 15.734 kHz NTSC, 51.97 MHz -> H = 15.625 kHz
PAL), so the CRT only ever sees 15.6-15.8 kHz. Interlaced DTD vertical
values are in FRAME units — verified empirically: Linux drm_edid does NOT
double per-field values (a field-encoded DTD came out as "2560x240i@120").
288 active lines at 50 Hz give the same 92 % active-height as 240 lines at
60 Hz (240 lines in a 313-line scan would display vertically squashed).

Usage (on the Pi):
  python3 build_edid.py /sys/class/drm/card1-HDMI-A-1/edid /lib/firmware/edid/mortaca_240p.bin
Then append to /boot/firmware/cmdline.txt (same line):
  drm.edid_firmware=HDMI-A-1:edid/mortaca_240p.bin
and reboot.
"""

import sys


def build_dtd(clk, hact, hso_start, hso_end, htot, vact, vso_start, vso_end, vtot, interlaced=False):
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
    if interlaced:
        b[17] |= 0x80
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

    # CEA extension: strip its video modes (audio kept), then append the two
    # interlaced wide modes as CEA detailed timings — the 4 base descriptor
    # slots are already taken (2 DTDs + range limits + monitor name).
    cleaned = None
    if d[126] >= 1 and len(d) >= 256:
        cleaned = _neutralize_cea(d[128:256])
    if cleaned is None:
        # No usable CEA block in the source: build a minimal one (rev 3, no
        # data blocks) just to carry the interlaced DTDs.
        cleaned = bytearray(128)
        cleaned[0], cleaned[1], cleaned[2] = 0x02, 0x03, 4
    dtd_off = cleaned[2]
    for dtd in (
        # 2560x480i @ 59.94 — NTSC broadcast line rate (H = 15.734 kHz).
        build_dtd(5233, 2560, 2664, 2910, 3326, 480, 484, 490, 525, interlaced=True),
        # 2560x576i @ 50.00 — PAL broadcast line rate (H = 15.625 kHz).
        build_dtd(5197, 2560, 2664, 2910, 3326, 576, 580, 586, 625, interlaced=True),
    ):
        if dtd_off + 18 > 127:
            sys.exit("no room left in the CEA extension for the interlaced DTDs")
        cleaned[dtd_off : dtd_off + 18] = dtd
        dtd_off += 18
    cleaned[127] = (256 - sum(cleaned[0:127])) & 0xFF
    d = d[:128]
    d[126] = 1
    d[127] = (256 - sum(d[0:127])) & 0xFF
    d += cleaned

    # --- verify & report: EVERY exposed timing must stay in the safe band ---
    timings = [(d[54:72], "DTD1"), (d[72:90], "DTD2")]
    ext = d[128:256]
    p, n = ext[2], 3
    while p + 18 <= 127 and not (ext[p] == 0 and ext[p + 1] == 0):
        timings.append((ext[p : p + 18], f"DTD{n} (CEA)"))
        p, n = p + 18, n + 1
    for b, label in timings:
        clk = (b[0] | b[1] << 8) * 10
        hact = b[2] | ((b[4] & 0xF0) << 4)
        hbl = b[3] | ((b[4] & 0x0F) << 8)
        vact = b[5] | ((b[7] & 0xF0) << 4)
        vbl = b[6] | ((b[7] & 0x0F) << 8)
        inter = bool(b[17] & 0x80)
        htot, vtot = hact + hbl, vact + vbl
        h_khz = clk / htot
        # Interlaced DTD verticals are frame units; V shown = field rate.
        v_hz = clk * 1000 / htot / vtot * (2 if inter else 1)
        print(
            f"{label}: {hact}x{vact}{'i' if inter else ''} clock={clk}kHz "
            f"htot={htot} vtot={vtot} -> H={h_khz:.3f}kHz V={v_hz:.3f}Hz"
        )
        if h_khz > 16.0 or clk > 60000:
            sys.exit(f"SAFETY ABORT: {label} exceeds H 16 kHz / clock 60 MHz — nothing written")

    open(dst, "wb").write(d)
    print(f"written: {dst}")


if __name__ == "__main__":
    main()
