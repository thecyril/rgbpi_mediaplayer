# rgbpi_mediaplayer on RePlayOS (RGB-Pi 2 HDMI DAC + 15 kHz CRT)

This folder contains everything needed to understand and install the player on
a **RePlayOS** machine (Debian 13 trixie, full-KMS vc4) driving a **15 kHz RGB
CRT** through the **RGB-Pi 2** HDMI dongle (Chrontel CH7101 DAC + composite
sync combiner). Everything below was reverse-engineered and validated on a
Raspberry Pi 4 + Sony PVM-14M4E.

## How it works (the short version)

The RGB-Pi 2 is a *transparent* HDMI→RGB DAC: **no scaler**. A 15 kHz CRT can
only ever display ~15.7 kHz horizontal timings, so every mode sent to it must
be a wide "super-resolution" 15 kHz mode (vc4-hdmi refuses pixel clocks under
~31 MHz, hence the width). RePlay itself never uses the EDID mode list — it
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

## Install on a fresh RePlayOS

Prerequisites: root SSH on the Pi, the player deployed at
`/opt/rgbpi_mediaplayer` (use `tar` over the network — macOS openrsync drops
the bundle's 238 symlinks), and `apt-get install -y python3-pygame
python3-requests i2c-tools`.

1. **Bundle libs** (once):
   `sh deploy/replayos/fix_bundle_libs.sh /opt/rgbpi_mediaplayer`
2. **EDID override**:
   ```
   python3 build_edid.py /sys/class/drm/card1-HDMI-A-1/edid /lib/firmware/edid/mortaca_240p.bin
   ```
   Append to the single line of `/boot/firmware/cmdline.txt`:
   `drm.edid_firmware=HDMI-A-1:edid/mortaca_240p.bin` — then reboot and check
   `cat /sys/class/drm/card1-HDMI-A-1/modes` starts with `2560x240` and also
   lists `2560x288`.
3. **Launcher**: copy `replay_launch.sh` to `/opt/rgbpi_mediaplayer/` (it
   stops replay.service, exports the display/audio/lib environment, runs the
   csync watchdog, starts the player, restarts RePlay on exit).
4. **RePlay menu entry** (config-only hijack, no binary patching): RePlay's
   `game_launcher` maps Extra entries to cores by hardcoded name; only
   `pibench.lr` and `audio_video_test.lr` map to cores, and `.sh` entries are
   blocked ("FORBIDDEN!!!"). But the name→.so mapping goes through
   `/opt/replay/cores/cores.cfg`, which is editable:
   ```
   gcc -O2 -shared -fPIC -o rgbpi_mediaplayer_libretro.so rgbpi_mediaplayer_libretro.c
   cp rgbpi_mediaplayer_libretro.so /opt/replay/cores/
   cp /opt/replay/cores/cores.cfg /opt/replay/cores/cores.cfg.orig
   # edit cores.cfg: point the [avtest] section's low/mid/hi at
   # "rgbpi_mediaplayer_libretro.so"
   ```
   The Extra menu entry named "audio_video_test" then launches the player
   (the label is the hardcoded filename; renaming it would require patching
   the replay binary). Revert = restore `cores.cfg.orig`.
5. **Player settings**: SETTINGS → AUDIO OUTPUT → **HDMI (RGB-PI 2)**.
6. Optional geometry/env tuning lives at the top of `replay_launch.sh`
   (`DVDPLAYER_DISPLAY_W/H`, `DVDPLAYER_MPV_DRM_MODE`,
   `DVDPLAYER_MPV_DRM_MODE_PAL`, `DVDPLAYER_DRM_CONNECTOR`).

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
