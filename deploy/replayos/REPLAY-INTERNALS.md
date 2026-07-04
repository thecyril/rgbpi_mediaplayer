# RePlay binary — reverse-engineering reference

Notes on the internals of RePlayOS's front-end binary (`/opt/replay/replay`,
ARM64, **not stripped** — symbols are present, which is what made all of this
tractable). Everything here was reverse-engineered while porting
`rgbpi_mediaplayer` and restoring the RGB-Pi OS 4 CRT experience on a Pi 4 +
Sony PVM-14M4E. Addresses are from the build with
`BuildID 8ba4eef3…` / stock md5 `b93a278bf4ca444f8a74492a9123d8ad`; they will
shift across RePlayOS updates — re-derive with `nm /opt/replay/replay` and
`objdump -d`. All the patches in this folder re-verify their target bytes
before writing, so a moved symbol fails loudly instead of corrupting.

## RGB-Pi 2 DAC (Chrontel CH7101) — i2c control

- Transparent HDMI→RGBS DAC, **no scaler**. i2c address **0x78** (7-bit, in the
  reserved range → needs `i2cset -a` / `I2C_SLAVE_FORCE`). Bus **i2c-20** (the
  HDMI0 DDC bus on kernel 6.12; older kernels numbered it 13). Probe it:
  `for b in /dev/i2c-*; do i2cget -y -a ${b#/dev/i2c-} 0x78 && echo $b; done`.
- **Paged registers** via register `0x00` (page select):
  - page 0: `0x61` = sync status (**0xff = locked**, anything else = drift/dropout,
    e.g. 0xef/0x18/0xc0), `0x60` = reset (0x00 assert / 0xff deassert — a reset
    alone makes things *worse*; it needs a fresh modeset afterwards).
  - page 4: `0xB5` = **csync mode** — `0x00` separated H/V, `0x0C` XOR,
    **`0x06` AND** (what a PVM wants; matches replay.cfg `video_crt_csync_mode=0`).
- The chip rebuilds its output config on every HDMI modeset and falls back to
  separated sync (rolling picture / shimmer) even though 0xB5 still *reads* the
  old value. It must be re-written after each modeset. RePlay does this at its
  own video init only — later drift is uncorrected, hence `rgbpi-csync.service`.
- Relevant symbols: `set_csync_mode` @ 0x2eec68 (writes page4/0xB5),
  `init_rgbpi_dac.isra.0` @ 0x2ec180, `drm_dac_init` @ 0x2f438c. Both csync
  writers early-out when `is_replay_hacked()` is true (see anti-tamper below).

## Display pipeline (15 kHz safety)

- RePlay ignores the EDID mode list: it **fabricates its own 15 kHz USERDEF
  modes** (`generic_15` CRT engine) and applies them atomically. Its UI mode
  measured: `2560x240p 52.085 MHz, 3326 px total → H = 15.658 kHz`, negative
  syncs. On EDID failure it falls back to `640x480@60` (31.5 kHz — dangerous on
  a 15 kHz CRT), so a *valid* EDID matters.
- Other clients (SDL, mpv) pick from the connector list → a custom EDID is
  required. `build_edid.py` makes a **15 kHz-only** EDID (only 2560x240 &
  2560x288 exposed) so nothing can select a >15.7 kHz mode. The firmware early
  boot is forced to 15 kHz separately via `config.txt` `hdmi_mode=87` +
  `hdmi_timings` (see the main README's "15 kHz safety" section).

## In-game menu & core options

The in-game menu (opened during a game) is built by these functions:

- `game_launcher` @ 0x2db3a4-region — handles both the initial game/core load
  and the in-game menu actions. Menu items are string-keyed: `resume`,
  `soft_reset`, `hard_reset`, `save_state`/`load_slot_N`, `game_list`,
  `screenshot`, `core_options` (label **"SYSTEM SETTINGS"**), `core_input`
  ("SYSTEM INPUT"), `core_disk` ("SYSTEM MEDIA"), `replay_options`
  ("REPLAY OPTIONS"). Note **`core_options` → "SYSTEM SETTINGS"** is where core
  variables live.
- `gen_game_opt_entries` @ 0x2cd984 — builds the SYSTEM SETTINGS list. For each
  core option it calls `should_show_core_option` @ 0x2c86e0.
- **`should_show_core_option`** @ 0x2c86e0 — returns 1 (show) / 0 (hide). It
  checks the option key against a hard-coded blacklist AND a second internal
  condition; **both** must pass for an option to appear.

### Core-option blacklists

- `options_black_list_basic` @ 0x549960 — 177 `char*` entries.
- `options_black_list_advanced` @ 0x549190 — 141 entries.
- `blacklist` (global ptr) @ 0x547c10 + `blacklist_size` @ 0x401158 — the active
  list is selected at runtime (basic vs advanced); its static initializer is a
  meaningless reloc artifact (`0x338be0` → "0000…").
- The Blargg NTSC filters — `snes9x_blargg`, `fceumm_ntsc_filter`,
  `genesis_plus_gx_blargg_ntsc_filter` — are in **both** lists. **Removing them
  from the arrays is NOT enough**: the second internal gate in
  `should_show_core_option` still hides them. RePlay also does **not** implement
  `RETRO_ENVIRONMENT_SET_CORE_OPTIONS_DISPLAY` ("not implemented!"), so a core's
  own "show advanced" dependency is irrelevant here.
- `allow_video_filter` @ 0x2acf40 is a **function** (reads a cfg key), unrelated
  to the core blacklist — it gates RePlay's own GPU `video_filter`, not blargg.
- **Working fix** (`show_all_core_options.py`): overwrite the entry of
  `should_show_core_option` with `mov w0,#1; ret` (8 bytes) → every core option
  shows, live-selectable, like RetroArch. See that script + the README.

### Where option values are stored

- Per-system: `/media/sd/config/settings/system/{crt,lcd}/<system>.cfg`
  (format `%s%s%s/%s.cfg`). Per-game override: `…/settings/game/{crt,lcd}/%s@%s.cfg`.
- Written lazily (first time a setting is saved from the menu); the dir is empty
  on a fresh install. RePlay parses core options (`GET_VARIABLE`/`SET_VARIABLE`
  implemented) and feeds them to the core; the blacklist only affects *display*,
  not whether a cfg-set value is honored.

## Menu integration (launching our player)

- Extra menu (`/media/sd/roms/_extra/*.lr`, copied from `/opt/replay/extra`):
  `.lr` names are mapped to cores by a **hard-coded** table (`pibench.lr`,
  `audio_video_test.lr` only); `.sh` run via `run_script` is **blocked**
  ("FORBIDDEN!!!"). The name→`.so` mapping goes through the editable
  `/opt/replay/cores/cores.cfg` (`get_core(name)` reads the `[name]` section's
  `hi`/`mid`/`low` → the `.so`, then `core_load` dlopen's it).
- **Alpha Player** is a first-class *main-menu* system (tile gated by
  `view_player="true"`): it lists `/media/sd/roms/alpha_player/` by media
  extension and launches files through `cores.cfg [alpha_player]`. We point that
  section at our stub core + drop a 0-byte `MEDIA PLAYER.mkv` launcher file →
  the tile launches our player. No binary patching needed for this.

## Anti-tamper (`is_replay_hacked`)

- `is_replay_hacked` @ 0x2ea700 returns true when the binary's signature check
  fails (`replay_insider_*`, EVP_DigestVerify against
  `replay_insider_public_key_pem` @ 0x401170). Any modification to
  `/opt/replay/replay` trips it.
- **7 call sites** and their (benign-for-us) effects:
  - `main` ×2 — sets the flag / cosmetic warning at startup.
  - `set_csync_mode` @ 0x2eec70, `init_rgbpi_dac` @ 0x2ec1bc — **csync writes
    become no-ops** ⇒ the reason we run `rgbpi-csync.service` (our own watchdog
    keeps the CH7101 synced regardless).
  - `cheevos_on_game_loaded` — RetroAchievements (hardcore) gating. Irrelevant.
  - `replay_http_handle_client` — the HTTP API. Irrelevant.
  - `game_launcher` — gates a menu detail, **not** the launch itself: games
    still boot and run with a patched binary (verified live).
- Net: patching the binary is safe here **because** our csync watchdog replaces
  the one function the anti-tamper disables that we care about. Every binary
  patch keeps a stock backup at `/opt/replay/replay.orig`; a RePlayOS update
  restores the stock binary (re-run `install.sh` to re-apply opt-in patches).

## Deploying a patched binary

You **cannot overwrite the running binary in place** (`scp`/write → `ETXTBSY`,
"text file busy"). Copy to a temp path, then **atomic `mv`** over it (rename
keeps the running process's old inode alive), then reboot:

```sh
cp /opt/replay/replay /opt/replay/replay.new
python3 <patch>.py /opt/replay/replay.new
mv -f /opt/replay/replay.new /opt/replay/replay && reboot
```

## Handy commands

```sh
# CH7101 sync health (0xff = locked)
i2cset -y -a 20 0x78 0x00 0x00; i2cget -y -a 20 0x78 0x61
# What is scanned out right now (H = clock/htotal)
grep 'mode:' /sys/kernel/debug/dri/*/state | grep -v '""'
# Re-derive symbol addresses after an update
nm /opt/replay/replay | grep -E 'should_show_core_option|is_replay_hacked|set_csync_mode|options_black_list'
```
