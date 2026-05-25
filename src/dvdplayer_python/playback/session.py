from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dvdplayer_python.core.debuglog import log_event
from dvdplayer_python.core.models import PlaybackKind, PlaybackPrefs, PlaybackSource

RGBPI_CONNECTOR_NAME = os.environ.get("DVDPLAYER_DRM_CONNECTOR", "VGA-1")
OVERLAY_MAIN_ID = 1
OVERLAY_BADGE_ID = 2
FFPROBE_TIMEOUT_SECS = float(os.environ.get("DVDPLAYER_FFPROBE_TIMEOUT", "6"))
LIGHT_NORMALIZATION_FILTER = "lavfi=[acompressor=threshold=-16dB:ratio=2:attack=20:release=250:makeup=2]"
HIGH_NORMALIZATION_FILTER = "lavfi=[loudnorm=I=-18:TP=-2:LRA=11]"
# Bob deinterlace filter (only used when PlaybackPrefs.deinterlace_mode = "bob";
# the default is "weave" which leaves interlaced fields untouched — best for the
# CRT pipeline since the analog output already scans by fields).
#
# DVDPLAYER_BWDIF_MODE env var lets the rare opt-in user pick send_field
# (60p output, 2x VO thread cost) instead of send_frame (1x). On a CRT both
# modes look identical because the CRT itself scans by fields; on a progressive
# LCD setup, send_field gives smoother 60p motion at the cost of CPU.
_BWDIF_MODE = os.environ.get("DVDPLAYER_BWDIF_MODE", "send_field").strip() or "send_field"
BOB_DEINTERLACE_FILTER = f"bwdif=mode={_BWDIF_MODE}:parity=auto:deint=interlaced"
SMOOTH_FPS_FILTER = "fps=60000/1001"
CABLE_SMOOTH_BLEND_FILTER = "lavfi=[tblend=all_mode=average]"
_FFMPEG_FILTER_SUPPORT_CACHE: dict[str, bool] = {}


def _which(binary: str) -> Optional[str]:
    name = str(binary or "").strip().lower()
    env_key = {
        "mpv": "DVDPLAYER_MPV_BIN",
        "ffprobe": "DVDPLAYER_FFPROBE_BIN",
        "ffmpeg": "DVDPLAYER_FFMPEG_BIN",
        "modetest": "DVDPLAYER_MODETEST_BIN",
    }.get(name)
    preferred = os.environ.get(env_key) if env_key else None
    if preferred:
        p = Path(preferred)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    for path in os.environ.get("PATH", "").split(":"):
        candidate = Path(path) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _normalize_motion_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"cable_smooth", "cable", "ultra_smooth"}:
        return "cable_smooth"
    if text in {"smooth", "smooth_tv", "tv", "cable"}:
        return "smooth_tv"
    if text in {"authentic", "classic"}:
        return "authentic"
    return "smooth_tv"


def _normalize_volume_normalization(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"off", "none", "0"}:
        return "off"
    if text in {"high", "strong", "aggressive"}:
        return "high"
    return "light"


def _normalize_default_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"50", "50hz", "pal", "576", "576i"}:
        return "50hz"
    return "60hz"


def _normalize_deinterlace_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"bob", "bwdif", "on", "yes", "1"}:
        return "bob"
    return "weave"


def _resolved_video_sync() -> str:
    """Resolve the mpv `--video-sync` value, honouring an env-var override.

    The fork default is ``audio`` (same as upstream mpv) because our earlier
    experiments with ``display-resample`` regressed motion smoothness on the
    Pi 4 / vc4 KMS DRM stack — see FORK_NOTES.md "Why we reverted to upstream
    motion defaults". Users who want to try ``display-resample`` (smooth 60i
    on matched output, but judder on 24p→60Hz pulldown) can set
    ``DVDPLAYER_VIDEO_SYNC=display-resample`` on the launcher wrapper.
    """
    override = os.environ.get("DVDPLAYER_VIDEO_SYNC", "").strip().lower()
    if override in {"audio", "display-resample", "display-vdrop", "desync"}:
        return override
    return "audio"


def _ffmpeg_supports_filter(filter_name: str) -> bool:
    key = str(filter_name or "").strip().lower()
    if not key:
        return False
    cached = _FFMPEG_FILTER_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    ffmpeg = _which("ffmpeg") or ("/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").exists() else None)
    if not ffmpeg:
        _FFMPEG_FILTER_SUPPORT_CACHE[key] = False
        return False
    try:
        out = subprocess.check_output(
            [ffmpeg, "-hide_banner", "-filters"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3.0,
        )
        supported = re.search(rf"\b{re.escape(key)}\b", out.lower()) is not None
    except Exception:
        supported = False
    _FFMPEG_FILTER_SUPPORT_CACHE[key] = supported
    return supported


def resolve_motion_mode(prefs: Optional[PlaybackPrefs] = None) -> str:
    env_mode = os.environ.get("DVDPLAYER_CRT_MOTION_MODE")
    if env_mode:
        return _normalize_motion_mode(env_mode)
    if prefs is not None:
        return _normalize_motion_mode(prefs.motion_mode)
    return "smooth_tv"


def _is_pal_rate(fps: float) -> bool:
    return abs(fps - 25.0) < 0.2 or abs(fps - 50.0) < 0.2


def _is_film_rate(fps: float) -> bool:
    return abs(fps - 23.976) < 0.2 or abs(fps - 24.0) < 0.2


def _is_ntsc_rate(fps: float) -> bool:
    return (
        abs(fps - 29.97) < 0.2
        or abs(fps - 30.0) < 0.2
        or abs(fps - 59.94) < 0.2
        or abs(fps - 60.0) < 0.2
    )


def _resolve_alsa_device() -> str:
    """Return the ALSA device string mpv should open.

    Default is ``hw:0,0`` (card 0 = bcm2835 analog, the RGB-Pi DAC pins).
    Going through ``default`` would route us via the system's
    ``pcm.!default → plug → sysdefault:0`` chain, where the ``plug``
    type does linear-interpolation rate/format conversion — perceptibly
    worse than what mpv's internal resampler can do, and audible as a
    "metallic / muffled" timbre vs Kodi (Kodi's AudioEngine talks
    direct to ``hw:0,0`` for the same reason).

    Override with ``DVDPLAYER_ALSA_DEVICE`` (e.g. ``hw:1,0`` for HDMI-0,
    ``hw:2,0`` for HDMI-1, ``plughw:0,0`` if ``hw:0,0`` is held by
    something else and you need ALSA's softer conversion).
    """
    override = os.environ.get("DVDPLAYER_ALSA_DEVICE", "").strip()
    return override or "hw:0,0"


def _resolve_bool_pref(
    env_var: str,
    pref_name: str,
    prefs: Optional[PlaybackPrefs] = None,
    default: bool = True,
) -> bool:
    """Resolve a bool setting: env var > prefs > default. Shared by speedups."""
    env = os.environ.get(env_var, "").strip()
    if env != "":
        return env != "0"
    if prefs is not None:
        return bool(getattr(prefs, pref_name, default))
    return default


def _pal_speedup_enabled(prefs: Optional[PlaybackPrefs] = None) -> bool:
    """Is the European "PAL speedup" trick allowed for film-rate sources?

    Resolution: ``DVDPLAYER_PAL_SPEEDUP`` env > ``prefs.pal_speedup`` > True.

    When enabled, 23.976 / 24 fps sources are routed to PAL 50 Hz output
    AND played at exactly 25 fps via ``--speed=25/src_fps``, giving a
    mathematically perfect 1:2 vsync cadence (zero judder). Audio is
    pitched +4 % (~0.7 semitones) as a side effect — the same shift
    every European TV broadcast of a Hollywood film used from 1960 to
    2010. When disabled, film rate falls back to NTSC 60 Hz with the
    classical 2:3 pulldown (regular judder, audio at correct pitch).
    Toggled in-app via SETTINGS → "24P SMOOTHING".
    See FORK_NOTES "Film-rate handling".
    """
    return _resolve_bool_pref("DVDPLAYER_PAL_SPEEDUP", "pal_speedup", prefs, default=True)


def _ntsc_speedup_enabled(prefs: Optional[PlaybackPrefs] = None) -> bool:
    """Is the NTSC speedup trick allowed for 29.97 / 59.94 fps sources?

    Resolution: ``DVDPLAYER_NTSC_SPEEDUP`` env > ``prefs.ntsc_speedup`` > True.

    Symmetric to the PAL speedup, but for the NTSC-side drift: 29.97 (=
    30000/1001) and 59.94 (= 60000/1001) sources played on a 60 Hz output
    naturally drift by 0.1 % against the display vblank, forcing mpv to
    drop/duplicate a frame roughly every 50 s with the default ``audio``
    sync (visible as an intermittent micro-hiccup). Speeding the source
    up by exactly that 0.1 % matches the output rate perfectly (1:1 or
    1:2 cadence). The audio side-effect is +0.017 semitones — below the
    human pitch-detection threshold (≈ 0.05 semitones for a trained
    ear), genuinely inaudible.

    Toggled in-app via SETTINGS → "30P SMOOTHING".
    """
    return _resolve_bool_pref("DVDPLAYER_NTSC_SPEEDUP", "ntsc_speedup", prefs, default=True)


def _pal_speedup_factor(
    fps: Optional[float],
    target_mode: Optional[str],
    prefs: Optional[PlaybackPrefs] = None,
) -> Optional[float]:
    """Return the ``--speed`` factor for PAL speedup, or ``None`` if N/A.

    Returns ``None`` unless **all** of:
      - the feature is enabled (env var or prefs);
      - the source fps is known and matches film rate (23.976 / 24);
      - the **output target is PAL 50 Hz** (target_mode == "720x576i") —
        critical, because the whole point of the speedup is to convert
        24 → 25 → 1:2 clean at 50 Hz. On an LCD or other 60 Hz output,
        25 fps on 60 Hz gives ratio 2.4 (a *worse* cadence than the
        native 23.976 → 60 Hz 2:3 pulldown). Without this gate we'd
        actively make things worse on the LCD pipeline.

    The caller is responsible for setting ``--audio-pitch-correction=no``
    when applying the returned speed.
    """
    if not _pal_speedup_enabled(prefs):
        return None
    if fps is None or fps <= 0:
        return None
    if not _is_film_rate(fps):
        return None
    if target_mode != "720x576i":
        return None
    return 25.0 / float(fps)


def _ntsc_speedup_factor(
    fps: Optional[float],
    target_mode: Optional[str],
    prefs: Optional[PlaybackPrefs] = None,
) -> Optional[float]:
    """Return the ``--speed`` factor to bump 29.97/59.94 to 30/60, or None.

    Source fps     | speedup       | rounded-up effective rate
    ---------------|---------------|--------------------------
    29.97003       | 30/29.97003   | 30.0 → ratio 60/30 = 2.000 exact
    59.94006       | 60/59.94006   | 60.0 → ratio 60/60 = 1.000 exact
    30.000 / 60.000 (already exact) → None (no-op)
    any other rate → None

    Gated on target_mode == "720x480i" (NTSC 60 Hz) for the same reason
    as :func:`_pal_speedup_factor`: applying the speedup on an LCD with
    a non-60Hz refresh would actively hurt cadence.
    """
    if not _ntsc_speedup_enabled(prefs):
        return None
    if fps is None or fps <= 0:
        return None
    if target_mode != "720x480i":
        return None
    # Identify the nearest integer multiple of 60 that the source rate
    # *almost* equals. 60/fps ≈ 2 for ~29.97, ≈ 1 for ~59.94.
    n = round(60.0 / float(fps))
    if n < 1 or n > 4:
        return None
    target = 60.0 / n
    factor = target / float(fps)
    # Only return a non-trivial factor — skip the no-op case where the
    # source is already exact (30.000 / 60.000) so we don't add useless
    # --speed=1.0 and --audio-pitch-correction=no args.
    if abs(factor - 1.0) < 5e-4:
        return None
    # Sanity: don't apply if the source rate is *far* from the target
    # (means it wasn't actually 29.97/59.94 but some unrelated rate
    # that happened to round). Cap at 0.5 % drift.
    if abs(factor - 1.0) > 5e-3:
        return None
    return factor


def _speedup_for_source(
    fps: Optional[float],
    target_mode: Optional[str],
    prefs: Optional[PlaybackPrefs] = None,
) -> Optional[float]:
    """Pick the right speedup factor for this source/output, or ``None``.

    PAL and NTSC speedups are mutually exclusive by construction (each
    is gated on a specific target_mode), so we just try one then the
    other and take whichever returns a value.
    """
    pal = _pal_speedup_factor(fps, target_mode, prefs)
    if pal is not None:
        return pal
    return _ntsc_speedup_factor(fps, target_mode, prefs)


def _desired_output_mode(
    width: int,
    height: int,
    fps: Optional[float],
    prefs: Optional[PlaybackPrefs] = None,
) -> Optional[str]:
    if width <= 400 and height <= 300:
        return None
    # Film rate (23.976 / 24 fps): the cadence is the hard problem here
    # because neither 50 nor 60 divides evenly. Two strategies:
    #
    #   PAL speedup ON  (default, DVDPLAYER_PAL_SPEEDUP unset or "1"):
    #     → 720x576i (PAL 50Hz). Combined with --speed=25/src_fps in
    #       _spawn_mpv, the effective rate is exactly 25 fps and the
    #       ratio 50/25 = 2.000 gives a perfect 1:2 cadence (zero
    #       judder). Audio is pitched +4% as a side effect.
    #
    #   PAL speedup OFF (DVDPLAYER_PAL_SPEEDUP=0):
    #     → 720x480i (NTSC 60Hz). Ratio 60/23.976 = 2.503 → strictly
    #       alternating 2-3 pulldown. Regular judder ("film look"),
    #       audio at original pitch.
    #
    # Both strategies are explicitly chosen — *not* the upstream default,
    # which was 720x576i without speedup, giving ratio 2.085 = an
    # irregular 2:2:2:2:2:2:2:2:2:2:2:2:3 cadence (a hiccup every ~12
    # frames). That irregular cadence is what the user was reporting as
    # "ça jitter" and is the worst of the three options.
    if fps is not None and _is_film_rate(fps):
        return "720x576i" if _pal_speedup_enabled(prefs) else "720x480i"
    if fps is not None and _is_pal_rate(fps):
        return "720x576i"
    if fps is not None and _is_ntsc_rate(fps):
        return "720x480i"
    if height >= 560:
        return "720x576i"
    if height >= 470 or width >= 640:
        return "720x480i"
    return None


def _mpv_drm_mode_value(target_mode: str) -> str:
    if target_mode == "720x576i":
        return "720x576@50"
    if target_mode == "720x480i":
        return "720x480@60"
    return target_mode


def _mpv_drm_connector_value(drm_target: DrmLaunchTarget) -> str:
    card = drm_target.card.strip().lower()
    if card.startswith("card") and card[4:].isdigit():
        return f"{card[4:]}.{drm_target.connector}"
    return drm_target.connector


def _friendly_mode_label(raw: str) -> str:
    norm = raw.lower().replace(" ", "")
    if "720x480" in norm:
        return "480i NTSC"
    if "720x576" in norm:
        return "576i PAL"
    if "640x480" in norm:
        return "480p"
    if "320x240" in norm:
        return "240p"
    return raw


def _monitor_pixel_aspect_for_mode(target_mode: Optional[str]) -> Optional[float]:
    if target_mode == "720x480i":
        return 8.0 / 9.0
    if target_mode == "720x576i":
        return 16.0 / 15.0
    return None


def _parse_frame_rate(raw: object) -> Optional[float]:
    text = str(raw or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return None
    try:
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            num = float(num_s)
            den = float(den_s)
            if den == 0:
                return None
            return num / den
        return float(text)
    except Exception:
        return None


def _probe_video_info(uri: str) -> Optional[VideoProbeInfo]:
    ffprobe = _which("ffprobe") or ("/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").exists() else None)
    if not ffprobe:
        return None
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,field_order",
        "-of",
        "json",
        uri,
    ]
    timeouts = [FFPROBE_TIMEOUT_SECS, max(FFPROBE_TIMEOUT_SECS * 2.0, 12.0)]
    for timeout in timeouts:
        try:
            out = subprocess.check_output(
                args,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except Exception:
            continue
        try:
            payload = json.loads(out)
            streams = payload.get("streams") or []
            if not streams:
                continue
            stream = streams[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            fps = _parse_frame_rate(stream.get("avg_frame_rate")) or _parse_frame_rate(stream.get("r_frame_rate"))
            field_order = stream.get("field_order")
            if width <= 0 or height <= 0:
                continue
            return VideoProbeInfo(width=width, height=height, fps=fps, field_order=str(field_order) if field_order else None)
        except Exception:
            continue
    return None


def _probe_video_fps(uri: str) -> Optional[float]:
    ffprobe = _which("ffprobe") or ("/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").exists() else None)
    if not ffprobe:
        return None
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        uri,
    ]
    timeouts = [FFPROBE_TIMEOUT_SECS, max(FFPROBE_TIMEOUT_SECS * 2.0, 12.0)]
    for timeout in timeouts:
        try:
            out = subprocess.check_output(
                args,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except Exception:
            continue
        try:
            payload = json.loads(out)
            streams = payload.get("streams") or []
            if not streams:
                continue
            stream = streams[0]
            fps = _parse_frame_rate(stream.get("avg_frame_rate")) or _parse_frame_rate(stream.get("r_frame_rate"))
            if fps is not None:
                return fps
        except Exception:
            continue
    return None


def _mode_from_fps_only(fps: Optional[float]) -> Optional[str]:
    if fps is None:
        return None
    if _is_film_rate(fps):
        return "720x576i"
    if _is_pal_rate(fps):
        return "720x576i"
    if _is_ntsc_rate(fps):
        return "720x480i"
    return None


def _probe_mpeg2_dimensions(path: Path) -> Optional[tuple[int, int]]:
    read_limit = 8 * 1024 * 1024
    try:
        with path.open("rb") as fh:
            data = fh.read(read_limit)
    except Exception:
        return None
    # MPEG-2 sequence header start code 0x000001B3.
    for i in range(max(0, len(data) - 7)):
        if data[i : i + 4] == b"\x00\x00\x01\xB3":
            width = (data[i + 4] << 4) | (data[i + 5] >> 4)
            height = ((data[i + 5] & 0x0F) << 8) | data[i + 6]
            if width > 0 and height > 0:
                return (width, height)
    return None


def _authored_video_ts_root(path: Path) -> Optional[Path]:
    if path.is_dir() and path.name.upper() == "VIDEO_TS":
        return path
    child = path / "VIDEO_TS"
    if child.is_dir():
        return child
    return None


def _probe_authored_dvd_dimensions(path: Path) -> Optional[tuple[int, int]]:
    root = _authored_video_ts_root(path)
    if not root:
        return None
    candidates: list[Path] = []
    for f in sorted(root.glob("*.VOB")):
        name = f.name.upper()
        if name == "VIDEO_TS.VOB" or (name.startswith("VTS_") and name.endswith("_1.VOB")):
            candidates.append(f)
    for candidate in candidates:
        dims = _probe_mpeg2_dimensions(candidate)
        if dims:
            log_event("authored_dvd_probe", path=str(candidate), width=dims[0], height=dims[1])
            return dims
    return None


def _target_mode_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> Optional[str]:
    fallback_default_mode = _normalize_default_mode(getattr(prefs, "default_mode", "60hz"))
    default_fallback_output_mode = "720x576i" if fallback_default_mode == "50hz" else "720x480i"
    if source.authored_dvd:
        p = Path(source.uri)
        if p.exists():
            dims = _probe_authored_dvd_dimensions(p)
            if dims:
                mode = _desired_output_mode(dims[0], dims[1], None, prefs=prefs)
                if mode:
                    log_event("video_timing_probe", title=source.title, kind=source.kind.value, width=dims[0], height=dims[1], fps=None, mode=mode, probe="authored_dvd")
                return mode
        info = _probe_video_info(source.uri)
        if info:
            mode = _desired_output_mode(info.width, info.height, info.fps, prefs=prefs)
            if not mode and info.field_order:
                field_order = info.field_order.lower()
                if field_order not in {"progressive", "unknown"}:
                    if info.height >= 560:
                        mode = "720x576i"
                    elif info.height >= 470:
                        mode = "720x480i"
            if mode:
                log_event(
                    "video_timing_probe",
                    title=source.title,
                    kind=source.kind.value,
                    width=info.width,
                    height=info.height,
                    fps=info.fps,
                    field_order=info.field_order,
                    mode=mode,
                    probe="authored_dvd_ffprobe",
                )
                return mode
        # CRT-safe default when authored DVD dimensions cannot be probed.
        fallback = os.environ.get("DVDPLAYER_DVD_FALLBACK_MODE", "").strip() or default_fallback_output_mode
        log_event("video_timing_probe", title=source.title, kind=source.kind.value, mode=fallback, probe="authored_dvd_fallback")
        return fallback

    if isinstance(source.hint_width, int) and isinstance(source.hint_height, int):
        mode = _desired_output_mode(source.hint_width, source.hint_height, source.hint_fps, prefs=prefs)
        if mode:
            log_event(
                "video_timing_probe",
                title=source.title,
                kind=source.kind.value,
                width=source.hint_width,
                height=source.hint_height,
                fps=source.hint_fps,
                mode=mode,
                probe="source_hint",
            )
            return mode
    hint_fps_mode = _mode_from_fps_only(source.hint_fps)
    if hint_fps_mode:
        log_event(
            "video_timing_probe",
            title=source.title,
            kind=source.kind.value,
            fps=source.hint_fps,
            mode=hint_fps_mode,
            probe="source_hint_fps",
        )
        return hint_fps_mode

    info = _probe_video_info(source.uri)
    if not info:
        fps_only = _probe_video_fps(source.uri)
        fps_only_mode = _mode_from_fps_only(fps_only)
        if fps_only_mode:
            log_event(
                "video_timing_probe",
                title=source.title,
                kind=source.kind.value,
                fps=fps_only,
                mode=fps_only_mode,
                probe="ffprobe_fps_only",
            )
            return fps_only_mode
        log_event(
            "video_timing_probe",
            title=source.title,
            kind=source.kind.value,
            mode=default_fallback_output_mode,
            default_mode=fallback_default_mode,
            probe="probe_missing_fallback",
        )
        return default_fallback_output_mode

    mode = _desired_output_mode(info.width, info.height, info.fps, prefs=prefs)
    if not mode and info.field_order:
        field_order = info.field_order.lower()
        if field_order not in {"progressive", "unknown"}:
            if info.height >= 560:
                mode = "720x576i"
            elif info.height >= 470:
                mode = "720x480i"

    if mode:
        log_event(
            "video_timing_probe",
            title=source.title,
            kind=source.kind.value,
            width=info.width,
            height=info.height,
            fps=info.fps,
            field_order=info.field_order,
            mode=mode,
            probe="ffprobe",
        )
    return mode


def _read_drm_mode(connector: Optional[str] = None) -> Optional[str]:
    pattern = f"card*-{connector}/mode" if connector else "card*-*/mode"
    for mode_file in sorted(Path("/sys/class/drm").glob(pattern)):
        try:
            mode = mode_file.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if mode:
            return mode
    # Older vc4 kernels expose connector "modes" but no active "mode" file.
    # Fall back to modetest and read the first active CRTC mode line.
    modetest = _which("modetest") or "/usr/bin/modetest"
    if Path(modetest).exists():
        try:
            out = subprocess.check_output(
                [modetest, "-p"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
            lines = out.splitlines()
            for idx, line in enumerate(lines):
                # Active CRTC lines have a non-zero size, e.g. "(720x480)".
                if re.search(r"\(\d+x\d+\)\s*$", line) and "(0x0)" not in line:
                    if idx + 1 < len(lines):
                        m = re.search(r"#0\s+([0-9]+x[0-9]+i?)", lines[idx + 1])
                        if m:
                            return m.group(1)
        except Exception:
            pass
    return None


@dataclass
class DrmLaunchTarget:
    card: str
    connector: str
    mode_name: str


@dataclass
class VideoProbeInfo:
    width: int
    height: int
    fps: Optional[float]
    field_order: Optional[str] = None


@dataclass
class PlaybackProfile:
    motion_mode: str
    video_sync: str
    interpolation: str
    tscale: str


def _resolve_drm_launch_target(target_mode: str, connector: str = RGBPI_CONNECTOR_NAME) -> Optional[DrmLaunchTarget]:
    base = Path("/sys/class/drm")
    if not base.exists():
        return None
    for status_file in sorted(base.glob(f"card*-{connector}/status")):
        try:
            status = status_file.read_text(encoding="utf-8").strip().lower()
        except Exception:
            continue
        if status != "connected":
            continue
        card = status_file.parent.name.split("-")[0]
        return DrmLaunchTarget(card=card, connector=connector, mode_name=target_mode)
    return None


def playback_profile_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> PlaybackProfile:
    motion_mode = resolve_motion_mode(prefs)
    video_sync = _resolved_video_sync()
    if source.authored_dvd:
        return PlaybackProfile(
            motion_mode="authentic",
            video_sync=video_sync,
            interpolation="no",
            tscale="box",
        )
    if motion_mode == "cable_smooth":
        return PlaybackProfile(
            motion_mode="cable_smooth",
            video_sync=video_sync,
            interpolation="no",
            tscale="box",
        )
    if motion_mode == "smooth_tv":
        return PlaybackProfile(
            motion_mode="smooth_tv",
            video_sync=video_sync,
            interpolation="no",
            tscale="box",
        )
    return PlaybackProfile(
        motion_mode="authentic",
        video_sync=video_sync,
        interpolation="no",
        tscale="box",
    )


def force_43_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> bool:
    if source.authored_dvd:
        return False
    if source.kind not in {PlaybackKind.VIDEO_FILE, PlaybackKind.PLEX_VIDEO, PlaybackKind.YOUTUBE_VIDEO}:
        return False
    return bool(getattr(prefs, "force_43", False))


def audio_normalization_profile_for_source(
    source: PlaybackSource,
    prefs: Optional[PlaybackPrefs] = None,
) -> tuple[str, Optional[str]]:
    if source.authored_dvd or source.kind not in {PlaybackKind.VIDEO_FILE, PlaybackKind.PLEX_VIDEO, PlaybackKind.YOUTUBE_VIDEO}:
        return "off", None
    configured = _normalize_volume_normalization(getattr(prefs, "volume_normalization", "light"))
    if configured == "off":
        return "off", None
    if configured == "high":
        if _ffmpeg_supports_filter("loudnorm"):
            return "high", HIGH_NORMALIZATION_FILTER
        log_event("audio_normalization_fallback", requested="high", effective="light", reason="loudnorm_unavailable")
    return "light", LIGHT_NORMALIZATION_FILTER


def deinterlace_profile_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> tuple[str, Optional[str]]:
    del source  # Applies globally to all playback sources.
    # Default is "weave" (no deinterlace) — matches upstream and is right for
    # the CRT pipeline (the CRT scans interlaced fields natively, so no
    # filter chain is needed and adding bwdif demonstrably regressed motion
    # smoothness on the Pi 4 / vc4 KMS DRM stack). Users on a progressive
    # display can opt in to "bob" via playback_prefs.json or the prefs UI.
    configured = _normalize_deinterlace_mode(getattr(prefs, "deinterlace_mode", "weave"))
    if configured == "bob":
        return "bob", BOB_DEINTERLACE_FILTER
    return "weave", None


def smooth_fps_filter_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> Optional[str]:
    if source.authored_dvd:
        return None
    if os.environ.get("DVDPLAYER_FORCE_FPS_FILTER", "0") == "1":
        return SMOOTH_FPS_FILTER
    return None


def motion_vf_filter_for_source(source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> Optional[str]:
    if source.authored_dvd:
        return None
    motion_mode = resolve_motion_mode(prefs)
    if motion_mode == "cable_smooth":
        return CABLE_SMOOTH_BLEND_FILTER
    return None


class PlaybackSession:
    def __init__(
        self,
        child: subprocess.Popen,
        ipc_path: Path,
        target_mode: Optional[str],
        drm_target: Optional[DrmLaunchTarget],
        backend: str = "mpv",
        tty_handle: Any = None,
        backend_profile: str = "legacy",
        effective_mode: Optional[str] = None,
        degraded: bool = False,
    ):
        self.child = child
        self.ipc_path = ipc_path
        self.target_mode = target_mode
        self.drm_target = drm_target
        self.backend = backend
        self.tty_handle = tty_handle
        self.backend_profile = backend_profile
        self.effective_mode = effective_mode
        self.degraded = degraded
        self._overlay_paths: list[Path] = []
        self._started_at = time.time()
        self._request_id = 0
        self._hud: Optional["PlaybackHUD"] = None
        # Persistent IPC socket. mpv ties osd-overlay lifetime to the libmpv
        # client that issued it: as soon as the client (= socket connection)
        # disconnects, mpv tears down its overlays. The original implementation
        # opened a new socket per command (`with socket(...) as s:`), which
        # made every `osd-overlay` we sent vanish on the next frame. Holding
        # one socket open for the whole session lets the HUD persist.
        # Ref: <https://github.com/mpv-player/mpv/blob/v0.32.0/DOCS/man/input.rst>
        self._ipc_sock: Optional[socket.socket] = None
        self._ipc_buf: bytes = b""

    @classmethod
    def start(cls, app_dir: Path, source: PlaybackSource, prefs: Optional[PlaybackPrefs] = None) -> "PlaybackSession":
        fallback_bins = [
            app_dir / "bin" / "mpv",
        ]
        mpv = _which("mpv")
        if not mpv:
            for candidate in fallback_bins:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    mpv = str(candidate)
                    break
        ffplay = _which("ffplay") or ("/usr/bin/ffplay" if Path("/usr/bin/ffplay").exists() else None)
        if not mpv:
            if ffplay and not source.authored_dvd:
                return cls._start_ffplay(app_dir, source, prefs, ffplay)
            bundled = app_dir / "bin" / "mpv"
            raise FileNotFoundError(f"Bundled mpv not found: '{bundled}'")

        target_mode = _target_mode_for_source(source, prefs)
        drm_target = _resolve_drm_launch_target(target_mode) if target_mode else None

        prefer_drm = os.environ.get("DVDPLAYER_PREFER_DRM", "1") != "0"
        if not drm_target:
            prefer_drm = False

        ipc_path = Path(f"/tmp/rgbpi-dvdplayer-ipc-{int(time.time() * 1000)}.sock")
        ipc_path.unlink(missing_ok=True)

        if prefer_drm:
            child, tty_handle = cls._spawn_mpv(app_dir, source, prefs, mpv, ipc_path, target_mode, drm_target, prefer_drm=True)
            try:
                cls._wait_for_ipc(child, ipc_path, timeout_s=8.0)
                cls._verify_playback_session(child, ipc_path, require_video=source.authored_dvd)
                cls._verify_session_stability(child, ipc_path, duration_s=1.0)
                log_event(
                    "mpv_launch_profile",
                    profile="drm",
                    connector=drm_target.connector if drm_target else None,
                    mode=target_mode,
                    args="drm",
                )
                effective_mode, degraded = cls._assess_output_mode(target_mode, drm_target)
                return cls(
                    child,
                    ipc_path,
                    target_mode,
                    drm_target,
                    backend="mpv",
                    tty_handle=tty_handle,
                    backend_profile="drm",
                    effective_mode=effective_mode,
                    degraded=degraded,
                )
            except Exception as exc:
                log_event("mpv_drm_launch_failed", error=str(exc), mode=target_mode)
                try:
                    child.kill()
                except Exception:
                    pass
                if tty_handle:
                    try:
                        tty_handle.close()
                    except Exception:
                        pass

        child, tty_handle = cls._spawn_mpv(app_dir, source, prefs, mpv, ipc_path, target_mode, drm_target, prefer_drm=False)
        try:
            cls._wait_for_ipc(child, ipc_path, timeout_s=8.0)
            cls._verify_playback_session(child, ipc_path, require_video=source.authored_dvd)
            log_event("mpv_launch_profile", profile="legacy", mode=target_mode)
            if tty_handle:
                try:
                    tty_handle.close()
                except Exception:
                    pass
            effective_mode, degraded = cls._assess_output_mode(target_mode, drm_target)
            return cls(
                child,
                ipc_path,
                target_mode,
                drm_target if prefer_drm else None,
                backend="mpv",
                backend_profile="legacy",
                effective_mode=effective_mode,
                degraded=True if target_mode else degraded,
            )
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
            if tty_handle:
                try:
                    tty_handle.close()
                except Exception:
                    pass
            raise

    @classmethod
    def _start_ffplay(
        cls,
        app_dir: Path,
        source: PlaybackSource,
        prefs: Optional[PlaybackPrefs],
        ffplay: str,
    ) -> "PlaybackSession":
        target_mode = _target_mode_for_source(source, prefs)
        profile = playback_profile_for_source(source, prefs)
        deinterlace_mode, _deinterlace_filter = deinterlace_profile_for_source(source, prefs)
        smooth_fps_filter = smooth_fps_filter_for_source(source, prefs)
        motion_vf_filter = motion_vf_filter_for_source(source, prefs)
        log_event(
            "playback_profile",
            source_kind=source.kind.value,
            authored_dvd=source.authored_dvd,
            target_mode=target_mode,
            motion_mode=profile.motion_mode,
            video_sync=profile.video_sync,
            interpolation=profile.interpolation,
            tscale=profile.tscale,
            deinterlace_mode=deinterlace_mode,
            backend="ffplay",
        )

        args = [
            ffplay,
            "-fs",
            "-autoexit",
            "-volume",
            "72",
            "-sync",
            "audio" if profile.video_sync == "audio" else "video",
        ]
        vf_filters: list[str] = []
        if deinterlace_mode == "bob":
            vf_filters.append(BOB_DEINTERLACE_FILTER)
        if smooth_fps_filter:
            vf_filters.append(smooth_fps_filter)
        if motion_vf_filter:
            vf_filters.append("tblend=all_mode=average")
        if vf_filters:
            args += ["-vf", ",".join(vf_filters)]
        args += [
            source.uri,
        ]

        log_path = Path(os.environ.get("DVDPLAYER_MPV_LOG", "/tmp/rgbpi-dvdplayer-mpv.log"))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("ab")
        except Exception:
            fallback = app_dir / "state" / "rgbpi-dvdplayer-mpv.log"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            log_file = fallback.open("ab")

        log_event(
            "ffplay_spawn",
            cmd=" ".join(args),
            mode=target_mode,
            motion_mode=profile.motion_mode,
            video_sync=profile.video_sync,
            deinterlace_mode=deinterlace_mode,
            smooth_fps_filter=smooth_fps_filter,
            motion_vf_filter=motion_vf_filter,
        )
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            cwd=str(app_dir),
            env=_child_env(app_dir),
        )
        time.sleep(0.3)
        if child.poll() is not None:
            raise RuntimeError("ffplay exited during startup")
        return cls(
            child,
            Path(f"/tmp/rgbpi-dvdplayer-ffplay-{int(time.time() * 1000)}.noop"),
            target_mode,
            None,
            backend="ffplay",
            backend_profile="ffplay",
            effective_mode=None,
            degraded=bool(target_mode),
        )

    @classmethod
    def _spawn_mpv(
        cls,
        app_dir: Path,
        source: PlaybackSource,
        prefs: Optional[PlaybackPrefs],
        mpv: str,
        ipc_path: Path,
        target_mode: Optional[str],
        drm_target: Optional[DrmLaunchTarget],
        prefer_drm: bool,
    ) -> subprocess.Popen:
        profile = playback_profile_for_source(source, prefs)
        force_43 = force_43_for_source(source, prefs)
        normalization_mode, audio_filter = audio_normalization_profile_for_source(source, prefs)
        deinterlace_mode, deinterlace_filter = deinterlace_profile_for_source(source, prefs)
        smooth_fps_filter = smooth_fps_filter_for_source(source, prefs)
        motion_vf_filter = motion_vf_filter_for_source(source, prefs)
        interpolation_flag = f"--interpolation={profile.interpolation}"
        tscale_flag = f"--tscale={profile.tscale}"
        video_sync_flag = f"--video-sync={profile.video_sync}"
        monitor_pixel_aspect = _monitor_pixel_aspect_for_mode(target_mode)

        hwdec_mode = os.environ.get("DVDPLAYER_MPV_HWDEC", "auto-safe").strip() or "auto-safe"

        # mpv `--sub-font-size`, `--osd-font-size`, `--sub-margin-y` and
        # `--sub-border-size` are expressed in "scaled pixels at a window
        # height of 720" and mpv rescales them automatically with the actual
        # output window height. Setting the values below to a fixed 720-base
        # therefore keeps the same screen-relative size on a 240p CRT, on a
        # 480i interlaced output and on a 1080p TV:
        #   sub font   = 11%  × 720 = 79
        #   osd font   = 5%   × 720 = 36 (1.5x the upstream default; kept
        #                                lower than subs so the 11-row START
        #                                menu overlay stays compact on 240p)
        #   sub margin = 6%   × 720 = 43
        #   sub border = 720  / 180 = 4
        args = [
            mpv,
            "--fs",
            "--no-osc",
            "--input-default-bindings=no",
            "--audio-channels=stereo",
            # Drive ALSA directly: skip the system pcm.!default → plug →
            # sysdefault:0 chain so we don't get ALSA's poor
            # linear-interpolation resampler in the path. mpv's own
            # resampler is much higher quality. The bcm2835 hardware is
            # also pinned to its native config: s16 + 48 kHz (the AC3 /
            # AAC / FLAC sources we play are all 48 kHz natively, so no
            # resampling is needed at all 99% of the time).
            "--ao=alsa",
            f"--alsa-device={_resolve_alsa_device()}",
            "--audio-samplerate=48000",
            "--audio-format=s16",
            "--osd-level=0",
            "--osd-align-x=center",
            "--osd-align-y=center",
            "--osd-font-size=36",  # 5% of 720 baseline; mpv scales to ~12px on 240p (1.5x the upstream default 24, comfortably readable, START menu stays under 75% of the screen)
            "--osd-margin-y=0",
            "--sub-auto=no",
            "--sub-font-size=79",
            "--sub-margin-y=43",
            "--sub-border-size=4",
            "--sub-color=#FFF6EC",
            "--sub-border-color=#2C1204",
            f"--hwdec={hwdec_mode}",
            "--deband=no",
            interpolation_flag,
            video_sync_flag,
            tscale_flag,
            "--sigmoid-upscaling=no",
            "--scale=bilinear",
            "--cscale=bilinear",
            "--dscale=bilinear",
            "--input-ipc-server=" + str(ipc_path),
            "--idle=no",
            "--force-window=no",
            "--keep-open=no",
            f"--alang={_mpv_language_preference(prefs.preferred_audio_language if prefs else None)}",
            "--slang=auto",
            "--audio-display=no",
            "--cache=yes",
            "--demuxer-max-bytes=256MiB",
            "--demuxer-readahead-secs=20",
            "--vd-lavc-threads=0",  # 0 = auto, use all CPU cores (helps Pi 4 software MPEG-2 decode)
        ]
        # Keep mpv's built-in deinterlace disabled; we apply bwdif explicitly
        # in bob mode to avoid double-processing and heavy frame amplification.
        args.append("--deinterlace=no")
        if deinterlace_mode == "bob":
            args.append(f"--vf-add={deinterlace_filter}")
        if smooth_fps_filter:
            args.append(f"--vf-add={smooth_fps_filter}")
        if motion_vf_filter:
            args.append(f"--vf-add={motion_vf_filter}")
        if force_43:
            args.append("--video-aspect-override=4:3")
        if audio_filter:
            args.append(f"--af={audio_filter}")
        if monitor_pixel_aspect is not None:
            args.append(f"--monitorpixelaspect={monitor_pixel_aspect:.7f}")

        # Frame-rate smoothing (PAL or NTSC speedup — see _speedup_for_source).
        # Gated on prefer_drm + drm_target so we only apply when the
        # output is *confirmed* to be a CRT 50/60 Hz mode. On the LCD
        # pipeline the native output rate is unpredictable and the
        # speedup would likely worsen cadence rather than improve it.
        if prefer_drm and drm_target:
            speedup = _speedup_for_source(source.hint_fps, target_mode, prefs=prefs)
        else:
            speedup = None
        if speedup is not None:
            args.append(f"--speed={speedup:.6f}")
            # Let audio pitch follow the speed change (default mpv behaviour
            # is to resample to keep pitch constant, which would defeat the
            # zero-judder trade we're making).
            args.append("--audio-pitch-correction=no")

        if prefer_drm and drm_target and target_mode:
            args += [
                "--vo=drm",
                f"--drm-connector={_mpv_drm_connector_value(drm_target)}",
                f"--drm-mode={_mpv_drm_mode_value(target_mode)}",
            ]
            log_event(
                "mpv_drm_target",
                card=drm_target.card,
                connector=drm_target.connector,
                mode=drm_target.mode_name,
            )

        if source.kind in {PlaybackKind.DVD_FOLDER, PlaybackKind.DVD_ISO, PlaybackKind.OPTICAL_DRIVE}:
            args += [f"--dvd-device={source.uri}", "dvdnav://"]
        else:
            args += [source.uri]

        log_path = Path(os.environ.get("DVDPLAYER_MPV_LOG", "/tmp/rgbpi-dvdplayer-mpv.log"))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("ab")
        except Exception:
            fallback = app_dir / "state" / "rgbpi-dvdplayer-mpv.log"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            log_file = fallback.open("ab")

        log_event(
            "playback_profile",
            source_kind=source.kind.value,
            authored_dvd=source.authored_dvd,
            target_mode=target_mode,
            motion_mode=profile.motion_mode,
            video_sync=profile.video_sync,
            interpolation=profile.interpolation,
            tscale=profile.tscale,
            force_43=force_43,
            volume_normalization=normalization_mode,
            audio_filter=audio_filter,
            deinterlace_mode=deinterlace_mode,
            deinterlace_filter=deinterlace_filter,
            smooth_fps_filter=smooth_fps_filter,
            motion_vf_filter=motion_vf_filter,
            pal_speedup=speedup,
            source_fps=source.hint_fps,
        )
        log_event(
            "mpv_spawn",
            cmd=" ".join(args[:28]) + " ...",
            mode=target_mode,
            prefer_drm=prefer_drm,
            video_sync=profile.video_sync,
            interpolation=profile.interpolation == "yes",
            motion_mode=profile.motion_mode,
            monitor_pixel_aspect=monitor_pixel_aspect,
            force_43=force_43,
            volume_normalization=normalization_mode,
            deinterlace_mode=deinterlace_mode,
            smooth_fps_filter=smooth_fps_filter,
            motion_vf_filter=motion_vf_filter,
            active_tty=os.environ.get("DVDPLAYER_ACTIVE_TTY"),
        )
        tty_stdin: Any = subprocess.DEVNULL
        tty_handle = None
        active_tty = os.environ.get("DVDPLAYER_ACTIVE_TTY")
        if prefer_drm and active_tty:
            try:
                tty_handle = open(active_tty, "rb", buffering=0)
                tty_stdin = tty_handle
            except Exception as exc:
                log_event("mpv_tty_open_failed", tty=active_tty, error=str(exc))

        child = subprocess.Popen(
            args,
            stdin=tty_stdin,
            stdout=log_file,
            stderr=log_file,
            cwd=str(app_dir),
            env=_child_env(app_dir),
        )
        return child, tty_handle

    @staticmethod
    def _wait_for_ipc(child: subprocess.Popen, ipc_path: Path, timeout_s: float) -> None:
        started = time.time()
        while time.time() - started < timeout_s:
            if child.poll() is not None:
                raise RuntimeError("mpv exited before IPC became ready")
            if ipc_path.exists():
                return
            time.sleep(0.05)
        raise RuntimeError("timeout waiting for mpv IPC")

    @staticmethod
    def _verify_playback_session(child: subprocess.Popen, ipc_path: Path, require_video: bool) -> None:
        # DRM mpv can create IPC and then fail KMS immediately; probe core properties before we accept startup.
        deadline = time.time() + 5.0
        last_error: Optional[str] = None
        while time.time() < deadline:
            if child.poll() is not None:
                raise RuntimeError("mpv exited during startup validation")
            try:
                pause_resp = PlaybackSession._ipc_get_property(ipc_path, "pause")
                if pause_resp is not None:
                    if not require_video:
                        return
                    tracks = PlaybackSession._ipc_get_property(ipc_path, "track-list") or []
                    if isinstance(tracks, list):
                        has_video = any(isinstance(t, dict) and t.get("type") == "video" for t in tracks)
                        if has_video:
                            return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.1)
        if not require_video and child.poll() is None:
            log_event("mpv_validation_relaxed", error=last_error, require_video=require_video)
            return
        raise RuntimeError(f"mpv startup validation failed{': ' + last_error if last_error else ''}")

    @staticmethod
    def _verify_session_stability(child: subprocess.Popen, ipc_path: Path, duration_s: float) -> None:
        deadline = time.time() + max(0.2, duration_s)
        while time.time() < deadline:
            if child.poll() is not None:
                raise RuntimeError("mpv exited during stability check")
            try:
                PlaybackSession._ipc_get_property(ipc_path, "pause")
            except Exception as exc:
                raise RuntimeError(f"mpv unstable after startup: {exc}") from exc
            time.sleep(0.1)

    @staticmethod
    def _ipc_get_property(ipc_path: Path, name: str) -> Any:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.8)
            sock.connect(str(ipc_path))
            request_id = int(time.time() * 1_000_000) & 0x7FFFFFFF
            payload = {"command": ["get_property", name], "request_id": request_id}
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            deadline = time.time() + 2.0
            pending = ""
            while time.time() < deadline:
                try:
                    raw = sock.recv(65536)
                except socket.timeout:
                    continue
                if not raw:
                    break
                pending += raw.decode("utf-8", errors="ignore")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(response, dict) and response.get("request_id") == request_id:
                        if response.get("error") not in (None, "success"):
                            raise RuntimeError(str(response))
                        return response.get("data")
        raise RuntimeError(f"mpv IPC timeout waiting for property '{name}'")

    def is_running(self) -> bool:
        return self.child.poll() is None

    def quit(self) -> None:
        try:
            self.clear_overlays()
        except Exception:
            pass
        if self.backend == "mpv":
            try:
                self.command(["quit"])
            except Exception:
                pass
        else:
            try:
                self.child.terminate()
            except Exception:
                pass
        try:
            self.child.wait(timeout=2)
        except Exception:
            self.child.kill()
        self._cleanup()

    def _cleanup(self) -> None:
        # Close the persistent IPC socket *before* unlinking the path —
        # the socket will fault otherwise and we'd lose the chance to
        # send a clean disconnect.
        self._close_ipc_socket()
        try:
            self.ipc_path.unlink(missing_ok=True)
        except Exception:
            pass
        if self.tty_handle:
            try:
                self.tty_handle.close()
            except Exception:
                pass
            self.tty_handle = None
        for path in self._overlay_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        self._overlay_paths.clear()

    def _open_ipc_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        sock.connect(str(self.ipc_path))
        return sock

    def _close_ipc_socket(self) -> None:
        if self._ipc_sock is not None:
            try:
                self._ipc_sock.close()
            except Exception:
                pass
        self._ipc_sock = None
        self._ipc_buf = b""

    def _send(self, payload: dict) -> dict:
        """Send a JSON-IPC request to mpv and wait for the matching response.

        Uses the persistent socket stored on the session. mpv attaches
        ``osd-overlay`` lifetimes to the client (= socket connection) — every
        disconnect tears the overlays down — so we must hold one socket open
        for the whole session, not one per request.

        Asynchronous events / unrequested property-change messages that
        arrive between requests are kept in :attr:`_ipc_buf` and discarded
        on the next read; we only return when we find a response whose
        ``request_id`` matches ours. On socket-level errors we close the
        socket and retry once — a fresh socket reconnects to the same
        ``--input-ipc-server`` and gets a new libmpv client (so any
        ``osd-overlay`` still on screen would be dropped, but that's the
        worst case, not the steady state).
        """
        if self.backend != "mpv":
            raise RuntimeError(f"{self.backend} backend does not support IPC")
        self._request_id += 1
        request_id = int(payload.get("request_id") or self._request_id)
        message = dict(payload)
        message["request_id"] = request_id
        wire = (json.dumps(message) + "\n").encode("utf-8")

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            if self._ipc_sock is None:
                try:
                    self._ipc_sock = self._open_ipc_socket()
                    self._ipc_buf = b""
                except OSError as exc:
                    last_exc = exc
                    continue
            sock = self._ipc_sock
            try:
                sock.sendall(wire)
            except OSError as exc:
                last_exc = exc
                self._close_ipc_socket()
                continue

            deadline = time.time() + 2.0
            while time.time() < deadline:
                # First try to satisfy the request from already-buffered bytes.
                while b"\n" in self._ipc_buf:
                    line, _, self._ipc_buf = self._ipc_buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        response = json.loads(line.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(response, dict) and response.get("request_id") == request_id:
                        return response
                # Need more data.
                try:
                    raw = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as exc:
                    last_exc = exc
                    self._close_ipc_socket()
                    break
                if not raw:
                    self._close_ipc_socket()
                    break
                self._ipc_buf += raw
            else:
                # while-else: we ran out of time without breaking out due to a
                # socket error or finding a matching response → timeout.
                raise RuntimeError(f"mpv IPC timeout waiting for response {request_id}")
            # Socket died mid-request — fall through to retry once with a
            # fresh connection. `last_exc` carries the underlying OSError.
        raise RuntimeError(f"mpv IPC unreachable: {last_exc}")

    def command(self, command: list[Any]) -> dict:
        response = self._send({"command": command})
        if response.get("error") not in (None, "success"):
            raise RuntimeError(f"mpv command failed: {response}")
        return response

    def get_property(self, name: str) -> Any:
        return self.command(["get_property", name]).get("data")

    def set_property(self, name: str, value: Any) -> None:
        self.command(["set_property", name, value])

    def set_pause(self, paused: bool) -> None:
        if self.backend != "mpv":
            return
        self.command(["set_property", "pause", paused])

    def pause_state(self) -> bool:
        if self.backend != "mpv":
            return False
        return bool(self.get_property("pause"))

    def current_time(self) -> float:
        if self.backend != "mpv":
            return max(0.0, time.time() - self._started_at)
        value = self.get_property("time-pos")
        return float(value) if value is not None else 0.0

    def duration(self) -> Optional[float]:
        if self.backend != "mpv":
            return None
        value = self.get_property("duration")
        return float(value) if value is not None else None

    def seek_absolute(self, seconds: float) -> None:
        if self.backend != "mpv":
            return
        self.command(["seek", float(seconds), "absolute+exact"])

    def seek_relative(self, seconds: int) -> None:
        if self.backend != "mpv":
            return
        self.command(["seek", int(seconds), "relative+exact"])

    def speed(self) -> float:
        if self.backend != "mpv":
            return 1.0
        value = self.get_property("speed")
        return float(value) if value is not None else 1.0

    def set_speed(self, value: float) -> None:
        if self.backend != "mpv":
            return
        self.command(["set_property", "speed", float(value)])

    def volume(self) -> float:
        if self.backend != "mpv":
            return 72.0
        value = self.get_property("volume")
        return float(value) if value is not None else 0.0

    def set_volume(self, value: float) -> None:
        if self.backend != "mpv":
            return
        self.command(["set_property", "volume", float(value)])

    def set_audio_track(self, track_id: int) -> None:
        if self.backend != "mpv":
            return
        self.command(["set_property", "aid", int(track_id)])

    def set_subtitle_track(self, track_id: int) -> None:
        if self.backend != "mpv":
            return
        if track_id < 0:
            self.command(["set_property", "sid", "no"])
        else:
            self.command(["set_property", "sid", int(track_id)])

    def current_audio_track(self) -> Optional[int]:
        if self.backend != "mpv":
            return None
        value = self.get_property("aid")
        return int(value) if value is not None else None

    def current_subtitle_track(self) -> Optional[int]:
        if self.backend != "mpv":
            return None
        value = self.get_property("sid")
        return int(value) if isinstance(value, int) else None

    def _tracks_by_type(self, track_type: str) -> list[dict[str, Any]]:
        if self.backend != "mpv":
            return []
        tracks = self.get_property("track-list")
        if not isinstance(tracks, list):
            return []
        out: list[dict[str, Any]] = []
        for track in tracks:
            if not isinstance(track, dict) or track.get("type") != track_type:
                continue
            track_id = track.get("id")
            if not isinstance(track_id, (int, float)):
                continue
            title = str(track.get("title") or "").strip()
            lang = str(track.get("lang") or "").strip().upper()
            if title and lang and lang not in title.upper():
                label = f"{lang} {title}"
            elif title:
                label = title
            elif lang:
                label = lang
            else:
                label = f"TRACK {int(track_id)}"
            out.append({"id": int(track_id), "label": label, "lang": lang})
        return out

    def audio_tracks(self) -> list[dict[str, Any]]:
        return self._tracks_by_type("audio")

    def subtitle_tracks(self) -> list[dict[str, Any]]:
        return self._tracks_by_type("sub")

    def show_text(self, text: str, duration_ms: int = 2000) -> None:
        if self.backend != "mpv":
            return
        self.command(["show-text", text, int(duration_ms)])

    def clear_text(self) -> None:
        if self.backend != "mpv":
            return
        self.command(["show-text", "", 1])

    def send_keypress(self, key: str) -> None:
        if self.backend != "mpv":
            return
        self.command(["keypress", key])

    def go_to_dvd_menu(self) -> None:
        self.send_keypress("MENU")

    def current_chapter(self) -> Optional[int]:
        if self.backend != "mpv":
            return None
        value = self.get_property("chapter")
        return int(value) if isinstance(value, (int, float)) else None

    def set_chapter(self, chapter: int) -> None:
        if self.backend != "mpv":
            return
        self.set_property("chapter", int(chapter))

    def step_chapter(self, delta: int) -> Optional[int]:
        cur = self.current_chapter()
        if cur is None:
            return None
        nxt = max(0, cur + int(delta))
        self.set_chapter(nxt)
        return nxt

    def screenshot_to_file(self, path: Path) -> None:
        if self.backend != "mpv":
            raise RuntimeError("ffplay backend has no screenshot support")
        self.command(["screenshot-to-file", str(path), "window"])

    def display_mode_badge_text(self) -> str:
        if self.effective_mode:
            return _friendly_mode_label(self.effective_mode)
        if self.target_mode:
            return _friendly_mode_label(self.target_mode)
        raw = _read_drm_mode(self.drm_target.connector if self.drm_target else None)
        if raw:
            return _friendly_mode_label(raw)
        return "CRT"

    @staticmethod
    def _assess_output_mode(target_mode: Optional[str], drm_target: Optional[DrmLaunchTarget]) -> tuple[Optional[str], bool]:
        effective_mode = _read_drm_mode(drm_target.connector if drm_target else None)
        degraded = False
        if target_mode and effective_mode:
            degraded = _friendly_mode_label(target_mode) != _friendly_mode_label(effective_mode)
        elif target_mode and not effective_mode:
            degraded = True
        log_event(
            "playback_output_mode",
            requested_mode=target_mode,
            effective_mode=effective_mode,
            connector=drm_target.connector if drm_target else None,
            degraded=degraded,
        )
        return effective_mode, degraded

    @property
    def hud(self) -> Optional["PlaybackHUD"]:
        """Lazy-built :class:`PlaybackHUD` bound to this session.

        Returns ``None`` for non-mpv backends (ffplay) which have no IPC
        channel to drive an overlay. Constructing on first access keeps the
        ``hud`` module out of the import graph until it is actually needed.
        """
        if self.backend != "mpv":
            return None
        if self._hud is None:
            from dvdplayer_python.playback.hud import PlaybackHUD

            self._hud = PlaybackHUD(
                send_command=self.command,
                get_state=lambda: (
                    self.pause_state(),
                    self.current_time(),
                    self.duration(),
                ),
                get_canvas_aspect=self._osd_display_aspect,
            )
        return self._hud

    def _osd_display_aspect(self) -> Optional[float]:
        """Display aspect of mpv's current OSD, or ``None`` if unknown.

        Computed as ``osd-width * osd-par / osd-height`` so it stays
        correct on non-square-pixel modes (e.g. 720×480 stretched to a
        4:3 CRT). The HUD uses this to size its 720-baseline canvas so
        the layout renders proportionally on 4:3 CRT outputs as well as
        16:9 LCDs.
        """
        if self.backend != "mpv":
            return None
        try:
            w = self.get_property("osd-width")
            h = self.get_property("osd-height")
        except Exception:
            return None
        try:
            fw = float(w) if w else 0.0
            fh = float(h) if h else 0.0
        except (TypeError, ValueError):
            return None
        if fw <= 0 or fh <= 0:
            return None
        try:
            par_raw = self.get_property("osd-par")
        except Exception:
            par_raw = None
        try:
            par = float(par_raw) if par_raw else 1.0
        except (TypeError, ValueError):
            par = 1.0
        if par <= 0:
            par = 1.0
        return (fw * par) / fh

    def clear_overlays(self) -> None:
        """Hide every transient overlay drawn on top of the video.

        The HUD is *hidden* but not destroyed — the next call to
        ``self.hud.flash()`` (e.g. user pauses or seeks) brings it back. Full
        teardown happens on :meth:`quit`, when the mpv subprocess goes away.
        """
        if self.backend != "mpv":
            return
        try:
            self.clear_text()
        except Exception:
            pass
        if self._hud is not None:
            try:
                self._hud.hide()
            except Exception:
                pass
        for oid in (OVERLAY_MAIN_ID, OVERLAY_BADGE_ID):
            try:
                self.command(["overlay-remove", oid])
            except Exception:
                pass

    def show_start_menu_overlay(self, selected: int, items: list[str]) -> None:
        self._show_simple_menu_overlay("PLAYBACK MENU", selected, items)

    def show_subtitle_menu_overlay(self, selected: int, items: list[str]) -> None:
        self._show_simple_menu_overlay("SUBTITLE MENU", selected, items)

    def show_audio_menu_overlay(self, selected: int, items: list[str]) -> None:
        self._show_simple_menu_overlay("AUDIO MENU", selected, items)

    def _show_simple_menu_overlay(self, title: str, selected: int, items: list[str]) -> None:
        if self.backend != "mpv":
            return
        rows = [title, ""]
        visible_count = 6
        safe_selected = min(max(0, selected), max(0, len(items) - 1))
        start = 0
        if len(items) > visible_count:
            start = min(max(0, safe_selected - visible_count // 2), len(items) - visible_count)
        visible = items[start : start + visible_count]
        if start > 0:
            rows.append("  ...")
        for offset, text in enumerate(visible):
            index = start + offset
            prefix = ">" if index == safe_selected else " "
            rows.append(f"{prefix} {text}")
        if start + visible_count < len(items):
            rows.append("  ...")
        rows.extend(["", "UP/DOWN  A OK", "START/SELECT/B CLOSE"])
        self.show_text("\n".join(rows), duration_ms=600000)

    def show_seek_overlay(self, paused: bool, step_seconds: int = 30) -> None:
        if self.backend != "mpv":
            return
        status = "PAUSED" if paused else "PLAYING"
        text = f"SEEK / PLAYBACK\n\n{status}\n\nLEFT/RIGHT {step_seconds}s\nA TOGGLE\nSTART/SELECT/B CLOSE"
        self.show_text(text, duration_ms=600000)


def _child_env(app_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    lib_paths = [
        str(app_dir / "lib"),
    ]
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join([p for p in (lib_paths + ([existing] if existing else [])) if p])
    return env


def _mpv_language_preference(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "auto"
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789,_-")
    cleaned = "".join(ch for ch in text if ch in allowed)
    return cleaned or "auto"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def _centered_osd(text: str, body_size: int = 24, title_size: int = 28) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    title = _escape_ass_text(lines[0])
    body = [_escape_ass_text(line) for line in lines[1:]]
    body_text = r"\N".join(body)
    header = r"{{\an5\pos(160,120)\bord2\shad0\1c&HFFF6EC&\3c&H241000&\fs{}}}".format(title_size)
    if not body:
        return header + title
    body_header = r"{{\fs{}}}".format(body_size)
    return header + title + r"\N" + body_header + body_text
