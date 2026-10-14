"""Which OS the player runs on — the ONE place platform differences are decided.

Two supported platforms share this codebase:

  * ``os4``      — RGB-Pi OS4 (Debian bullseye). CRT on the GPIO DPI/VGA666
                   DAC (``VGA-1``), real 480i/576i modes, pygame owns a 320x240
                   window that mpv's vo=drm can overlay without taking it down.
  * ``replayos`` — RePlayOS (Debian trixie) + RGB-Pi 2 HDMI DAC (``HDMI-A-1``).
                   15 kHz super-resolution modes, a single exclusive vc4-hdmi
                   PCM, and SDL/KMSDRM holds DRM master while its window exists.

Detection is automatic and asymmetric on purpose: RePlayOS behaviour needs a
positive marker (``/opt/replay/replay``, RePlay's own frontend binary), and
anything else — including a detection failure — is OS4. OS4 can therefore
never pick up RePlayOS behaviour by accident. ``DVDPLAYER_PLATFORM=os4|replayos``
forces the choice (debugging only; no launcher sets it).

Only behaviour that is NOT already opt-in via its own ``DVDPLAYER_*`` env var
lives here. The RePlayOS launcher (deploy/replayos/replay_launch.sh) still
exports the display/DRM/audio env it always did; those vars are inert unless
set. tests/platform_golden/check.py proves both platforms' behaviour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

OS4 = "os4"
REPLAYOS = "replayos"

# Filesystem root the markers are looked up under (the golden harness points
# it at a fake root).
ROOT = Path("/")
REPLAYOS_MARKER = "opt/replay/replay"


@dataclass(frozen=True)
class Platform:
    name: str
    detected_by: str
    # UI window size. None = draw straight onto a WINDOW_W x WINDOW_H window
    # (OS4); otherwise draw on a WINDOW_W x WINDOW_H canvas scaled to this.
    ui_display_size: Optional[tuple[int, int]]
    # Tear the SDL window down (dropping DRM master) while mpv plays.
    release_display_for_playback: bool
    # Close pygame's audio mixer at startup (it would hold the only PCM).
    close_audio_mixer: bool
    # SETTINGS > AUDIO OUTPUT choices, in menu order.
    audio_outputs: tuple[str, ...]
    # Extra --sws-scaler for vo=drm, or None to keep mpv's default.
    mpv_sws_scaler: Optional[str]


def _os4(detected_by: str) -> Platform:
    return Platform(
        name=OS4,
        detected_by=detected_by,
        ui_display_size=None,
        release_display_for_playback=False,
        close_audio_mixer=False,
        audio_outputs=("jack", "optical_5_1"),
        mpv_sws_scaler=None,
    )


def _replayos(detected_by: str) -> Platform:
    return Platform(
        name=REPLAYOS,
        detected_by=detected_by,
        ui_display_size=(
            int(os.environ.get("DVDPLAYER_DISPLAY_W", "640")),
            int(os.environ.get("DVDPLAYER_DISPLAY_H", "480")),
        ),
        release_display_for_playback=True,
        close_audio_mixer=True,
        audio_outputs=("jack", "optical_5_1", "hdmi"),
        # vo=drm scales in software (swscale) and the default scaler saturates
        # a Pi 4 core on 2560-wide CRT modes (~5 dropped frames/s on a PAL
        # DVD); fast-bilinear measures 0 drops at these super-res widths.
        mpv_sws_scaler="fast-bilinear",
    )


def detect() -> Platform:
    forced = os.environ.get("DVDPLAYER_PLATFORM", "").strip().lower()
    if forced == REPLAYOS:
        return _replayos("env DVDPLAYER_PLATFORM")
    if forced == OS4:
        return _os4("env DVDPLAYER_PLATFORM")
    if (ROOT / REPLAYOS_MARKER).exists():
        return _replayos(f"/{REPLAYOS_MARKER}")
    return _os4("default (no RePlayOS marker)")


_current: Optional[Platform] = None


def current() -> Platform:
    global _current
    if _current is None:
        _current = detect()
    return _current
