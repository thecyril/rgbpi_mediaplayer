from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional

from .models import BookmarkState, LastPlayedState, PlaybackKind, PlaybackPrefs, PlaybackSource


class PlaybackStateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.bookmarks_path = self.state_dir / "playback_bookmarks.json"
        self.prefs_path = self.state_dir / "playback_prefs.json"
        self.last_played_path = self.state_dir / "playback_last_played.json"
        self.bookmarks: Dict[str, BookmarkState] = {}
        self.prefs = PlaybackPrefs()
        self.last_played: Optional[LastPlayedState] = None
        self.load()

    def load(self) -> None:
        if self.bookmarks_path.is_file():
            raw = json.loads(self.bookmarks_path.read_text(encoding="utf-8"))
            self.bookmarks = {
                key: BookmarkState(**value) for key, value in raw.items() if isinstance(value, dict)
            }
        if self.prefs_path.is_file():
            raw = json.loads(self.prefs_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.prefs = PlaybackPrefs(**{**asdict(PlaybackPrefs()), **raw})
                self.prefs.motion_mode = _normalize_motion_mode(self.prefs.motion_mode)
                self.prefs.default_mode = _normalize_default_mode(self.prefs.default_mode)
                self.prefs.volume_normalization = _normalize_volume_normalization(self.prefs.volume_normalization)
                self.prefs.deinterlace_mode = _normalize_deinterlace_mode(self.prefs.deinterlace_mode)
                self.prefs.aspect_mode = _normalize_aspect_mode(self.prefs.aspect_mode, self.prefs.force_43)
                # Keep the legacy bool consistent so a downgrade still behaves.
                self.prefs.force_43 = self.prefs.aspect_mode == "stretch"
        if self.last_played_path.is_file():
            raw = json.loads(self.last_played_path.read_text(encoding="utf-8"))
            self.last_played = _decode_last_played(raw)

    def bookmark(self, key: str) -> Optional[BookmarkState]:
        return self.bookmarks.get(key)

    def save_bookmark(
        self,
        key: str,
        title: str,
        uri: str,
        position_seconds: float,
        duration_seconds: Optional[float],
        now_ms: int,
    ) -> None:
        self.bookmarks[key] = BookmarkState(
            title=title,
            uri=uri,
            position_seconds=position_seconds,
            duration_seconds=duration_seconds,
            updated_at_unix_ms=now_ms,
        )
        self.write_bookmarks()

    def clear_bookmark(self, key: str) -> None:
        self.bookmarks.pop(key, None)
        self.write_bookmarks()

    def write_bookmarks(self) -> None:
        data = {key: asdict(value) for key, value in self.bookmarks.items()}
        self.bookmarks_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def write_prefs(self) -> None:
        self.prefs.motion_mode = _normalize_motion_mode(self.prefs.motion_mode)
        self.prefs.default_mode = _normalize_default_mode(self.prefs.default_mode)
        self.prefs.volume_normalization = _normalize_volume_normalization(self.prefs.volume_normalization)
        self.prefs.deinterlace_mode = _normalize_deinterlace_mode(self.prefs.deinterlace_mode)
        self.prefs.aspect_mode = _normalize_aspect_mode(self.prefs.aspect_mode, self.prefs.force_43)
        self.prefs.force_43 = self.prefs.aspect_mode == "stretch"
        self.prefs_path.write_text(json.dumps(asdict(self.prefs), indent=2), encoding="utf-8")

    def save_last_played(
        self,
        source: PlaybackSource,
        position_seconds: float,
        duration_seconds: Optional[float],
        now_ms: int,
    ) -> None:
        self.last_played = LastPlayedState(
            source=source,
            position_seconds=float(position_seconds),
            duration_seconds=float(duration_seconds) if duration_seconds is not None else None,
            updated_at_unix_ms=int(now_ms),
        )
        self.write_last_played()

    def clear_last_played(self) -> None:
        self.last_played = None
        self.write_last_played()

    def write_last_played(self) -> None:
        if not self.last_played:
            try:
                self.last_played_path.unlink()
            except FileNotFoundError:
                pass
            return
        payload = {
            "source": _encode_source(self.last_played.source),
            "position_seconds": self.last_played.position_seconds,
            "duration_seconds": self.last_played.duration_seconds,
            "updated_at_unix_ms": self.last_played.updated_at_unix_ms,
        }
        self.last_played_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def _normalize_aspect_mode(value: object, legacy_force_43: bool = False) -> str:
    text = str(value or "").strip().lower()
    if text in {"zoom", "crop", "panscan"}:
        return "zoom"
    if text in {"stretch", "fill", "force", "force_43", "4:3", "43"}:
        return "stretch"
    if text in {"off", "none", "native", "0", ""}:
        # No explicit aspect_mode (e.g. prefs written before this setting
        # existed): fall back to the legacy force_43 boolean = old "stretch".
        return "stretch" if legacy_force_43 else "off"
    return "off"


def _encode_source(source: PlaybackSource) -> dict:
    return {
        "title": source.title,
        "kind": source.kind.value,
        "uri": source.uri,
        "subtitle": source.subtitle,
        "authored_dvd": bool(source.authored_dvd),
        "file_size": source.file_size,
        "container": source.container,
        "hint_width": source.hint_width,
        "hint_height": source.hint_height,
        "hint_fps": source.hint_fps,
    }


def _decode_source(raw: object) -> Optional[PlaybackSource]:
    if not isinstance(raw, dict):
        return None
    uri = str(raw.get("uri") or "").strip()
    if not uri:
        return None
    kind_raw = str(raw.get("kind") or "").strip().lower()
    try:
        kind = PlaybackKind(kind_raw)
    except ValueError:
        return None
    file_size_raw = raw.get("file_size")
    file_size: Optional[int] = None
    if isinstance(file_size_raw, (int, float)):
        file_size = int(file_size_raw)
    hint_width_raw = raw.get("hint_width")
    hint_height_raw = raw.get("hint_height")
    hint_fps_raw = raw.get("hint_fps")
    hint_width: Optional[int] = int(hint_width_raw) if isinstance(hint_width_raw, (int, float)) else None
    hint_height: Optional[int] = int(hint_height_raw) if isinstance(hint_height_raw, (int, float)) else None
    hint_fps: Optional[float] = float(hint_fps_raw) if isinstance(hint_fps_raw, (int, float)) else None
    return PlaybackSource(
        title=str(raw.get("title") or Path(uri).name or "Video"),
        kind=kind,
        uri=uri,
        subtitle=str(raw.get("subtitle") or ""),
        authored_dvd=bool(raw.get("authored_dvd", False)),
        file_size=file_size,
        container=str(raw.get("container") or "") or None,
        hint_width=hint_width,
        hint_height=hint_height,
        hint_fps=hint_fps,
    )


def _decode_last_played(raw: object) -> Optional[LastPlayedState]:
    if not isinstance(raw, dict):
        return None
    source = _decode_source(raw.get("source"))
    if not source:
        return None
    try:
        position_seconds = float(raw.get("position_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    if position_seconds <= 0.0:
        return None
    duration_raw = raw.get("duration_seconds")
    duration_seconds: Optional[float]
    if isinstance(duration_raw, (int, float)):
        duration_seconds = float(duration_raw)
    else:
        duration_seconds = None
    updated_raw = raw.get("updated_at_unix_ms")
    try:
        updated_at_unix_ms = int(updated_raw) if updated_raw is not None else 0
    except (TypeError, ValueError):
        updated_at_unix_ms = 0
    return LastPlayedState(
        source=source,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        updated_at_unix_ms=updated_at_unix_ms,
    )


def cleanup_stale_runtime_files(control_socket: str, control_state_path: str) -> None:
    try:
        os.unlink(control_socket)
    except FileNotFoundError:
        pass
    try:
        if os.path.exists(control_state_path) and os.path.getsize(control_state_path) > 0:
            os.unlink(control_state_path)
    except FileNotFoundError:
        pass
