# rgbpi_mediaplayer on RePlayOS (RGB-Pi 2 HDMI DAC + 15 kHz CRT)

This folder contains everything needed to understand and install the player on
a **RePlayOS** machine (Debian 13 trixie, full-KMS vc4) driving a **15 kHz RGB
CRT** through the **RGB-Pi 2** HDMI dongle (Chrontel CH7101 DAC + composite
sync combiner). Everything below was reverse-engineered and validated on a
Raspberry Pi 4 + Sony PVM-14M4E.

> **RePlay binary internals** (i2c/DAC registers, the menu & core-option
> system, `should_show_core_option`, the anti-tamper, symbol addresses) are
> documented separately in [`REPLAY-INTERNALS.md`](REPLAY-INTERNALS.md) — the
> reverse-engineering reference behind every patch here.

## How it works (the short version)

The RGB-Pi 2 is a *transparent* HDMI→RGB DAC: **no scaler**. A 15 kHz CRT can
only ever display ~15.7 kHz horizontal timings, so every mode sent to it must
be a wide "super-resolution" 15 kHz mode: a native 320x240 raster would need a
~6.4 MHz pixel clock, far below the HDMI TMDS minimum (25 MHz) and the DAC's
lock range — widening to 2560 pixels keeps the clock at a comfortable 52 MHz.
(vc4-hdmi itself enforces no minimum clock, only even horizontal timings on
Pi 4.) RePlay itself never uses the EDID mode list — it
fabricates its own USERDEF 15 kHz modes. Any other client (SDL, mpv) picks
modes from the connector list, which is why a custom EDID is required.

Three hardware/OS quirks drive the whole design:

1. **CSYNC dies on every modeset.** The CH7101 rebuilds its output config on
   each HDMI re-train and falls back to separated H/V sync → the CRT rolls,
   even though the csync register still *reads* the right value. RePlay fixes
   this by rewriting the csync mode over i2c after each video init; anything
   else must do the same. The chip answers at address `0x78` (reserved range →
   `i2cset -a`) on the **HDMI0 DDC bus** (i2c-20 on kernel 6.12; older kernels
   numbered it differently, e.g. 13). Registers are paged via reg `0x00`:
   - page 0: `0x61` status (`0xff` locked / `0xef` dropout), `0x60` reset
     (`0x00` assert / `0xff` deassert — reset alone makes things *worse*, it
     needs a fresh modeset afterwards);
   - page 4: `0xB5` csync mode — `0x00` separated H/V, `0x0C` XOR, **`0x06`
     AND** (what a PVM wants).
   `replay_launch.sh` runs a watchdog that re-applies csync whenever the KMS
   mode line changes OR `0x61` blips away from `0xff` (catches same-mode
   modesets).
2. **One DRM master at a time.** The pygame UI (SDL2/KMSDRM) holds DRM master;
   mpv (`--vo=drm`) needs it to modeset. The player releases its SDL window
   for the whole playback session and re-creates it afterwards (gamepad input
   is raw evdev and keeps working).
3. **The bundled bullseye rootfs must stay invisible to the host.** Only the
   bundled mpv gets it (`DVDPLAYER_RUNTIME_LIBS=mpv-only` →
   `DVDPLAYER_MPV_LD_LIBRARY_PATH`, consumed by `_child_env`). Exposing it to
   the system python/pygame crashes them (old glibc/SDL shadows), and the
   bundled libasound cannot read trixie's ALSA configs — mpv uses the host's.

### Display modes

Two wide 15 kHz modes are injected via the kernel's EDID firmware override
(no EEPROM flashing, fully reversible). Both share RePlay's exact horizontal
timing (52.08 MHz clock, 3326 px total → H = 15.658 kHz):

| Mode           | vtotal | Refresh  | Used for                          |
|----------------|--------|----------|-----------------------------------|
| 2560x240 (pref)| 261    | 59.99 Hz | player UI, NTSC/film content      |
| 2560x288       | 313    | 50.03 Hz | PAL content (25 fps = 2 vsyncs)   |

288 active lines at 50 Hz are **mandatory** (not optional): a 50 Hz scan has
313 lines, so 240 active lines would only cover 77 % of the screen height and
the picture displays vertically squashed. 288/313 = the same 92 % active
height as 240/261 — the classic PAL 288p raster.

### 15 kHz safety — no >15.7 kHz signal ever reaches the CRT

A 15 kHz CRT (PVM/BVM) can be **damaged** by a horizontal signal above
~15.7 kHz. The setup guarantees every output phase stays at 15.66 kHz:

- **Kernel / userspace (all normal operation).** `build_edid.py` produces a
  **15 kHz-only** EDID: it strips the stock EDID's established timings, the 8
  standard timings, and every CEA extension video mode (VICs + detailed
  timings), keeping only the two 15 kHz DTDs (audio data blocks are
  preserved). It also clamps the display range-limits descriptor to
  15-16 kHz. Result: `cat /sys/class/drm/card1-HDMI-A-1/modes` lists **only**
  `2560x240` and `2560x288` — no client can pick a 31 kHz+ mode because none
  exist. RePlay makes its own 15 kHz USERDEF modes and, with a valid EDID,
  never hits its 640x480 fallback.
- **Firmware early boot (~5 s before KMS).** Left alone the firmware drives
  the dongle EEPROM's preferred **1024x768 @ 48 kHz** (verified: the boot
  `simple-framebuffer` was 1024x768, and changing `hdmi_timings` changed it —
  so it is a real HDMI output, not just a RAM buffer). `config.txt`
  `hdmi_mode=87` + `hdmi_timings` force a **1920x240 @ 39.15 MHz** raster
  (H = 15.66 kHz) from power-on. The width is native (the firmware clamps the
  boot framebuffer to 1920), so the scanned raster equals the framebuffer with
  no scaling — `dmesg | grep simple-framebuffer` showing **1920x240** is
  direct proof of the 15.66 kHz output.

`install.sh --check` verifies all three: only-15 kHz kernel modes, the
config.txt firmware block, and the 1920x240 boot framebuffer. Run it after
every reboot before trusting a new CRT.

**Residual risk (documented for honesty).** If the EDID override file under
`/lib/firmware/edid/` disappears (RePlayOS reflash, SD corruption), the kernel
falls back to the dongle EEPROM's stock EDID and the boot console could output
~48 kHz for the few seconds before RePlay takes over with its own 15 kHz mode.
The definitive close is to flash the 15 kHz-only EDID into the dongle EEPROM
itself (RePlayOS ships `edid_rw.py` in the Extra tools; keep a backup of the
stock EDID first — `install.sh` already caches one at
`state/.stock_edid.bin`). Until then: after any OS reflash, re-run
`install.sh` before connecting the CRT.

### Sync maintenance (csync watchdog)

RePlay writes the DAC's csync mode only at its own video init. The CH7101 can
later drift into a **marginal sync state**: the picture still displays but
shimmers, and the status register (page 0, reg `0x61`) reads something other
than the healthy steady `0xff` (e.g. `0x18`, `0xc0`, `0x00`). Rewriting the
csync mode (page 4, reg `0xB5`) re-latches the output and `0x61` returns to
`0xff` — measured and reproduced.

Two watchdogs cover every situation, both following the user's RePlay
`video_crt_csync_mode` setting (0 = AND `0x06`, 1 = XOR `0x0C`,
2 = separated `0x00`):

- **`rgbpi-csync.service`** (system-wide, always on, installed by
  `install.sh`): polls `0x61` at 1 Hz and re-applies csync whenever it leaves
  `0xff`. Survives early boot (retries until the i2c bus and DAC appear) and
  never gives up (`StartLimitIntervalSec=0`).
- **The launcher watchdog** (player sessions): faster, keyed to KMS mode-line
  changes and `0x61` blips (mpv modesets). `replay_launch.sh` stops the
  system service for the session and restarts it on exit, so exactly one
  watchdog runs at any time.

The write is the exact operation RePlay performs itself, with the same value —
racing RePlay's own init is harmless.

### Connecting a new CRT — checklist

1. Full cold power-off first (cables out ~10 s): the dongle is powered from
   HDMI 5 V and never resets across mere reboots — a cold start clears any
   accumulated DAC state.
2. Boot with the CRT connected, then over SSH:
   `/opt/rgbpi_mediaplayer/deploy/replayos/install.sh --check`
   — every line must be OK, in particular the three 15 kHz safety gates and
   "csync watchdog service active".
3. If the picture ever shimmers: read the DAC status
   (`i2cset -y -a <bus> 0x78 0x00 0x00; i2cget -y -a <bus> 0x78 0x61`) —
   anything but `0xff` means sync drift; the watchdog should correct it within
   ~2 s, and `csync_probe.sh` logs the transitions if you want a trace.

mpv 0.32 gotchas: `--drm-mode=WxH@R` needs an *exact* refresh match ("@50"
never matches 50.03 Hz) → modes are selected by unique name (`2560x288`) or by
index; `--vo=gpu` segfaults with the bundled bullseye Mesa (GBM/V3D) → stick
to `--vo=drm`, whose default software scaler drops ~5 frames/s at these widths
→ `--sws-scaler=fast-bilinear` (0 drops, indistinguishable on a CRT).

### Audio

Audio rides the HDMI link; the RGB-Pi 2 extracts it to its own 3.5 mm jack.
The vc4-hdmi PCM only accepts IEC958 subframes, so mpv must open the ALSA
*card profile* (`default:CARD=vc4hdmi0`), which converts. In the player,
select **SETTINGS → AUDIO OUTPUT → HDMI (RGB-PI 2)** (the `hdmi` value of
`audio_output`; device overridable via `DVDPLAYER_HDMI_ALSA_DEVICE`). The UI
itself frees the sound device at startup (`pygame.mixer.quit()` — the single
vc4-hdmi PCM is exclusive and the UI plays no sounds).

## Install

Three commands over SSH, as root on the Pi (RePlayOS default root login; RGB-Pi
2 on HDMI0, CRT connected):

```sh
apt-get update && apt-get install -y git && git clone --depth 1 https://github.com/thecyril/rgbpi_mediaplayer.git /opt/rgbpi_mediaplayer
/opt/rgbpi_mediaplayer/deploy/replayos/install.sh
reboot
```

After the reboot, verify everything (16 checks: 15 kHz safety gates, live
output frequency, menu entry, mpv/bundle health, watchdog, DAC, audio):

```sh
/opt/rgbpi_mediaplayer/deploy/replayos/install.sh --check
```

Then open **"Alpha Player"** in RePlay's **main menu** and pick **MEDIA
PLAYER**. `install.sh` is idempotent (safe to re-run after a `git pull`; never
touches `state/`). It installs the apt dependencies, fixes the bundle libs,
builds the EDID override from the dongle's own EDID and wires it into
`cmdline.txt`, installs the launcher, compiles the stub core and patches
`cores.cfg`, and seeds the HDMI audio preference. The launcher's csync
watchdog follows the user's RePlay `video_crt_csync_mode` setting
(AND/XOR/separated) and finds the DAC's i2c bus dynamically (kernel updates
renumber the DDC buses).

### What the installer does (manual reference)

1. **Bundle libs**: `fix_bundle_libs.sh` — SONAME symlinks for the bundled
   mpv, minus the host-shadowing core libs, bundled libasound disabled.
2. **EDID override**: `build_edid.py <connector edid> /lib/firmware/edid/…`
   plus `drm.edid_firmware=HDMI-A-1:edid/…` appended to the single line of
   `/boot/firmware/cmdline.txt`. After reboot,
   `cat /sys/class/drm/card1-HDMI-A-1/modes` must list `2560x240` (first) and
   `2560x288`.
3. **Launcher**: `replay_launch.sh` at the app root (stops replay.service,
   exports the display/audio/lib environment, runs the csync watchdog, starts
   the player, restarts RePlay on exit). Geometry/env tuning lives at the top
   (`DVDPLAYER_DISPLAY_W/H` UI size, `DVDPLAYER_MPV_DRM_MODE` playback mode,
   `DVDPLAYER_MPV_DRM_MODE_PAL` PAL-content mode (+ optional
   `DVDPLAYER_MPV_DRM_MODE_PAL_GEOM` when selecting by index),
   `DVDPLAYER_DRM_CONNECTOR`, `DVDPLAYER_RUNTIME_LIBS=mpv-only` lib isolation,
   `DVDPLAYER_HDMI_ALSA_DEVICE` for the in-app HDMI audio output, and
   `DVDPLAYER_ALSA_DEVICE` as a safety net so the legacy "jack" output also
   lands on the HDMI card).
4. **RePlay main-menu entry** (config-only hijack, no binary patching):
   "Alpha Player" is a first-class main-menu system in RePlay — its tile
   (shown when `view_player="true"`, i.e. SETTINGS → VIEW → SHOW ALPHA
   PLAYER) lists the media files of `/media/sd/roms/alpha_player/` and
   launches them through the core mapped in `/opt/replay/cores/cores.cfg`
   `[alpha_player]` (stock: `alpha_player_libretro.so`). The installer points
   that section at the stub core (`rgbpi_mediaplayer_libretro.c`, compiled
   on-Pi) and drops a 0-byte `MEDIA PLAYER.mkv` launcher file in the folder —
   only its name shows in the list, and the stub ignores the path and fires
   `replay_launch.sh` instead. Revert = restore `cores.cfg.orig`.
   (Legacy alternative — Extra-menu route, same idea: `.sh` entries are blocked
   ("FORBIDDEN!!!") and `.lr` names are hardcoded, but the stock
   `audio_video_test.lr` maps to `get_core("avtest")` whose `[avtest]`
   section can be pointed at the stub the same way.)
5. **Audio**: fresh installs are seeded with `audio_output=hdmi`; existing
   installs keep their choice (SETTINGS → AUDIO OUTPUT → **HDMI (RGB-PI 2)**).

## Core-option filters (Blargg NTSC: composite / RF / S-Video) — opt-in

RGB-Pi OS 4 / RetroArch let you pick the per-core **Blargg NTSC filter** to
emulate a composite / RF / S-Video / RGB cable look. The cores RePlayOS ships
(snes9x, fceumm, genesis_plus_gx, ...) all support it, but RePlay **hides most
core options** from its in-game menu (via `should_show_core_option`), so the
filter is unreachable by default.

`show_all_core_options.py` reveals them: an 8-byte patch that makes
`should_show_core_option` always return "show", so the in-game **SYSTEM
SETTINGS** menu lists every core option, live-selectable — exactly like
RetroArch's own menu. Find **"Blargg NTSC Filter"** (SNES/NES/Genesis) and set
Composite / S-Video / RGB; it changes live and RePlay remembers it per system.

It is **opt-in** because it patches `/opt/replay/replay`, which trips RePlay's
anti-tamper (`is_replay_hacked`) — RePlay then stops maintaining the CH7101
csync itself. That is already covered by `rgbpi-csync.service`, and nothing
else changes (boot, game launching and stability are unaffected; the patch only
changes which menu entries are shown). Enable it with a marker file that
survives OS updates, then re-run the installer:

```sh
touch /opt/rgbpi_mediaplayer/state/.show_all_core_options
/opt/rgbpi_mediaplayer/deploy/replayos/install.sh        # applies the patch
reboot
```

The stock binary is backed up to `/opt/replay/replay.orig` (revert:
`cp /opt/replay/replay.orig /opt/replay/replay && reboot`, and `rm` the marker).
A RePlayOS update restores the stock binary; re-running the installer
re-applies the patch. `install.sh --check` verifies it when the marker is set.

## Diagnostics

- `csync_probe.sh` — logs transitions of the CH7101 page0/`0x61` status
  (`0xff` locked / `0xef` dropout): a remote "is the CRT synced" probe.
- `ch7101_dump.sh` — dumps all register pages (diff a good vs bad state).
- mpv live stats: connect to `/tmp/rgbpi-dvdplayer-ipc-*.sock` and query
  `frame-drop-count` / `video-params` / `container-fps` over JSON IPC.
- Player logs: `/opt/rgbpi_mediaplayer/state/runtime/*.log`, launcher log:
  `/var/log/rgbpi_mediaplayer_launch.log`.

## Known limitations

- DVDs are downscaled to 240/288 lines (progressive). True interlaced
  2560x480i/2560x576i output ("phase B-full") is unexplored: vo=gpu is dead
  (Mesa segfault) and vo=drm's interlaced behaviour on vc4 6.12 is unverified.
- `systemctl restart replay.service` on RePlayOS has been seen to trigger a
  full reboot — prefer a clean `reboot` or the launcher's stop/start flow.
- Watch out for `pkill -f`/`pgrep -f` over SSH: the remote shell's own command
  line contains the quoted paths (e.g. `bin/mpv`) and matches itself — use
  `pkill -x mpv` / bracketed patterns (`pgrep -f "[d]vdplayer"`).
