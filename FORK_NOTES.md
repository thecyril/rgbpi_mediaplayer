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
| — (TBD) | Plex-style playback HUD overlay (progress bar, pause icon, time, START hint) | `plex-style-playback-hud` | ✅ working on the Pi — ready to open upstream PR |

Each PR branch is a clean cherry-pick on top of `upstream/main` so it can be reviewed and merged independently. The corresponding feature is **also** present in this fork's `main`, sometimes via a slightly different combined commit; nothing depends on PR merge order.

### Plex-style playback HUD (branch `plex-style-playback-hud`)

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

These flip behaviour defaults that make sense on a CRT but might surprise a user running on a 1080p LCD. They live on `main` but were intentionally **not** PRified. See [`Local: flip playback defaults for CRT-first usage`](https://github.com/thecyril/rgbpi_mediaplayer/commit/833a892) for the actual diff.

| Setting | Upstream default | Fork default | Why |
|---|---|---|---|
| `PlaybackPrefs.deinterlace_mode` | `weave` | `bob` | DVD remuxes show a visible comb artifact with `weave`. The `bwdif … deint=interlaced` filter passes progressive frames through unchanged, so this is safe on every source. |
| `PlaybackProfile.video_sync` | `audio` | `display-resample` | `audio` sync occasionally drops a frame when the source frame rate doesn't divide the display refresh. `display-resample` resamples audio (≪ 0.1 % pitch shift, inaudible) and pins each video frame to a fresh vblank — visibly smoother. Held in a single `_DEFAULT_VIDEO_SYNC` constant so the four `PlaybackProfile` sites share one value. |
| `BOB_DEINTERLACE_FILTER` `mode=` | `send_field` | `send_frame` (env-overridable) | `send_field` outputs 60 progressive frames per second, doubling the VO thread's CPU cost. On a CRT 60 Hz interlaced display both modes look identical; halving the cost gives ~50 % more headroom on the Pi 4's VO thread, killing the residual jitter on MPEG-2 SD content. Override with `DVDPLAYER_BWDIF_MODE=send_field` on a progressive LCD setup. |
| `mpv --osd-font-size` | `36` *(from PR #4 once merged)* | `36` (same) | Same as PR #4 — the original `24` was ~8 px on a 240p output (illegible) and `65` (an earlier revision of PR #4) overflowed the 11-row START overlay on 240p. `36` lands ~12 px on 240p, ~24 px on 480i, ~54 px on 1080p. |

### Reverting per-user

- `playback_prefs.json` — edit `"deinterlace_mode"` back to `"weave"` to disable bob deinterlace.
- `DVDPLAYER_BWDIF_MODE=send_field` (env var, set in the launcher wrapper) — restore 60 fps progressive deinterlace.
- `_DEFAULT_VIDEO_SYNC` constant in `session.py` — change to `"audio"` if `display-resample` ever causes a regression.

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

_Last updated: 25 mai 2026 (HUD branch validated on the Pi)._
