#!/usr/bin/env python3
"""Reveal ALL libretro core options in RePlay's in-game SYSTEM SETTINGS menu.

By default RePlay hides most core options (via `should_show_core_option`), so
the per-core video filters you'd use on RGB-Pi OS 4 / RetroArch — most notably
the **Blargg NTSC filter** (RF / Composite / S-Video / RGB) in snes9x, fceumm,
genesis_plus_gx, ... — are unreachable, even though the cores fully support
them.

This patch neutralizes `should_show_core_option` so it always returns "show".
The in-game SYSTEM SETTINGS menu then lists every core option, live-selectable,
exactly like RetroArch's own options menu on OS 4. It is a two-instruction,
8-byte overwrite of that one function's entry; the rest of the binary is
untouched.

⚠️ This modifies /opt/replay/replay, which trips RePlay's anti-tamper
(`is_replay_hacked`): RePlay then stops maintaining the CH7101 csync itself —
already covered by rgbpi-csync.service (the always-on watchdog). Everything
else (boot, game launching, stability) is unaffected: the patch only changes
which menu entries are shown. A RePlayOS update restores the stock binary;
re-run this patch after one.

Usage (idempotent; keep a stock backup first):
    cp /opt/replay/replay /opt/replay/replay.orig     # once, if not present
    python3 show_all_core_options.py /opt/replay/replay
    # deploy the running binary with an atomic mv (cannot overwrite in place):
    #   cp /opt/replay/replay /opt/replay/replay.new && \
    #   python3 show_all_core_options.py /opt/replay/replay.new && \
    #   mv -f /opt/replay/replay.new /opt/replay/replay && reboot
Revert:
    cp /opt/replay/replay.orig /opt/replay/replay && reboot
"""

import struct
import sys

# Virtual address of should_show_core_option (from `nm replay.elf`).
FUNC_VADDR = 0x2C86E0
# Expected first 8 bytes = the stock prologue, little-endian:
#   stp x30, x19, [sp, #-0x30]!   (a9bd4ffe)
#   stp x20, x21, [sp, #0x10]     (a90157f4)
STOCK_PROLOGUE = bytes.fromhex("fe4fbda9f45701a9")
# Replacement: mov w0, #1 ; ret  -> always "show this option".
PATCH = bytes.fromhex("20008052c0035fd6")
# The patch's own signature, so a re-run is a no-op instead of an error.
ALREADY = PATCH


def v2o(f, v):
    e_phoff = struct.unpack("<Q", f[0x20:0x28])[0]
    e_phentsize = struct.unpack("<H", f[0x36:0x38])[0]
    e_phnum = struct.unpack("<H", f[0x38:0x3A])[0]
    for i in range(e_phnum):
        p = f[e_phoff + i * e_phentsize : e_phoff + (i + 1) * e_phentsize]
        if struct.unpack("<I", p[0:4])[0] == 1:  # PT_LOAD
            off, vaddr, _, filesz = struct.unpack("<QQQQ", p[8:40])
            if vaddr <= v < vaddr + filesz:
                return off + (v - vaddr)
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    data = bytearray(open(path, "rb").read())
    if data[:4] != b"\x7fELF":
        sys.exit("not an ELF binary")
    off = v2o(data, FUNC_VADDR)
    if off is None:
        sys.exit(f"cannot map should_show_core_option @ {hex(FUNC_VADDR)}")
    cur = bytes(data[off : off + 8])
    if cur == ALREADY:
        print("already patched — no change")
        return
    if cur != STOCK_PROLOGUE:
        sys.exit(
            "unexpected bytes at should_show_core_option "
            f"({cur.hex()} != {STOCK_PROLOGUE.hex()}) — wrong RePlay build; aborting"
        )
    data[off : off + 8] = PATCH
    open(path, "wb").write(data)
    print(f"patched should_show_core_option -> always show: {path}")


if __name__ == "__main__":
    main()
