# rgbpi_mediaplayer

Python implementation of the RGB-Pi media player, intended to run from the RGB-Pi
ports directory.

## Expected install path

```bash
/media/sd/roms/ports/rgbpi_mediaplayer
```

The launch scripts resolve `DVDPLAYER_APP_DIR` from their own location, so this
folder can be moved if needed.

## Standalone runtime

This folder is shipped as a standalone app package for Linux ARM.
No runtime `apt`, `pip`, or `npm` install steps are required by the launcher.

## Run

```bash
cd /media/sd/roms/ports/rgbpi_mediaplayer
./start_rgbpi_dvdplayer_python.sh
```

## Playback controls

While playing back a Plex / video file / DVD source, press **START** on the
gamepad to open the playback overlay menu. Available entries (vary by source):

- **TOGGLE PAUSE**
- **DVD MENU** (DVD only)
- **CHAPTER -/+** (DVD only)
- **AUDIO TRACK** — opens a sub-menu listing all audio tracks (language + title
  when available). UP/DOWN to navigate, ACCEPT to switch.
- **ENABLE SUBTITLES** — opens a sub-menu with OFF + each available subtitle
  track. UP/DOWN to navigate, ACCEPT to set.
- **INFORMATION** — overlay with current playback info.
- **RETURN TO BROWSER** — stop playback and go back to the file/library list.

Within a sub-menu (audio / subtitles), press **BACK / SELECT / START** to close
and return to playback.

When a file starts with multiple audio tracks, the player asks once which audio
track to use. That choice is remembered for the current app session so the next
episode can start with the same language automatically.

### Overlay text scaling

Subtitle and OSD font sizes, border thickness and bottom margin are passed to
mpv in its native "scaled pixels at a window height of 720" unit. mpv then
rescales them automatically to the actual output window height — so the same
constants give readable text on a 240p CRT, on a 480i interlaced output, and
on a 1080p TV without any manual tweak (roughly 11 % of the screen height for
subtitles, 9 % for OSD).

## Runtime files

Default runtime directory:

```bash
state/runtime
```

Key files:
- control socket: `state/runtime/rgbpi-dvdplayer-api.sock`
- state snapshot: `state/runtime/rgbpi-dvdplayer-state.json`
- player log: `state/runtime/rgbpi-dvdplayer-python.log`
- launch log: `state/runtime/rgbpi-dvdplayer-python-launch.log`

Useful environment overrides:
- `DVDPLAYER_APP_DIR`
- `DVDPLAYER_WINDOWED=1`
- `DVDPLAYER_CONTROL_SOCKET`
- `DVDPLAYER_STATE_PATH`
- `DVDPLAYER_DEBUG_LOG`
- `DVDPLAYER_MPV_LOG`

## API helper

```bash
./dvdplayer_api.py status
./dvdplayer_api.py wait-ready 15
./dvdplayer_api.py show-overlay start
./dvdplayer_api.py screenshot /tmp/shot.png
./dvdplayer_api.py remote-play-json '{"title":"Trailer","url":"https://example/media.mp4","kind":"video_file"}'
```

## YouTube TV Code (Standalone)

This app now expects YouTube TV Code support to be self-contained inside this
folder, without runtime `apt/pip/npm` installs.

Bundled runtime paths:
- bundled MPV binary: `bin/mpv`
- bundled Linux ARM rootfs libs (incl. `libdvdcss`): `runtime/linux-arm64-rootfs/`
- sidecar script: `runtime/youtube_receiver/sidecar.mjs`
- sidecar packages: `runtime/youtube_receiver/node_modules/`
- vendored `yt_dlp` module: `src/dvdplayer_python/vendor/yt_dlp/`
- bundled `yt-dlp` fallback binary: `runtime/yt_dlp/linux-arm/yt-dlp`
- bundled Linux ARM Node runtime: `runtime/node/linux-arm/node`

Expected target:
- Linux ARM devices (RGB-Pi style deployment)

Optional overrides:
- `DVDPLAYER_YOUTUBE_NODE_BIN` (explicit Node binary override)
- `DVDPLAYER_YOUTUBE_SIDECAR_CMD` (fully custom sidecar command)
- `DVDPLAYER_YOUTUBE_DEVICE_NAME`
- `DVDPLAYER_YOUTUBE_SCREEN_NAME`
- `DVDPLAYER_YOUTUBE_FORMAT`
- `DVDPLAYER_YTDLP_BIN` (external fallback binary, development only)
- `DVDPLAYER_MPV_BIN` (override bundled mpv binary)

Quick validation:

```bash
./dvdplayer_api.py youtube-link-start
./dvdplayer_api.py status
```

## Runtime checker

`runtime/check_runtime_bundle.sh` validates that bundled runtime files are present.
The launcher runs this check at startup and aborts if the bundle is incomplete.
