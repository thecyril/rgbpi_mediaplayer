from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import List, Optional


WINDOW_W = 320
WINDOW_H = 240
FPS = 30
CONTROL_API_SOCKET = os.environ.get("DVDPLAYER_CONTROL_SOCKET", "/tmp/rgbpi-dvdplayer-api.sock")
CONTROL_STATE_PATH = os.environ.get("DVDPLAYER_STATE_PATH", "/tmp/rgbpi-dvdplayer-state.json")
ROOT_BROWSE_PATHS = ["/mnt/nas", "/mnt", "/media/usb1", "/media/usb2"]
DVD_SCAN_ROOTS = ["/media/usb1", "/media/usb2"]


class Screen(str, Enum):
    HOME = "home"
    LIST = "list"
    BUSY = "busy"
    DVD_PICKER = "dvd_picker"
    PLEX_LINK = "plex_link"
    PLEX_CODE = "plex_code"
    YOUTUBE_LINK = "youtube_link"
    KEYBOARD = "keyboard"
    CONFIRM = "confirm"
    PLAYBACK = "playback"


class Action(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ACCEPT = "accept"
    BACK = "back"
    START = "start"
    SELECT = "select"
    X = "x"
    HOME = "home"
    QUIT = "quit"


class PlaybackKind(str, Enum):
    VIDEO_FILE = "video_file"
    DVD_FOLDER = "dvd_folder"
    DVD_ISO = "dvd_iso"
    OPTICAL_DRIVE = "optical_drive"
    PLEX_VIDEO = "plex_video"
    YOUTUBE_VIDEO = "youtube_video"


@dataclass
class PlaybackSource:
    title: str
    kind: PlaybackKind
    uri: str
    subtitle: str = ""
    authored_dvd: bool = False
    file_size: Optional[int] = None
    container: Optional[str] = None
    hint_width: Optional[int] = None
    hint_height: Optional[int] = None
    hint_fps: Optional[float] = None


@dataclass
class DvdCandidate:
    title: str
    subtitle: str
    source: PlaybackSource


@dataclass
class ListItem:
    title: str
    subtitle: str
    kind: str
    path: Optional[str] = None
    payload: dict = field(default_factory=dict)


@dataclass
class MessageBox:
    title: str
    body: str


@dataclass
class RuntimeSnapshot:
    pid: int
    started_at_unix_ms: int
    updated_at_unix_ms: int
    control_socket: str
    screen: str
    title: str
    section: str
    status_line: str
    screensaver: bool
    message_title: Optional[str]
    message_body: Optional[str]
    selected_index: Optional[int]
    item_count: Optional[int]
    overlay: Optional[str]
    playback_ipc_path: Optional[str]
    playback_kind: Optional[str]
    playback_title: Optional[str]
    playback_position_seconds: Optional[float]
    playback_duration_seconds: Optional[float]
    playback_paused: Optional[bool]
    playback_speed: Optional[float]
    bookmark_seconds: Optional[float]
    volume: Optional[float]
    audio_track_id: Optional[int]
    subtitle_track_id: Optional[int]
    drm_mode_target: Optional[str]
    drm_mode_effective: Optional[str]
    drm_mode_label: Optional[str]
    drm_connector: Optional[str]
    playback_backend: Optional[str]
    playback_profile: Optional[str]
    playback_degraded: bool
    youtube_link_state: str
    youtube_screen_name: Optional[str]
    youtube_queue_size: int
    youtube_receiver_healthy: bool
    overlay_focus: Optional[int]
    overlay_items: List[str]
    active_tty: Optional[str]
    items: List[str]


@dataclass
class BookmarkState:
    title: str
    uri: str
    position_seconds: float
    duration_seconds: Optional[float]
    updated_at_unix_ms: int


@dataclass
class LastPlayedState:
    source: PlaybackSource
    position_seconds: float
    duration_seconds: Optional[float]
    updated_at_unix_ms: int


@dataclass
class PlaybackPrefs:
    volume: float = 72.0
    stereo_downmix: bool = True
    preferred_audio_language: Optional[str] = None
    subtitles_enabled: bool = False
    preferred_subtitle_language: Optional[str] = None
    motion_mode: str = "smooth_tv"
    default_mode: str = "60hz"
    force_43: bool = False
    volume_normalization: str = "light"
    deinterlace_mode: str = "weave"


def app_dir() -> Path:
    custom = Path(__import__("os").environ.get("DVDPLAYER_APP_DIR", "")).expanduser()
    if custom and custom.is_dir():
        return custom
    return Path.cwd()
