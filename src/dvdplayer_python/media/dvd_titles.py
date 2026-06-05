"""DVD title enumeration.

mpv 0.32 dropped interactive DVD-menu navigation (`dvdnav://` is now an alias
for `dvd://`; the menu video plays but the buttons are dead). Instead of a dead
on-screen menu, the player lets the user pick a title directly. We enumerate the
titles + durations with `lsdvd`, which reads the IFO structure of an ISO, a
folder containing VIDEO_TS, or a block device — no playback required.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from typing import List

from dvdplayer_python.core.debuglog import log_event


@dataclass
class DvdTitle:
    index: int
    length_seconds: float


def probe_dvd_titles(device: str, timeout: float = 60.0) -> List[DvdTitle]:
    """Return the DVD's titles (index + duration) via lsdvd.

    `device` is an ISO path, a folder containing VIDEO_TS, or a block device.
    Returns [] on any failure — the caller falls back to plain `dvdnav://` so a
    probe miss never blocks playback.
    """
    try:
        proc = subprocess.run(
            ["lsdvd", "-Oy", device],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        log_event("dvd_probe_failed", device=device, error="lsdvd not installed (apt install lsdvd)")
        return []
    except subprocess.TimeoutExpired:
        log_event("dvd_probe_failed", device=device, error="lsdvd timed out")
        return []
    except Exception as exc:  # pragma: no cover - defensive
        log_event("dvd_probe_failed", device=device, error=str(exc))
        return []

    titles = _parse_lsdvd(proc.stdout or "")
    if not titles:
        log_event(
            "dvd_probe_empty",
            device=device,
            rc=proc.returncode,
            stderr=(proc.stderr or "").strip()[-200:],
        )
    return titles


def _parse_lsdvd(text: str) -> List[DvdTitle]:
    """Parse `lsdvd -Oy` output.

    The format is `lsdvd = { ... }` — a Python literal, possibly preceded by
    libdvdread warnings on stdout/stderr. We slice from the first `{` and
    `ast.literal_eval` it; if that fails we fall back to a regex over the
    `'ix' : N , 'length' : F` pairs.
    """
    titles: List[DvdTitle] = []
    marker = text.find("lsdvd")
    brace = text.find("{", marker) if marker != -1 else -1
    if brace != -1:
        try:
            data = ast.literal_eval(text[brace:])
            for track in data.get("track", []):
                ix = track.get("ix")
                length = track.get("length")
                if isinstance(ix, int) and isinstance(length, (int, float)):
                    titles.append(DvdTitle(index=ix, length_seconds=float(length)))
        except Exception:
            titles = []

    if not titles:
        for m in re.finditer(r"'ix'\s*:\s*(\d+)\s*,\s*'length'\s*:\s*([\d.]+)", text):
            titles.append(DvdTitle(index=int(m.group(1)), length_seconds=float(m.group(2))))

    titles.sort(key=lambda t: t.index)
    return titles
