# Fork notes — `thecyril/rgbpi_mediaplayer`

Living document. Track everything this fork carries on top of
[`joeblack2k/rgbpi_mediaplayer`](https://github.com/joeblack2k/rgbpi_mediaplayer) — why each change exists, what is upstream-pending vs. local-only, and how to operate the dev workflow.

Hardware target this fork is tuned for:
**Raspberry Pi 4 + RGB-Pi OS (Debian Bullseye, kernel 5.15.45-v8+) + `vc4-vga666-argon` DPI DAC → 320×240 RGB → Sony PVM CRT.**
Most defaults assume that pipeline; anything that hurts a 1080p LCD pipeline is gated behind a config flag (see [Reverting per-user](#reverting-per-user)).

---

## Workflow

| Where | What lives there |
|---|---|
| `joeblack2k/rgbpi_mediaplayer` | upstream, source of truth |
| `thecyril/rgbpi_mediaplayer` (`main`) | dev trunk — has the 4 upstream-pending features merged + the local defaults flip |
| Feature branches on the fork | each upstream-pending PR is also a branch here (`proportional-overlay-text-sizes`, `mpeg2-multi-thread-decode-and-cache`, `information-shows-storage-and-display-resolution`, `plex-folder-back-navigation`) |
| `/media/sd/roms/ports/rgbpi_mediaplayer/` on the Pi | tracks **fork** `main` (not upstream). `origin` is repointed to `thecyril/...`. |

### Dev cycle

```bash
# Mac — start a feature
cd ~/code/rgbpi_mediaplayer
git checkout main && git pull
git checkout -b some-feature
# ...edit, commit...
git push -u origin some-feature

# Optional: test on the Pi without merging to main
ssh rgbpi 'cd /media/sd/roms/ports/rgbpi_mediaplayer && git fetch && git checkout some-feature'

# Mac — once happy, merge into the fork's main
git checkout main
git merge --no-ff some-feature -m "Merge: …"
git push

# Pi — pick the merge up
ssh rgbpi 'cd /media/sd/roms/ports/rgbpi_mediaplayer && git checkout main && git pull --ff-only'
```

### Picking up upstream changes

```bash
cd ~/code/rgbpi_mediaplayer
git fetch upstream
git merge upstream/main          # resolve conflicts on local-default lines if upstream touched them
git push origin main
# then `git pull` on the Pi
```

### Branch hygiene

The fork keeps a branch per still-open upstream PR (`proportional-overlay-text-sizes`, `mpeg2-multi-thread-decode-and-cache`, `information-shows-storage-and-display-resolution`, `plex-folder-back-navigation`). They look "unmerged" in a GUI like GitKraken because GitHub references them from the PR pages — their content is already in `main` via the merge commits, but the branch ref itself has to stay alive until the maintainer merges or closes the PR.

Rule of thumb:

- **Upstream-pending PR branch** — keep it. Delete only after the corresponding PR is merged or closed upstream.
- **Internal work branch** (e.g. `plex-back-navigation`, `local-pi-defaults` were used during dev) — delete as soon as the work is merged into `main`. Pushed-but-no-PR-attached branches can be removed both locally (`git branch -D <name>`) and on the remote (`git push origin --delete <name>`).
- **Anything older than the most recent merge into `main`** that doesn't match the two rules above is stale; nuke it.

---

## Upstream-pending PRs

| # | Title | Branch | Status |
|---|---|---|---|
| [#1](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/1) | Add AUDIO TRACK overlay menu | `add-audio-track-overlay` | ✅ merged + extended by maintainer in PR #2 |
| [#4](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/4) | Replace illegible hardcoded mpv overlay sizes with 720-baseline constants | `proportional-overlay-text-sizes` | 🕐 pending — fixed up after Codex review (now `--osd-font-size=36`) |
| [#5](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/5) | Multi-thread MPEG-2 decode + larger demuxer cache | `mpeg2-multi-thread-decode-and-cache` | 🕐 pending |
| [#6](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/6) | Show both storage and display resolution in INFORMATION | `information-shows-storage-and-display-resolution` | 🕐 pending |
| [#7](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/7) | Per-folder BACK navigation for Plex browsing | `plex-folder-back-navigation` | 🕐 pending |
| [#8](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/8) | Plex-style playback HUD overlay (incl. persistent-IPC bugfix) | `playback-hud-overlay` | 🕐 pending — working on the Pi |

Each PR branch is a clean cherry-pick on top of `upstream/main` so it can be reviewed and merged independently. The corresponding feature is **also** present in this fork's `main`, sometimes via a slightly different combined commit; nothing depends on PR merge order.

### Plex-style playback HUD (branch `playback-hud-overlay`, PR #8)

A bottom-band HUD that shows the title, a progress bar with playhead, current
time / duration, a play/pause glyph, and a "START menu" hint. It auto-hides
4 seconds after the last input — same UX as Plex Web's peek-the-timeline.

Implementation lives in [`src/dvdplayer_python/playback/hud.py`](src/dvdplayer_python/playback/hud.py) (~410 lines, snapshot-rendered ASS):

- Rendered via mpv's `osd-overlay` JSON-IPC command with ASS markup — same
  primitive uosc uses. No extra process, no extra dependency.
- Reference resolution **1280×720** so it scales identically on a 240p CRT
  and a 1080p LCD; same baseline as the `--osd-font-size=36` constant from
  PR #4.
- Owns mpv overlay slot id `7` (1 and 2 are taken by the existing
  badge / info overlays).
- Lazy-constructed on `PlaybackSession.hud` so the ffplay backend never
  pulls the module in.
- **Snapshot semantics.** `flash()` sends one `osd-overlay` and that's it.
  `tick()` only handles auto-hide. mpv keeps the overlay on screen until
  we send `format=none`. Steady-state IPC cost: 1 send per user input,
  0 between.
- `flash()` on ACCEPT (pause toggle), LEFT/RIGHT (±30 s seek), at
  `start_playback()` (to show the title), and on `_close_overlay()` so the
  user is reoriented when returning to plain playback. Hidden while a
  START / AUDIO / SUBTITLE / INFORMATION text menu is up.
- Standalone-testable: `PlaybackHUD` takes a `send_command` callable and a
  `get_state` callable, so a unit test can assert the exact IPC payload
  without spawning mpv.

#### Lesson learned: persistent IPC socket is mandatory for `osd-overlay`

Initial attempts had the HUD flicker (5 Hz re-render) or disappear within
~1 frame (snapshot). After research, [mpv's `input.rst` spells it out](https://github.com/mpv-player/mpv/blob/v0.32.0/DOCS/man/input.rst):

> "If the libmpv client is destroyed, all overlays associated with it are
> also deleted. In particular, connecting via `--input-ipc-server`, adding
> an overlay, and disconnecting will remove the overlay immediately again."

`PlaybackSession._send` was opening a fresh Unix socket per command
(`with socket(...) as s:`). Every `osd-overlay` we sent was tied to that
short-lived libmpv client and got dropped on the next frame. The fix was
structural — `PlaybackSession` now holds **one socket open for the whole
session** (`self._ipc_sock`) with lazy reconnect on socket errors. The
HUD code then collapses to pure snapshot semantics.

This is the same reason `uosc` and the built-in `osc.lua` are in-process
Lua scripts: the "client" is mpv itself, so the overlay lives forever.

---

## Local-only changes (not upstream-bound)

The fork currently keeps these local defaults different from upstream:

| Setting | Upstream default | Fork default | Why |
|---|---|---|---|
| `mpv --osd-font-size` | `36` *(from PR #4 once merged)* | `36` (same) | Same as PR #4 — the original `24` was ~8 px on a 240p output (illegible) and `65` (an earlier revision of PR #4) overflowed the 11-row START overlay on 240p. `36` lands ~12 px on 240p, ~24 px on 480i, ~54 px on 1080p. |
| Film-rate handling (23.976 / 24 fps source) | `720x576i` (PAL 50Hz) at native speed → ratio 2.085 → **irregular** 2:2:2:2:2:2:2:2:2:2:2:2:3 cadence (hiccup every ~12 frames, very visible judder) | **PAL speedup** by default: `720x576i` (PAL 50Hz) **+ `--speed=25/src_fps` + `--audio-pitch-correction=no`** → effective rate 25 fps, ratio `50/25 = 2.000` exact → **perfect 1:2 cadence, zero judder**. Audio is pitched +4 % (~0.7 semitones up) as a side effect — the same shift every European broadcast of a Hollywood film used from 1960 to ~2010. Override: `DVDPLAYER_PAL_SPEEDUP=0` falls back to NTSC 60Hz routing (`720x480i`, ratio `60/23.976 = 2.503` → regular 2:3 pulldown, audio at correct pitch). |
| NTSC-rate handling (29.97 / 59.94 fps source) | `720x480i` (NTSC 60Hz) at native speed → ratio 2.002 → mpv's audio sync drops/duplicates a frame every ~50 s to keep up with the 0.067 % drift → an intermittent **micro-hiccup** | **NTSC speedup** by default: `720x480i` + `--speed=30/src_fps` (= 1.001) + `--audio-pitch-correction=no` → effective rate 30 fps, ratio `60/30 = 2.000` exact → **perfect cadence**. Audio is pitched +0.017 semitones (well below human detection threshold of ≈ 0.05 semitones — genuinely inaudible). Symmetric to PAL speedup. Override: `DVDPLAYER_NTSC_SPEEDUP=0` or in-app SETTINGS → "30P SMOOTHING" OFF. |

### Reverted: motion defaults flip (commit 833a892 / merge c20029e + gating 0d7e2cd)

We previously flipped `PlaybackPrefs.deinterlace_mode` to `bob` and the global
`--video-sync` to `display-resample` (later: gated `display-resample` on
field-rate matching), thinking those were the right CRT-friendly defaults.
**A back-to-back A/B against upstream main showed those defaults clearly
regressed motion smoothness on the Pi 4 / vc4 KMS DRM pipeline**, on both 480i
and 480p content. We reverted to the upstream behaviour. Likely culprits:

- `bob` adds `bwdif` to the filter chain *unconditionally*. Even though
  `deint=interlaced` passes progressive frames through, just having the
  filter in the graph adds a pipeline stage that introduces frame-time
  micro-variability. On a 480i source going to a 480i CRT output, the
  upstream `weave` path (`decode → scale → output`, no filter) is
  field-perfect — the CRT scans the interlaced fields natively.
- `display-resample` relies on the reported display-fps, which is
  documented as inaccurate on vc4 KMS DRM (raspberrypi/firmware#960).
  Empirically the math we relied on (the wiki's "zero dropped/duplicated
  frames" promise on matching ratios) doesn't hold on this hardware.
  Audio sync — mpv's default and the manual's "most robust mode" — is
  smoother in practice.

### Reverting per-user (escape hatches for the per-session knobs we kept)

- `playback_prefs.json` — set `"deinterlace_mode": "bob"` (or via the prefs
  UI) to enable bwdif. Defaults to `"weave"`.
- `DVDPLAYER_BWDIF_MODE=send_frame` (env var) — when bob is enabled,
  halve the VO thread cost at the price of losing the doubled 60p motion
  cadence. Defaults to `"send_field"` (upstream behaviour, smoother 60p
  on progressive displays).
- `DVDPLAYER_VIDEO_SYNC=display-resample` (env var) — force
  `display-resample` per session for users who want to test it on a
  fixed-rate LCD setup. Defaults to `"audio"`.
- **24P SMOOTHING** (in-app SETTINGS menu) — toggle PAL speedup
  ON/OFF without restarting. Stored in `playback_prefs.json`
  (`"pal_speedup": true/false`). Use the menu for permanent change,
  `DVDPLAYER_PAL_SPEEDUP=0/1` env var for a per-launch override
  (env always wins over prefs). Default: ON.
- **30P SMOOTHING** (in-app SETTINGS menu) — same for the NTSC
  speedup (29.97/59.94 → 30/60). Stored as `"ntsc_speedup"` in
  `playback_prefs.json`. Env var `DVDPLAYER_NTSC_SPEEDUP=0/1`.
  Default: ON. Audio shift is +0.017 semitones (inaudible) so
  there's no real downside to leaving it on; the OFF case is
  available mainly for A/B comparison.

---

## Hardware-specific patches kept outside of git

Some setup steps had to be done in-place on the Pi (no Python code involved). They are not in the repo but are mandatory for the player to run on this hardware:

- `ldconfig -v` on the bundled `linux-arm64-rootfs/usr/lib/aarch64-linux-gnu/` — the bundle ships `.so.X.Y.Z` files but no `.so.X` SONAME symlinks, so `bin/mpv` initially fails to load `liblua5.2.so.0` and friends. Running `ldconfig` once on the directory creates the symlinks.
- `state/plex_state.json` — written by the player after a PIN link, sometimes records a `plex.direct` URI for a stale machine identifier (Plex.tv keeps resolving old MAC-style server IDs in its `/api/v2/resources` response). When that happens, hand-edit the file to set:
  ```json
  "server_uri": "http://192.168.1.3:32400"
  ```
  pointing at the current LAN IP of the active Plex server (machine identifier `cfd904f5…` for this setup).
- `/etc/asound.conf` — kept on the bypass-EQ config (`pcm.!default → plug → sysdefault:0`). The original equaliser config (`/etc/asound.conf.backup-pre-godot-fix`) caused xruns on the kernel-6.1 ALSA stack during a brief sinistre and was reverted; restoring it once the LCD/USB 5.1 setup is wired in will need a new asound.conf anyway.

---

## Performance notes (RGB-Pi specific)

These are not changes to the code, but they are why the code looks the way it does:

- **MPEG-2 software decode**: Pi 4 V4L2M2M only covers H.264 / HEVC. DVD remuxes (`mpeg2video`) are decoded entirely in software. `--vd-lavc-threads=0` (PR #5) lets ffmpeg spread the work across all four cores. Without it, a single CPU thread spikes to ~95 % on a `720x480i + bwdif` workload on a 2.4 GHz overclocked Pi 4 and you get visible micro-jitter on the busy scenes.
- **480i interlaced output via Mesa V3D + GBM is unstable on Pi 4** when SDL2 KMSDRM allocates the EGL surface. mpv itself uses raw DRM/KMS (no GBM) for the scanout so it copes fine; SDL2-based games (xash3d, reVC) crash in `gladLoadGLES2Loader` on the same setup. That's why `target_mode=720x480i` works for mpv but is unusable for the Half-Life port. See `Volumes/Disk1/Games/RGBPI/rgbpi-display-modeswitching-guide.md` for the full pipeline story.
- **Pi 4 overclock currently in use** (in `/boot/config.txt` on this device):
  ```
  arm_freq=2400
  gpu_freq=850
  over_voltage=15
  dvfs=3
  temp_soft_limit=80
  ```
  Throttled bit stays at `0x0` under a 4-core stress test with a max temperature of 62.8 °C, thanks to the Argon ONE case + active fan.

---

## Open follow-ups

- Extend the `_push_list_nav()` calls (PR #7) to the **network browser** (SMB) and the **local file browser** (`browser_local`). Same pattern, ~3 extra lines. Would be a one-line follow-up PR.
- Plumb `--demuxer-max-bytes` / `--demuxer-readahead-secs` from env vars (`DVDPLAYER_MPV_CACHE_SIZE`, `DVDPLAYER_MPV_READAHEAD_SECS`) for users who want to tune them without editing source.
- Investigate the `--vo=drm` jitter further on `720x480i@60` mode — once PR #5 is merged the `mpv/vo` thread is the only single-thread bottleneck left. A profile run with `perf top` against the running mpv would tell whether it's pixel-format conversion or actual scanout.
- The Plex token written to `plex_state.json` after PIN linking is sometimes scoped to a stale `machineIdentifier`. Add an idempotent migration step that, when the token works but `server_uri` is unreachable, asks Plex.tv `/api/v2/resources` for a fresh URI for the current `machineIdentifier`.

---

## Audio track menu (PR #1, already merged)

The maintainer accepted [PR #1](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/1) and then expanded it in [PR #2](https://github.com/joeblack2k/rgbpi_mediaplayer/pull/2) with **session-wide audio track persistence + language-alias matching** (FR/FRE/FRA, EN/ENG, JPN/JA, …). Both are now in `upstream/main` and therefore in this fork too. Nothing to maintain on this side.

---

_Last updated: 25 mai 2026 (reverted motion defaults to upstream after A/B comparison)._
