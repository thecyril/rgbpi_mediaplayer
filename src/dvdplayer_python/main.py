from __future__ import annotations

import json
import os
import queue
import signal
import time
import subprocess
import fcntl
import struct
import threading
from glob import glob
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

import pygame

from dvdplayer_python.control.server import ControlServer
from dvdplayer_python.core.debuglog import log_event, log_path
from dvdplayer_python.core.models import (
    CONTROL_API_SOCKET,
    CONTROL_STATE_PATH,
    ROOT_BROWSE_PATHS,
    Action,
    DvdCandidate,
    ListItem,
    MessageBox,
    PlaybackKind,
    PlaybackSource,
    RuntimeSnapshot,
    Screen,
    app_dir,
)
from dvdplayer_python.core.persistence import PlaybackStateStore, cleanup_stale_runtime_files
from dvdplayer_python.media.network_backend import NetworkBackend, make_saved_root
from dvdplayer_python.media.plex_client import PlexClient
from dvdplayer_python.media.scanner import scan_dvd_candidates, scan_local_items
from dvdplayer_python.media.youtube_receiver import (
    YOUTUBE_LINK_CODE_PENDING,
    YOUTUBE_LINK_LINKED,
    YOUTUBE_LINK_UNLINKED,
    YouTubeReceiverManager,
    resolve_youtube_stream,
)
from dvdplayer_python.playback.session import PlaybackSession
from dvdplayer_python.ui.renderer import RenderModel, Renderer

APP_WINDOW_TITLE = "DVD Mediaplayer"
FPS = 30
BOOKMARK_SAVE_INTERVAL = 5
RESUME_MIN_SECONDS = 15.0
RESUME_CLEAR_THRESHOLD = 10.0
IGNORE_PYGAME_QUIT = os.environ.get("DVDPLAYER_IGNORE_PYGAME_QUIT", "1") != "0"
PLEX_LINK_POLL_INTERVAL = 2.0
JS_COMBO_WINDOW_SECS = 0.18
BUSY_ANIMATION_INTERVAL_SECS = 0.25
JS_EVENT_SIZE = 8
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80
JS_AXIS_THRESHOLD = 20000
OVERLAY_ACTION_TOGGLE_PAUSE = "toggle_pause"
OVERLAY_ACTION_DVD_MENU = "dvd_menu"
OVERLAY_ACTION_CHAPTER_PREV = "chapter_prev"
OVERLAY_ACTION_CHAPTER_NEXT = "chapter_next"
OVERLAY_ACTION_AUDIO_TRACKS = "audio_tracks"
OVERLAY_ACTION_AUDIO_TRACK_PREFIX = "audio_track:"
OVERLAY_ACTION_SUBTITLES = "subtitles_menu"
OVERLAY_ACTION_INFORMATION = "information"
OVERLAY_ACTION_RETURN_TO_BROWSER = "return_to_browser"
OVERLAY_ACTION_SUBTITLE_OFF = "subtitle_off"
OVERLAY_ACTION_SUBTITLE_TRACK_PREFIX = "subtitle_track:"

START_MENU_ENTRIES_DVD = [
    (OVERLAY_ACTION_TOGGLE_PAUSE, "TOGGLE PAUSE"),
    (OVERLAY_ACTION_DVD_MENU, "DVD MENU"),
    (OVERLAY_ACTION_CHAPTER_PREV, "CHAPTER -"),
    (OVERLAY_ACTION_CHAPTER_NEXT, "CHAPTER +"),
    (OVERLAY_ACTION_AUDIO_TRACKS, "AUDIO TRACK"),
    (OVERLAY_ACTION_SUBTITLES, "ENABLE SUBTITLES"),
    (OVERLAY_ACTION_INFORMATION, "INFORMATION"),
    (OVERLAY_ACTION_RETURN_TO_BROWSER, "RETURN TO BROWSER"),
]
START_MENU_ENTRIES_VIDEO = [
    (OVERLAY_ACTION_TOGGLE_PAUSE, "TOGGLE PAUSE"),
    (OVERLAY_ACTION_AUDIO_TRACKS, "AUDIO TRACK"),
    (OVERLAY_ACTION_SUBTITLES, "ENABLE SUBTITLES"),
    (OVERLAY_ACTION_INFORMATION, "INFORMATION"),
    (OVERLAY_ACTION_RETURN_TO_BROWSER, "RETURN TO BROWSER"),
]
START_MENU_ENTRIES_PLEX = [
    (OVERLAY_ACTION_TOGGLE_PAUSE, "TOGGLE PAUSE"),
    (OVERLAY_ACTION_AUDIO_TRACKS, "AUDIO TRACK"),
    (OVERLAY_ACTION_SUBTITLES, "ENABLE SUBTITLES"),
    (OVERLAY_ACTION_INFORMATION, "INFORMATION"),
    (OVERLAY_ACTION_RETURN_TO_BROWSER, "RETURN TO BROWSER"),
]
VISIBLE_LIST_ROWS = 6
HOME_MENU_SIZE = 5
NETWORK_AUTH_GUEST = "GUEST"
NETWORK_AUTH_LOGIN = "LOGIN"
KEYBOARD_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
KEYBOARD_NUMBERS = "0123456789"
KEYBOARD_SYMBOLS = "._-@"
AUDIO_LANGUAGE_ALIASES = {
    "DUT": ("DUT", "NLD", "NL"),
    "NLD": ("NLD", "DUT", "NL"),
    "NL": ("NL", "NLD", "DUT"),
    "ENG": ("ENG", "EN"),
    "EN": ("EN", "ENG"),
    "FRE": ("FRE", "FRA", "FR"),
    "FRA": ("FRA", "FRE", "FR"),
    "FR": ("FR", "FRA", "FRE"),
    "GER": ("GER", "DEU", "DE"),
    "DEU": ("DEU", "GER", "DE"),
    "DE": ("DE", "DEU", "GER"),
    "SPA": ("SPA", "ES"),
    "ES": ("ES", "SPA"),
    "ITA": ("ITA", "IT"),
    "IT": ("IT", "ITA"),
    "JPN": ("JPN", "JA"),
    "JA": ("JA", "JPN"),
}


def start_menu_entries_for_source(source: Optional[PlaybackSource]) -> list[tuple[str, str]]:
    if source and source.kind == PlaybackKind.PLEX_VIDEO and not source.authored_dvd:
        return list(START_MENU_ENTRIES_PLEX)
    if source and source.kind in {PlaybackKind.VIDEO_FILE, PlaybackKind.YOUTUBE_VIDEO} and not source.authored_dvd:
        return list(START_MENU_ENTRIES_VIDEO)
    return list(START_MENU_ENTRIES_DVD)


class App:
    def __init__(self):
        self.app_dir = app_dir()
        self.state_dir = self.app_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = self.state_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = None
        self._acquire_singleton_lock()

        cleanup_stale_runtime_files(CONTROL_API_SOCKET, CONTROL_STATE_PATH)

        self.renderer = Renderer()
        self.control_queue: queue.Queue = queue.Queue()
        self.background_queue: queue.Queue = queue.Queue()
        self.control = ControlServer(
            CONTROL_API_SOCKET,
            CONTROL_STATE_PATH,
            self.control_queue,
            fallback_dir=self.state_dir,
        )
        self.control.start()

        self.playback_state = PlaybackStateStore(self.state_dir)
        self.network = NetworkBackend(self.app_dir)
        self.plex = PlexClient(self.state_dir)
        self.youtube = YouTubeReceiverManager(self.app_dir, self.state_dir, self.control_queue)
        self.youtube_queue: list[dict] = []

        self.screen = Screen.HOME
        self.section = "HOME"
        self.home_selected = 0
        self.list_items: list[ListItem] = []
        self.list_selected = 0
        self.dvd_candidates: list[DvdCandidate] = []
        self.dvd_selected = 0
        self.playback: Optional[PlaybackSession] = None
        self.playback_source: Optional[PlaybackSource] = None
        self.return_screen_after_playback: Optional[Screen] = None
        self.return_section_after_playback = ""
        self.return_list_items: list[ListItem] = []
        self.return_list_selected = 0
        self.playback_overlay: Optional[str] = None
        self.playback_overlay_focus = 0
        self.playback_overlay_items: list[str] = []
        self.playback_overlay_actions: list[str] = []
        self.session_audio_language: Optional[str] = None
        self.session_audio_label_key: Optional[str] = None
        self.session_audio_track_id: Optional[int] = None
        self.playback_audio_prompt_resume_after = False
        self.playback_bookmark_key: Optional[str] = None
        self.last_bookmark_save = 0.0
        self.last_input = time.time()
        self.screensaver = False
        self.status_line = ""
        self.message: Optional[MessageBox] = None
        self.plex_link_pin_id: Optional[int] = None
        self.plex_link_expires_at = 0.0
        self.plex_link_last_poll = 0.0
        self.plex_link_code = ""
        self.pending_screenshots: list[Path] = []
        self.busy_context: Optional[str] = None
        self.busy_label = ""
        self.busy_started_at = 0.0
        self.busy_frame = 0
        self.busy_return_screen = Screen.LIST
        self.busy_return_section = ""
        self.confirm_context: Optional[str] = None
        self.confirm_options: list[str] = []
        self.confirm_selected = 0
        self.confirm_payload: dict = {}
        self.keyboard_context: Optional[str] = None
        self.keyboard_title = ""
        self.keyboard_value = ""
        self.keyboard_selected = 1
        self.keyboard_letter_index = 0
        self.keyboard_number_index = 0
        self.keyboard_symbol_index = 0
        self.keyboard_host: dict = {}
        self.keyboard_username = ""
        self.keyboard_saved_password = ""
        self.started_at_ms = _now_ms()
        self.active_tty = _detect_tty()
        if self.active_tty:
            os.environ["DVDPLAYER_ACTIVE_TTY"] = self.active_tty
        self.running = True
        self._js_stop = threading.Event()
        self._js_thread: Optional[threading.Thread] = None
        self._js_axis_state: dict[int, tuple[bool, bool]] = {}
        self._js_pending_combo_button: Optional[int] = None
        self._js_pending_combo_at = 0.0

        self.local_roots = [Path(p) for p in ROOT_BROWSE_PATHS if Path(p).is_dir()]
        self._start_joystick_listener()
        self.refresh_sources()
        self._autostart_youtube_receiver()
        log_event(
            "app_init",
            app_dir=str(self.app_dir),
            state_dir=str(self.state_dir),
            control_socket=CONTROL_API_SOCKET,
            state_path=CONTROL_STATE_PATH,
            log_path=log_path(),
            active_tty=self.active_tty,
        )

    def set_screen(self, screen: Screen, section: str):
        old = self.screen.value
        self.screen = screen
        self.section = section
        log_event("screen_change", old=old, new=screen.value, section=section)

    def refresh_sources(self):
        self.dvd_candidates = scan_dvd_candidates()
        self.status_line = (
            f"{len(self.dvd_candidates)} DVD source(s) ready" if self.dvd_candidates else "No DVD source found"
        )
        log_event("sources_refreshed", candidates=len(self.dvd_candidates), status=self.status_line)

    def _autostart_youtube_receiver(self):
        try:
            if self.youtube.ensure_started():
                log_event("youtube_receiver_autostart", status="ok")
                return
            error = self.youtube.state.last_error or "receiver_unavailable"
            self.status_line = "YouTube receiver unavailable"
            log_event("youtube_receiver_autostart", status="failed", error=error)
        except Exception as exc:
            self.status_line = "YouTube receiver unavailable"
            log_event("youtube_receiver_autostart", status="exception", error=str(exc))

    def run(self):
        clock = pygame.time.Clock()
        runtime_exc: Optional[Exception] = None
        try:
            while self.running:
                self._pump_control()
                self._pump_pygame()
                self._tick()
                self._draw()
                self._write_runtime_state()
                self._flush_screenshots()
                clock.tick(FPS)
        except Exception as exc:
            runtime_exc = exc
            log_event("app_runtime_exception", error=str(exc))
            self.running = False
        finally:
            if runtime_exc:
                self._force_playback_cleanup("runtime_exception")
            self.shutdown()

    def shutdown(self):
        log_event("app_shutdown")
        self._js_stop.set()
        self.youtube.stop()
        if self.playback:
            self.persist_bookmark(force=True)
            self.playback.quit()
        if self._lock_fh:
            try:
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None
        pygame.quit()

    def _reset_playback_overlay_state(self):
        self.playback_overlay = None
        self.playback_overlay_focus = 0
        self.playback_overlay_items = []
        self.playback_overlay_actions = []
        self.playback_audio_prompt_resume_after = False

    def _force_playback_cleanup(self, reason: str):
        if not self.playback:
            self._reset_playback_overlay_state()
            return
        try:
            self.persist_bookmark(force=True)
            self.playback.quit()
            log_event("playback_force_cleanup", reason=reason)
        except Exception as exc:
            log_event("playback_force_cleanup_failed", reason=reason, error=str(exc))
        self.playback = None
        self.playback_source = None
        self.playback_bookmark_key = None
        self._reset_playback_overlay_state()

    def _start_joystick_listener(self):
        self._js_thread = threading.Thread(target=self._poll_linux_js, daemon=True)
        self._js_thread.start()

    def _poll_linux_js(self):
        js_path = None
        while not self._js_stop.is_set():
            paths = sorted(glob("/dev/input/js*"))
            if paths:
                js_path = paths[0]
                break
            time.sleep(1.0)
        if not js_path:
            log_event("js_device_missing")
            return

        try:
            fd = os.open(js_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            log_event("js_open_failed", path=js_path, error=str(exc))
            return

        log_event("js_opened", path=js_path)
        try:
            while not self._js_stop.is_set():
                self._flush_pending_combo()
                try:
                    data = os.read(fd, JS_EVENT_SIZE * 32)
                except BlockingIOError:
                    self._flush_pending_combo()
                    time.sleep(0.01)
                    continue
                except OSError as exc:
                    log_event("js_read_failed", error=str(exc))
                    break
                if not data:
                    self._flush_pending_combo()
                    time.sleep(0.01)
                    continue
                for i in range(0, len(data) - (len(data) % JS_EVENT_SIZE), JS_EVENT_SIZE):
                    chunk = data[i : i + JS_EVENT_SIZE]
                    _time_ms, value, etype, number = struct.unpack("<IhBB", chunk)
                    etype = etype & ~JS_EVENT_INIT
                    if etype == JS_EVENT_BUTTON and value:
                        if self._handle_js_button_press(number):
                            continue
                    elif etype == JS_EVENT_AXIS:
                        self._flush_pending_combo(force=True)
                        self._handle_js_axis(number, value)
        finally:
            self._flush_pending_combo(force=True)
            try:
                os.close(fd)
            except Exception:
                pass

    def _handle_js_button_press(self, number: int) -> bool:
        now = time.time()
        if number in {6, 7}:
            pending = self._js_pending_combo_button
            if pending is not None and pending != number and (now - self._js_pending_combo_at) <= JS_COMBO_WINDOW_SECS:
                log_event("js_combo", buttons="start+select", action=Action.QUIT.value)
                self._js_pending_combo_button = None
                self._js_pending_combo_at = 0.0
                self.control_queue.put(("action", Action.QUIT))
                return True
            self._js_pending_combo_button = int(number)
            self._js_pending_combo_at = now
            return True

        action = _map_joystick_button(number)
        if action:
            log_event("js_button", number=int(number), value=1, action=action.value)
            self.control_queue.put(("action", action))
            return True
        return False

    def _flush_pending_combo(self, force: bool = False):
        pending = self._js_pending_combo_button
        if pending is None:
            return
        if not force and (time.time() - self._js_pending_combo_at) < JS_COMBO_WINDOW_SECS:
            return
        self._js_pending_combo_button = None
        self._js_pending_combo_at = 0.0
        action = _map_joystick_button(pending)
        if action:
            log_event("js_button", number=int(pending), value=1, action=action.value)
            self.control_queue.put(("action", action))

    def _handle_js_axis(self, axis: int, value: int):
        action_pair = _map_joystick_axis(axis)
        if not action_pair:
            return
        neg_action, pos_action = action_pair
        neg_active, pos_active = self._js_axis_state.get(axis, (False, False))
        now_neg = value <= -JS_AXIS_THRESHOLD
        now_pos = value >= JS_AXIS_THRESHOLD
        if now_neg and not neg_active:
            log_event("js_axis", axis=int(axis), value=int(value), action=neg_action.value)
            self.control_queue.put(("action", neg_action))
        if now_pos and not pos_active:
            log_event("js_axis", axis=int(axis), value=int(value), action=pos_action.value)
            self.control_queue.put(("action", pos_action))
        self._js_axis_state[axis] = (now_neg, now_pos)

    def _acquire_singleton_lock(self):
        preferred = Path(os.environ.get("DVDPLAYER_LOCK_PATH", str(self.runtime_dir / "rgbpi-dvdplayer.lock")))
        candidates = [preferred, Path(f"/tmp/rgbpi-dvdplayer-{os.getuid()}.lock"), Path("/tmp/rgbpi-dvdplayer.lock")]
        fh = None
        lock_path = None
        last_err = None
        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                fh = candidate.open("w")
                lock_path = candidate
                break
            except OSError as exc:
                last_err = exc
                continue
        if fh is None or lock_path is None:
            raise RuntimeError(f"cannot open lock file: {last_err}")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise RuntimeError("another instance running")
        fh.write(str(os.getpid()) + "\n")
        fh.flush()
        self._lock_fh = fh

    def _pump_control(self):
        while True:
            try:
                event, payload = self.control_queue.get_nowait()
            except queue.Empty:
                break
            log_event("control_event", control_event=event, payload=str(payload))
            if event == "action":
                self.dispatch(payload, source="control")
            elif event == "wake":
                self.screensaver = False
                self.last_input = time.time()
            elif event == "play-dvd":
                self.activate_play_dvd()
            elif event == "screenshot":
                self.pending_screenshots.append(payload)
            elif event == "show-overlay":
                name = str(payload).strip().lower()
                if name in {"start", "start_menu"}:
                    self._open_start_overlay()
                elif name in {"seek", "select_bar"}:
                    self._open_seek_overlay()
                elif name in {"none", "off", "clear"}:
                    self._close_overlay()
            elif event == "debug-ui":
                self.debug_ui(str(payload))
            elif event == "keyboard-fill":
                value = str(payload)
                self.status_line = f"INPUT: {value[:48]}"
                if self.screen == Screen.KEYBOARD:
                    self.keyboard_value = value
                log_event("keyboard_fill", value=value)
            elif event == "keyboard-submit":
                value = str(payload)
                self.status_line = f"SUBMIT: {value[:48]}"
                if self.screen == Screen.KEYBOARD:
                    if value:
                        self.keyboard_value = value
                    self._submit_keyboard_value()
                log_event("keyboard_submit", value=value)
            elif event == "remote-playpause":
                if self.playback:
                    self.playback.set_pause(not self.playback.pause_state())
            elif event == "remote-pause":
                if self.playback:
                    self.playback.set_pause(bool(payload))
            elif event == "remote-stop":
                if self.playback:
                    self.stop_playback("Playback stopped")
            elif event == "remote-seek-ms":
                if self.playback:
                    self.playback.seek_absolute(float(payload) / 1000.0)
            elif event == "remote-seek-relative":
                if self.playback:
                    self.playback.seek_relative(int(payload))
            elif event == "remote-set-chapter":
                if self.playback:
                    try:
                        self.playback.set_chapter(int(payload))
                        log_event("chapter_set", chapter=int(payload), via="remote")
                    except Exception as exc:
                        log_event("chapter_set_failed", chapter=int(payload), error=str(exc), via="remote")
            elif event == "remote-step-chapter":
                if self.playback:
                    try:
                        chapter = self.playback.step_chapter(int(payload))
                        log_event("chapter_step", delta=int(payload), chapter=chapter, via="remote")
                    except Exception as exc:
                        log_event("chapter_step_failed", delta=int(payload), error=str(exc), via="remote")
            elif event == "remote-play-json":
                self.handle_remote_play_json(payload)
            elif event == "youtube-sidecar-event":
                self._handle_youtube_sidecar_event(payload)
            elif event == "youtube_link_start":
                self.open_youtube_link()
            elif event == "youtube_unlink":
                self.unlink_youtube()
            elif event == "youtube_queue_next":
                self._play_next_queued_youtube()
            elif event == "youtube_queue_clear":
                self.youtube_queue.clear()
                self.youtube.queue_clear()
                self.status_line = "YouTube queue cleared"

    def debug_ui(self, value: str):
        value = value.strip().lower()
        log_event("debug_ui", value=value)
        if value == "home":
            self.go_home()
            return
        if value == "browser-mode":
            self.open_browser_mode()
            return
        if value == "network-home":
            self.open_network_home()
            return
        if value == "network-add":
            self.open_network_add()
            return
        if value == "scan-smb":
            self.scan_network("SMB")
            return
        if value == "scan-nfs":
            self.scan_network("NFS")
            return
        if value == "plex-link":
            self.set_screen(Screen.PLEX_LINK, "PLEX LINK")
            return
        if value == "youtube-link":
            self.open_youtube_link()

    def _pump_pygame(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                log_event("pygame_quit_event", ignored=IGNORE_PYGAME_QUIT)
                if not IGNORE_PYGAME_QUIT:
                    self.running = False
                    log_event("app_stop_requested", reason="pygame_quit")
                    continue
            if event.type == pygame.KEYDOWN:
                log_event("pygame_keydown", key=int(event.key))
                action = _map_key(event.key)
                if action:
                    self.dispatch(action, source="keyboard")

    def _tick(self):
        now = time.time()
        self.youtube.tick(now)
        self._tick_background_work(now)
        self._tick_plex_link(now)

        if self.playback:
            if not self.playback.is_running():
                self.stop_playback("Playback finished")
                return
            if now - self.last_bookmark_save >= BOOKMARK_SAVE_INTERVAL:
                self.persist_bookmark(force=False)
                self.last_bookmark_save = now

    def dispatch(self, action: Action, source: str):
        self.last_input = time.time()
        log_event(
            "action",
            source=source,
            action=action.value,
            screen=self.screen.value,
            home_selected=self.home_selected,
            list_selected=self.list_selected,
            dvd_selected=self.dvd_selected,
        )
        if self.screensaver and action in {Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT}:
            self.screensaver = False
            log_event("screensaver_off", reason="navigation_action")
            return
        self.screensaver = False

        if self.message and action in {Action.ACCEPT, Action.BACK, Action.START, Action.SELECT}:
            log_event("message_dismiss", title=self.message.title)
            self.message = None
            return

        if action == Action.QUIT:
            self.running = False
            return
        if action == Action.HOME:
            self.go_home()
            return

        if self.playback:
            self.handle_playback_action(action)
            return

        if self.screen == Screen.HOME:
            self.handle_home_action(action)
        elif self.screen == Screen.LIST:
            self.handle_list_action(action)
        elif self.screen == Screen.DVD_PICKER:
            self.handle_dvd_picker_action(action)
        elif self.screen == Screen.CONFIRM:
            self.handle_confirm_action(action)
        elif self.screen == Screen.KEYBOARD:
            self.handle_keyboard_action(action)
        elif self.screen == Screen.PLEX_LINK:
            self.handle_plex_link_action(action)
        elif self.screen == Screen.PLEX_CODE:
            self.handle_plex_code_action(action)
        elif self.screen == Screen.YOUTUBE_LINK:
            self.handle_youtube_link_action(action)

    def handle_home_action(self, action: Action):
        if action == Action.UP:
            self.home_selected = (self.home_selected - 1) % HOME_MENU_SIZE
            log_event("home_select", selected=self.home_selected, item=self._home_row(self.home_selected)[0])
        elif action == Action.DOWN:
            self.home_selected = (self.home_selected + 1) % HOME_MENU_SIZE
            log_event("home_select", selected=self.home_selected, item=self._home_row(self.home_selected)[0])
        elif action in {Action.ACCEPT, Action.START}:
            item = self._home_row(self.home_selected)[0]
            log_event("home_accept", selected=self.home_selected, item=item, via=action.value)
            if self.home_selected == 0:
                self.activate_play_dvd()
            elif self.home_selected == 1:
                self.open_browser_mode()
            elif self.home_selected == 2:
                self.open_media_server_menu()
            elif self.home_selected == 3:
                self.open_settings_menu()
            elif self.home_selected == 4:
                self.resume_last_playback(open_settings_on_missing=False)

    def handle_list_action(self, action: Action):
        if action == Action.UP and self.list_items:
            self.list_selected = (self.list_selected - 1) % len(self.list_items)
            self._log_list_selection()
        elif action == Action.DOWN and self.list_items:
            self.list_selected = (self.list_selected + 1) % len(self.list_items)
            self._log_list_selection()
        elif action in {Action.LEFT, Action.RIGHT} and self.list_items:
            item = self.list_items[self.list_selected]
            if self._adjust_switchable_setting(item, action):
                return
        elif action == Action.X and self.list_items:
            self._save_selected_network_favorite()
        elif action in {Action.BACK, Action.SELECT}:
            self.go_home()
        elif action in {Action.ACCEPT, Action.START} and self.list_items:
            item = self.list_items[self.list_selected]
            if self._is_switchable_setting_item(item):
                self.status_line = "Use LEFT/RIGHT to change"
                log_event("settings_accept_ignored", kind=item.kind, via=action.value)
                return
            log_event("list_accept", selected=self.list_selected, title=item.title, kind=item.kind, path=item.path, via=action.value)
            self.activate_list_item(item)

    def _log_list_selection(self):
        if not self.list_items:
            return
        item = self.list_items[self.list_selected]
        log_event(
            "list_select",
            selected=self.list_selected,
            count=len(self.list_items),
            title=item.title,
            kind=item.kind,
            screen=self.screen.value,
            section=self.section,
        )

    def _visible_list_window(self, items: list[ListItem], selected: int, window_size: int = VISIBLE_LIST_ROWS) -> tuple[list[ListItem], int]:
        if not items:
            return [], 0
        if len(items) <= window_size:
            return items, min(selected, len(items) - 1)
        start = max(0, selected - (window_size // 2))
        max_start = max(0, len(items) - window_size)
        start = min(start, max_start)
        visible = items[start : start + window_size]
        visible_selected = max(0, min(selected - start, len(visible) - 1))
        return visible, visible_selected

    def handle_dvd_picker_action(self, action: Action):
        if not self.dvd_candidates:
            self.go_home()
            return
        if action == Action.UP:
            self.dvd_selected = (self.dvd_selected - 1) % len(self.dvd_candidates)
            log_event("dvd_select", selected=self.dvd_selected, title=self.dvd_candidates[self.dvd_selected].title)
        elif action == Action.DOWN:
            self.dvd_selected = (self.dvd_selected + 1) % len(self.dvd_candidates)
            log_event("dvd_select", selected=self.dvd_selected, title=self.dvd_candidates[self.dvd_selected].title)
        elif action in {Action.BACK, Action.SELECT}:
            self.go_home()
        elif action == Action.ACCEPT:
            candidate = self.dvd_candidates[self.dvd_selected]
            log_event("dvd_accept", selected=self.dvd_selected, title=candidate.title)
            self.start_playback(candidate.source)

    def handle_confirm_action(self, action: Action):
        if not self.confirm_options:
            self.go_home()
            return
        if action == Action.UP:
            self.confirm_selected = (self.confirm_selected - 1) % len(self.confirm_options)
            return
        if action == Action.DOWN:
            self.confirm_selected = (self.confirm_selected + 1) % len(self.confirm_options)
            return
        if action in {Action.BACK, Action.SELECT}:
            self._close_confirm_popup()
            return
        if action not in {Action.ACCEPT, Action.START}:
            return
        choice = self.confirm_options[self.confirm_selected]
        if self.confirm_context == "resume_playback":
            source = self.confirm_payload.get("source")
            resume_seconds = float(self.confirm_payload.get("resume_seconds", 0.0))
            return_section = str(self.confirm_payload.get("return_section", self.section))
            return_screen_raw = str(self.confirm_payload.get("return_screen", Screen.LIST.value))
            try:
                return_screen = Screen(return_screen_raw)
            except ValueError:
                return_screen = Screen.LIST
            self.confirm_context = None
            self.confirm_options = []
            self.confirm_selected = 0
            self.confirm_payload = {}
            if isinstance(source, PlaybackSource):
                self.set_screen(return_screen, return_section)
                if choice == "RESUME PLAYBACK" and resume_seconds >= RESUME_MIN_SECONDS:
                    self.start_playback(source, resume_prompt=False, resume_seconds=resume_seconds)
                    return
                self.start_playback(source, resume_prompt=False, start_from_beginning=True)
                return
            self.message = MessageBox("PLAYBACK", "Resume source unavailable")
            self.go_home()
            return
        if self.confirm_context == "smb_auth":
            host = dict(self.confirm_payload.get("host", {}))
            if choice == NETWORK_AUTH_GUEST:
                self._start_network_host_browse(host, None, None)
                return
            if choice == NETWORK_AUTH_LOGIN:
                saved = self.network.saved_credentials("SMB", host.get("address", host.get("host", "")))
                username, password = saved if saved else ("", "")
                self._open_keyboard_input(
                    context="smb_user",
                    title="SMB USER",
                    host=host,
                    initial=username,
                    username="",
                    saved_password=password,
                )
                return
        self._close_confirm_popup()

    def handle_keyboard_action(self, action: Action):
        if action == Action.UP:
            self.keyboard_selected = 5 if self.keyboard_selected <= 1 else self.keyboard_selected - 1
            return
        if action == Action.DOWN:
            self.keyboard_selected = 1 if self.keyboard_selected >= 5 else self.keyboard_selected + 1
            return
        if action == Action.LEFT:
            self._keyboard_shift(-1)
            return
        if action == Action.RIGHT:
            self._keyboard_shift(1)
            return
        if action in {Action.BACK, Action.SELECT}:
            if self.keyboard_value:
                self.keyboard_value = self.keyboard_value[:-1]
                return
            self._close_keyboard_input()
            return
        if action == Action.START:
            self._submit_keyboard_value()
            return
        if action != Action.ACCEPT:
            return
        if self.keyboard_selected == 1:
            self.keyboard_value += KEYBOARD_LETTERS[self.keyboard_letter_index]
            return
        if self.keyboard_selected == 2:
            self.keyboard_value += KEYBOARD_NUMBERS[self.keyboard_number_index]
            return
        if self.keyboard_selected == 3:
            self.keyboard_value += KEYBOARD_SYMBOLS[self.keyboard_symbol_index]
            return
        if self.keyboard_selected == 4:
            self.keyboard_value += " "
            return
        if self.keyboard_selected == 5:
            self._submit_keyboard_value()

    def handle_plex_link_action(self, action: Action):
        if action == Action.BACK:
            self.plex_link_pin_id = None
            self.plex_link_expires_at = 0.0
            self.plex_link_last_poll = 0.0
            self.plex_link_code = ""
            self.go_home()
        elif action == Action.SELECT:
            self.status_line = "Press A or START for code. B goes back."
        elif action in {Action.ACCEPT, Action.START}:
            try:
                code = self.plex.begin_device_link()
                now = time.time()
                self.plex_link_pin_id = code.id
                self.plex_link_expires_at = now + max(0, code.expires_in)
                self.plex_link_last_poll = 0.0
                self.plex_link_code = code.code
                self.status_line = f"Go to plex.tv/link and enter: {code.code}"
                self.set_screen(Screen.PLEX_CODE, "PLEX CODE")
                log_event("plex_link_code", code=code.code, pin_id=code.id, expires_in=code.expires_in)
            except Exception as exc:
                self.message = MessageBox("PLEX", f"Link failed: {exc}")
                log_event("plex_link_failed", error=str(exc))

    def handle_plex_code_action(self, action: Action):
        if action == Action.BACK:
            self.plex_link_pin_id = None
            self.plex_link_expires_at = 0.0
            self.plex_link_last_poll = 0.0
            self.plex_link_code = ""
            self.set_screen(Screen.PLEX_LINK, "PLEX LINK")
            return
        if action == Action.SELECT:
            self.status_line = "Use plex.tv/link, then wait here."
            return
        if action in {Action.ACCEPT, Action.START}:
            self.status_line = f"Waiting for Plex link: {self.plex_link_code or 'code'}"

    def open_youtube_link(self):
        if not self.youtube.link_start():
            self.message = MessageBox("YOUTUBE", "Receiver unavailable")
            self.status_line = "YouTube receiver unavailable"
            return
        self.status_line = "Waiting for TV code"
        self.set_screen(Screen.YOUTUBE_LINK, "YOUTUBE LINK")

    def unlink_youtube(self):
        if not self.youtube.unlink():
            self.message = MessageBox("YOUTUBE", "Receiver unavailable")
            self.status_line = "YouTube receiver unavailable"
            return
        self.youtube_queue.clear()
        self.status_line = "YouTube link reset"
        if self.screen == Screen.LIST and self.section == "SETTINGS":
            self._refresh_settings_items()
        if self.screen == Screen.LIST and self.section == "MEDIA SERVER":
            self.open_media_server_menu()
        self.message = MessageBox("YOUTUBE", "YouTube unlinked")
        log_event("settings_unlink_youtube")

    def handle_youtube_link_action(self, action: Action):
        if action == Action.BACK:
            self.go_home()
            return
        if action == Action.SELECT:
            self.status_line = "Use TV app > Link with TV code"
            return
        if action in {Action.ACCEPT, Action.START}:
            if self.youtube.link_start():
                self.status_line = "Waiting for TV code"
            else:
                self.status_line = "YouTube receiver unavailable"

    def _start_youtube_resolve(self, request: dict):
        label = str(request.get("title") or request.get("video_id") or "YOUTUBE")
        self._start_busy("youtube_resolve", f"LOADING {label[:18]}", self.screen, self.section)
        thread = threading.Thread(target=self._resolve_youtube_worker, args=(request,), daemon=True)
        thread.start()

    def _resolve_youtube_worker(self, request: dict):
        try:
            ref = str(request.get("url") or request.get("video_id") or "").strip()
            if not ref:
                raise RuntimeError("missing_youtube_reference")
            resolved = resolve_youtube_stream(ref)
            payload = {
                "title": resolved.get("title") or request.get("title") or "YouTube",
                "subtitle": "YouTube",
                "kind": "youtube_video",
                "url": resolved["stream_url"],
                "authored_dvd": False,
                "hint_width": resolved.get("width"),
                "hint_height": resolved.get("height"),
                "hint_fps": resolved.get("fps"),
            }
            self.background_queue.put(("youtube_resolve_done", request, payload, None))
        except Exception as exc:
            self.background_queue.put(("youtube_resolve_done", request, None, str(exc)))

    def _finish_youtube_resolve(self, request: dict, payload: Optional[dict], error: Optional[str]):
        self._clear_busy()
        self.set_screen(self.busy_return_screen, self.busy_return_section or "YOUTUBE LINK")
        if error:
            self.message = MessageBox("YOUTUBE", "Cannot play video")
            self.status_line = f"YouTube failed: {error[:48]}"
            log_event("youtube_resolve_failed", error=error, video_id=request.get("video_id"), url=request.get("url"))
            return
        if not payload:
            self.message = MessageBox("YOUTUBE", "Cannot play video")
            self.status_line = "YouTube failed"
            log_event("youtube_resolve_failed", error="missing_payload")
            return
        self.handle_remote_play_json(payload)
        self.status_line = "YouTube playback active"
        log_event(
            "youtube_resolve_ok",
            title=payload.get("title"),
            video_id=request.get("video_id"),
            hint_width=payload.get("hint_width"),
            hint_height=payload.get("hint_height"),
            hint_fps=payload.get("hint_fps"),
        )

    def _play_next_queued_youtube(self):
        if self.youtube_queue:
            request = self.youtube_queue.pop(0)
            self._start_youtube_resolve(request)
            return
        self.youtube.queue_next()

    def _handle_youtube_sidecar_event(self, payload: object):
        if not isinstance(payload, dict):
            return
        event = str(payload.get("event") or "").strip().lower()
        if event == "link_state":
            state = str(payload.get("state") or "").strip().lower()
            code = str(payload.get("code") or "").strip()
            if state in {YOUTUBE_LINK_UNLINKED, YOUTUBE_LINK_CODE_PENDING, YOUTUBE_LINK_LINKED}:
                self.youtube.state.link_state = state
            self.youtube.state.code = code
            screen_name = payload.get("screen_name")
            if isinstance(screen_name, str) and screen_name.strip():
                self.youtube.state.screen_name = screen_name.strip()
            if self.youtube.state.link_state == YOUTUBE_LINK_LINKED:
                self.status_line = "YouTube linked"
            elif code:
                self.status_line = f"TV code: {code}"
            else:
                self.status_line = "Waiting for TV code"
            return

        if event == "receiver_ready":
            self.youtube.state.receiver_healthy = True
            version = payload.get("receiver_version")
            if isinstance(version, str) and version.strip():
                self.youtube.state.receiver_version = version.strip()
            screen_name = payload.get("screen_name")
            if isinstance(screen_name, str) and screen_name.strip():
                self.youtube.state.screen_name = screen_name.strip()
            return

        if event == "receiver_error":
            self.youtube.state.receiver_healthy = False
            self.status_line = "YouTube receiver error"
            error = str(payload.get("error") or "receiver_error")
            log_event("youtube_receiver_error", error=error)
            return

        if event == "receiver_exit":
            self.youtube.state.receiver_healthy = False
            self.status_line = "YouTube receiver stopped"
            return

        if event == "queue_add":
            request = {
                "video_id": str(payload.get("video_id") or "").strip(),
                "title": str(payload.get("title") or "").strip(),
                "url": str(payload.get("url") or "").strip(),
            }
            if request["video_id"] or request["url"]:
                self.youtube_queue.append(request)
            self.youtube.state.queue_size = max(self.youtube.state.queue_size, len(self.youtube_queue))
            return

        if event == "queue_clear":
            self.youtube_queue.clear()
            self.youtube.state.queue_size = 0
            return

        if event == "queue_next":
            self._play_next_queued_youtube()
            return

        if event == "play":
            request = {
                "video_id": str(payload.get("video_id") or "").strip(),
                "title": str(payload.get("title") or "").strip(),
                "url": str(payload.get("url") or "").strip(),
                "position_seconds": float(payload.get("position_seconds") or 0.0),
            }
            self._start_youtube_resolve(request)
            return

        if event == "pause" and self.playback:
            self.playback.set_pause(True)
            return

        if event == "resume" and self.playback:
            self.playback.set_pause(False)
            return

        if event == "seek" and self.playback:
            try:
                position_seconds = float(payload.get("position_seconds") or 0.0)
            except Exception:
                position_seconds = 0.0
            self.playback.seek_absolute(max(0.0, position_seconds))
            return

        if event in {"status", "player_state"}:
            queue_size = payload.get("queue_size")
            if isinstance(queue_size, (int, float)):
                self.youtube.state.queue_size = max(0, int(queue_size))

    def _tick_plex_link(self, now: float):
        if not self.plex_link_pin_id:
            return
        if self.plex_link_expires_at and now >= self.plex_link_expires_at:
            log_event("plex_link_expired", pin_id=self.plex_link_pin_id)
            self.plex_link_pin_id = None
            self.plex_link_expires_at = 0.0
            self.plex_link_last_poll = 0.0
            self.plex_link_code = ""
            self.status_line = "Code expired. Press A/START to retry"
            self.set_screen(Screen.PLEX_LINK, "PLEX LINK")
            self.message = MessageBox("PLEX", "Code expired")
            return
        if now - self.plex_link_last_poll < PLEX_LINK_POLL_INTERVAL:
            return
        self.plex_link_last_poll = now
        try:
            if self.plex.poll_device_link(self.plex_link_pin_id):
                server_name = self.plex.server_name()
                log_event("plex_link_complete", pin_id=self.plex_link_pin_id, server=server_name)
                self.plex_link_pin_id = None
                self.plex_link_expires_at = 0.0
                self.plex_link_last_poll = 0.0
                self.plex_link_code = ""
                self.status_line = f"Linked to {server_name}"
                self.message = MessageBox("PLEX", f"Linked to {server_name}")
                self.open_plex()
        except Exception as exc:
            log_event("plex_link_poll_failed", pin_id=self.plex_link_pin_id, error=str(exc))

    def handle_playback_action(self, action: Action):
        if not self.playback:
            return
        if self.playback_overlay in {"start_menu", "seek", "audio_menu", "subtitle_menu", "information"}:
            self._handle_playback_overlay_action(action)
            return
        if action == Action.BACK:
            self.stop_playback("Playback stopped")
            return
        if action == Action.START:
            self._open_start_overlay()
        elif action == Action.SELECT:
            self.stop_playback("Returned to browser")
        elif action == Action.LEFT:
            self.playback.seek_relative(-30)
            log_event("playback_seek", relative=-30)
        elif action == Action.RIGHT:
            self.playback.seek_relative(30)
            log_event("playback_seek", relative=30)
        elif action == Action.ACCEPT:
            paused = not self.playback.pause_state()
            self.playback.set_pause(paused)
            log_event("playback_pause", paused=paused)

    def _open_start_overlay(self):
        if not self.playback:
            return
        entries = start_menu_entries_for_source(self.playback_source)
        self.playback_overlay = "start_menu"
        self.playback_overlay_focus = 0
        self.playback_overlay_actions = [action_id for action_id, _ in entries]
        self.playback_overlay_items = [label for _, label in entries]
        self.playback.show_start_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
        source_kind = self.playback_source.kind.value if self.playback_source else "unknown"
        log_event(
            "overlay_open",
            overlay=self.playback_overlay,
            focus=self.playback_overlay_focus,
            source_kind=source_kind,
            items=len(self.playback_overlay_items),
        )

    def _open_seek_overlay(self):
        if not self.playback:
            return
        self.playback_overlay = "seek"
        self.playback_overlay_focus = 0
        self.playback_overlay_items = []
        self.playback_overlay_actions = []
        self.playback.show_seek_overlay(paused=self.playback.pause_state(), step_seconds=30)
        log_event("overlay_open", overlay=self.playback_overlay)

    def _open_audio_overlay(self, *, auto_prompt: bool = False):
        if not self.playback:
            return
        try:
            tracks = self.playback.audio_tracks()
        except Exception as exc:
            self.message = MessageBox("AUDIO", "Could not read audio tracks")
            log_event("overlay_action_failed", action_id=OVERLAY_ACTION_AUDIO_TRACKS, error=str(exc))
            self._close_overlay()
            return
        valid_tracks = [track for track in tracks if isinstance(track.get("id"), (int, float))]
        if not valid_tracks:
            self.message = MessageBox("AUDIO", "No audio tracks available")
            log_event("overlay_action_ok", action_id=OVERLAY_ACTION_AUDIO_TRACKS, result="no_tracks")
            self._close_overlay()
            return

        self.playback_overlay = "audio_menu"
        self.playback_overlay_items = [str(track.get("label", "TRACK")) for track in valid_tracks]
        self.playback_overlay_actions = [
            f"{OVERLAY_ACTION_AUDIO_TRACK_PREFIX}{int(track.get('id', -1))}" for track in valid_tracks
        ]
        current_aid = self.playback.current_audio_track()
        focus = 0
        if isinstance(current_aid, int):
            for index, action_id in enumerate(self.playback_overlay_actions):
                if action_id == f"{OVERLAY_ACTION_AUDIO_TRACK_PREFIX}{current_aid}":
                    focus = index
                    break
        self.playback_overlay_focus = focus
        if auto_prompt:
            try:
                was_paused = self.playback.pause_state()
                self.playback_audio_prompt_resume_after = not was_paused
                if not was_paused:
                    self.playback.set_pause(True)
            except Exception as exc:
                self.playback_audio_prompt_resume_after = False
                log_event("audio_prompt_pause_failed", error=str(exc))
            self.status_line = "Choose audio language"
        self.playback.show_audio_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
        log_event(
            "overlay_open",
            overlay=self.playback_overlay,
            focus=self.playback_overlay_focus,
            items=len(self.playback_overlay_items),
            auto_prompt=auto_prompt,
        )

    def _playback_prefs_for_session(self):
        if not self.session_audio_language:
            return self.playback_state.prefs
        return replace(
            self.playback_state.prefs,
            preferred_audio_language=self._audio_language_preference(self.session_audio_language),
        )

    def _remember_session_audio_track(self, track: dict) -> None:
        lang = str(track.get("lang") or "").strip().upper()
        label = str(track.get("label") or "").strip()
        track_id = track.get("id")
        self.session_audio_language = lang or None
        self.session_audio_label_key = self._audio_label_key(label)
        self.session_audio_track_id = int(track_id) if isinstance(track_id, (int, float)) else None
        log_event(
            "audio_session_choice_remembered",
            lang=self.session_audio_language,
            label=label,
            track_id=self.session_audio_track_id,
        )

    def _audio_label_key(self, value: object) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        return " ".join(text.split())

    def _audio_language_keys(self, value: object) -> set[str]:
        text = str(value or "").strip().upper()
        if not text:
            return set()
        return set(AUDIO_LANGUAGE_ALIASES.get(text, (text,)))

    def _audio_language_preference(self, value: object) -> str:
        keys = AUDIO_LANGUAGE_ALIASES.get(str(value or "").strip().upper(), (str(value or "").strip().upper(),))
        return ",".join(key.lower() for key in keys if key)

    def _matching_session_audio_track(self, tracks: list[dict]) -> Optional[dict]:
        if not tracks:
            return None
        if self.session_audio_language:
            preferred = self._audio_language_keys(self.session_audio_language)
            for track in tracks:
                if preferred & self._audio_language_keys(track.get("lang")):
                    return track
        if self.session_audio_label_key:
            for track in tracks:
                if self._audio_label_key(track.get("label")) == self.session_audio_label_key:
                    return track
        if self.session_audio_language and any(self._audio_language_keys(track.get("lang")) for track in tracks):
            return None
        if self.session_audio_track_id is not None:
            for track in tracks:
                track_id = track.get("id")
                if isinstance(track_id, (int, float)) and int(track_id) == self.session_audio_track_id:
                    return track
        return None

    def _apply_or_prompt_audio_track(self):
        if not self.playback:
            return
        try:
            tracks = self.playback.audio_tracks()
        except Exception as exc:
            log_event("audio_tracks_read_failed", error=str(exc))
            return
        valid_tracks = [track for track in tracks if isinstance(track.get("id"), (int, float))]
        if len(valid_tracks) <= 1:
            return
        matched = self._matching_session_audio_track(valid_tracks)
        if matched:
            self._select_audio_track(matched, remember=False, via="session")
            return
        self._open_audio_overlay(auto_prompt=True)

    def _select_audio_track(self, track: dict, *, remember: bool, via: str) -> None:
        if not self.playback:
            return
        track_id = track.get("id")
        if not isinstance(track_id, (int, float)):
            raise RuntimeError("audio track has no valid id")
        self.playback.set_audio_track(int(track_id))
        label = str(track.get("label") or f"TRACK {int(track_id)}")
        if remember:
            self._remember_session_audio_track(track)
        self.status_line = f"Audio: {label}"
        log_event(
            "audio_track_selected",
            track_id=int(track_id),
            lang=str(track.get("lang") or ""),
            label=label,
            remember=remember,
            via=via,
        )

    def _open_subtitle_overlay(self):
        if not self.playback:
            return
        try:
            tracks = self.playback.subtitle_tracks()
        except Exception as exc:
            self.message = MessageBox("SUBTITLES", "Could not read subtitle tracks")
            log_event("overlay_action_failed", action_id=OVERLAY_ACTION_SUBTITLES, error=str(exc))
            self._close_overlay()
            return
        if not tracks:
            self.message = MessageBox("SUBTITLES", "No subtitles available")
            log_event("overlay_action_ok", action_id=OVERLAY_ACTION_SUBTITLES, result="no_tracks")
            self._close_overlay()
            return
        valid_tracks = [track for track in tracks if isinstance(track.get("id"), (int, float))]
        if not valid_tracks:
            self.message = MessageBox("SUBTITLES", "No subtitles available")
            log_event("overlay_action_ok", action_id=OVERLAY_ACTION_SUBTITLES, result="no_valid_tracks")
            self._close_overlay()
            return
        self.playback_overlay = "subtitle_menu"
        self.playback_overlay_items = ["OFF"] + [str(track.get("label", "TRACK")) for track in valid_tracks]
        self.playback_overlay_actions = [OVERLAY_ACTION_SUBTITLE_OFF]
        for track in valid_tracks:
            self.playback_overlay_actions.append(f"{OVERLAY_ACTION_SUBTITLE_TRACK_PREFIX}{int(track.get('id', -1))}")
        current_sid = self.playback.current_subtitle_track()
        focus = 0
        if isinstance(current_sid, int):
            for index, action_id in enumerate(self.playback_overlay_actions):
                if action_id == f"{OVERLAY_ACTION_SUBTITLE_TRACK_PREFIX}{current_sid}":
                    focus = index
                    break
        self.playback_overlay_focus = focus
        self.playback.show_subtitle_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
        log_event(
            "overlay_open",
            overlay=self.playback_overlay,
            focus=self.playback_overlay_focus,
            items=len(self.playback_overlay_items),
        )

    def _read_playback_property(self, name: str):
        if not self.playback:
            return None
        try:
            return self.playback.get_property(name)
        except Exception:
            return None

    def _current_tv_hz_label(self) -> str:
        display_fps = self._read_playback_property("display-fps")
        try:
            fps = float(display_fps)
        except (TypeError, ValueError):
            fps = None
        if fps is not None:
            if fps >= 55.0:
                return "60HZ"
            if fps >= 45.0:
                return "50HZ"
        mode = (self.playback.effective_mode or self.playback.target_mode) if self.playback else None
        mode_text = str(mode or "").lower()
        if "576" in mode_text:
            return "50HZ"
        if "480" in mode_text:
            return "60HZ"
        return "UNKNOWN"

    def _current_interpolation_type(self) -> str:
        vf = self._read_playback_property("vf")
        if isinstance(vf, list):
            for item in vf:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").lower()
                if name != "lavfi":
                    continue
                params = item.get("params")
                if isinstance(params, dict):
                    graph = str(params.get("graph") or "").lower()
                    if "tblend" in graph:
                        return "TBLEND"
        interpolation = self._read_playback_property("interpolation")
        if bool(interpolation):
            tscale = str(self._read_playback_property("tscale") or "").strip().upper() or "MPV"
            return f"MPV {tscale}"
        return "OFF"

    def _open_information_overlay(self):
        if not self.playback:
            return
        video_codec = str(self._read_playback_property("video-codec") or "UNKNOWN").upper()
        audio_codec = str(
            self._read_playback_property("audio-codec-name")
            or self._read_playback_property("audio-codec")
            or "UNKNOWN"
        ).upper()
        video_params = self._read_playback_property("video-params")
        if isinstance(video_params, dict):
            # Storage resolution (pixels actually encoded in the file)
            w = int(video_params.get("w") or 0)
            h = int(video_params.get("h") or 0)
            # Display resolution after pixel aspect ratio correction
            # (e.g. DVD NTSC 720x480 with PAR 0.9091 -> displayed as 720x540)
            dw = int(video_params.get("dw") or w)
            dh = int(video_params.get("dh") or h)
        else:
            w = h = dw = dh = 0
        if w > 0 and h > 0:
            if (dw, dh) != (w, h):
                video_resolution = f"{w}x{h} (display {dw}x{dh})"
            else:
                video_resolution = f"{w}x{h}"
        else:
            video_resolution = "UNKNOWN"
        tv_mode = self.playback.effective_mode or self.playback.target_mode
        tv_resolution = str(tv_mode or "UNKNOWN").upper()
        video_fps_raw = self._read_playback_property("container-fps") or self._read_playback_property("estimated-vf-fps")
        try:
            video_fps = f"{float(video_fps_raw):.3f}"
        except (TypeError, ValueError):
            video_fps = "UNKNOWN"
        tv_hz = self._current_tv_hz_label()
        interpolation_type = self._current_interpolation_type()
        rows = [
            "INFORMATION",
            "",
            f"VIDEO CODEC: {video_codec}",
            f"AUDIO CODEC: {audio_codec}",
            f"VIDEO RESOLUTION: {video_resolution}",
            f"TV RESOLUTION: {tv_resolution}",
            f"VIDEO FPS: {video_fps}",
            f"TV MODE: {tv_hz}",
            f"INTERPOLATION TYPE: {interpolation_type}",
            "",
            "START/SELECT/B CLOSE",
        ]
        self.playback_overlay = "information"
        self.playback_overlay_focus = 0
        self.playback_overlay_items = []
        self.playback_overlay_actions = []
        self.playback.show_text("\n".join(rows), duration_ms=600000)
        log_event(
            "overlay_open",
            overlay=self.playback_overlay,
            source_kind=self.playback_source.kind.value if self.playback_source else "unknown",
            video_codec=video_codec,
            audio_codec=audio_codec,
            video_resolution=video_resolution,
            tv_resolution=tv_resolution,
            video_fps=video_fps,
            tv_hz=tv_hz,
            interpolation_type=interpolation_type,
        )

    def _close_overlay(self):
        if not self.playback:
            self._reset_playback_overlay_state()
            return
        resume_after_audio_prompt = bool(self.playback_audio_prompt_resume_after)
        try:
            self.playback.clear_overlays()
        except Exception as exc:
            log_event("overlay_clear_failed", error=str(exc))
        self._reset_playback_overlay_state()
        if resume_after_audio_prompt and self.playback:
            try:
                self.playback.set_pause(False)
            except Exception as exc:
                log_event("audio_prompt_resume_failed", error=str(exc))
        log_event("overlay_close")

    def _execute_overlay_action(self, action_id: str) -> bool:
        if not self.playback:
            return False
        source_kind = self.playback_source.kind.value if self.playback_source else "unknown"
        try:
            if action_id == OVERLAY_ACTION_TOGGLE_PAUSE:
                paused = not self.playback.pause_state()
                self.playback.set_pause(paused)
                log_event("playback_pause", paused=paused, via="overlay_menu")
            elif action_id == OVERLAY_ACTION_DVD_MENU:
                self.playback.go_to_dvd_menu()
            elif action_id == OVERLAY_ACTION_CHAPTER_PREV:
                self.playback.step_chapter(-1)
            elif action_id == OVERLAY_ACTION_CHAPTER_NEXT:
                self.playback.step_chapter(1)
            elif action_id == OVERLAY_ACTION_AUDIO_TRACKS:
                self._open_audio_overlay()
            elif action_id == OVERLAY_ACTION_SUBTITLES:
                self._open_subtitle_overlay()
            elif action_id == OVERLAY_ACTION_INFORMATION:
                self._open_information_overlay()
            elif action_id == OVERLAY_ACTION_RETURN_TO_BROWSER:
                self.stop_playback("Returned to browser")
            else:
                raise RuntimeError(f"unknown overlay action: {action_id}")
            log_event("overlay_action_ok", action_id=action_id, source_kind=source_kind)
            return True
        except Exception as exc:
            log_event("overlay_action_failed", action_id=action_id, source_kind=source_kind, error=str(exc))
            return False

    def _handle_playback_overlay_action(self, action: Action):
        if not self.playback:
            return
        if action in {Action.BACK, Action.SELECT, Action.START}:
            self._close_overlay()
            return
        if self.playback_overlay == "start_menu":
            if action == Action.UP and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus - 1) % len(self.playback_overlay_items)
                self.playback.show_start_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="start_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.DOWN and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus + 1) % len(self.playback_overlay_items)
                self.playback.show_start_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="start_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.ACCEPT:
                choice = self.playback_overlay_items[self.playback_overlay_focus] if self.playback_overlay_items else ""
                action_id = self.playback_overlay_actions[self.playback_overlay_focus] if self.playback_overlay_actions else ""
                log_event(
                    "overlay_accept",
                    overlay="start_menu",
                    focus=self.playback_overlay_focus,
                    choice=choice,
                    action_id=action_id,
                )
                if not action_id:
                    self._close_overlay()
                    return
                self._execute_overlay_action(action_id)
                if action_id == OVERLAY_ACTION_RETURN_TO_BROWSER:
                    return
                if action_id in {OVERLAY_ACTION_AUDIO_TRACKS, OVERLAY_ACTION_SUBTITLES, OVERLAY_ACTION_INFORMATION}:
                    return
                self._close_overlay()
                return
        elif self.playback_overlay == "audio_menu":
            if action == Action.UP and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus - 1) % len(self.playback_overlay_items)
                self.playback.show_audio_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="audio_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.DOWN and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus + 1) % len(self.playback_overlay_items)
                self.playback.show_audio_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="audio_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.ACCEPT:
                action_id = self.playback_overlay_actions[self.playback_overlay_focus] if self.playback_overlay_actions else ""
                source_kind = self.playback_source.kind.value if self.playback_source else "unknown"
                try:
                    if not action_id.startswith(OVERLAY_ACTION_AUDIO_TRACK_PREFIX):
                        raise RuntimeError(f"unknown audio action: {action_id}")
                    track_id = int(action_id.split(":", 1)[1])
                    track = {"id": track_id, "label": "", "lang": ""}
                    try:
                        for candidate in self.playback.audio_tracks():
                            candidate_id = candidate.get("id")
                            if isinstance(candidate_id, (int, float)) and int(candidate_id) == track_id:
                                track = candidate
                                break
                    except Exception as exc:
                        log_event("audio_track_metadata_failed", error=str(exc))
                    self._select_audio_track(track, remember=True, via="overlay")
                    log_event("overlay_action_ok", action_id=action_id, source_kind=source_kind)
                except Exception as exc:
                    self.message = MessageBox("AUDIO", "Audio change failed")
                    log_event("overlay_action_failed", action_id=action_id, source_kind=source_kind, error=str(exc))
                self._close_overlay()
                return
        elif self.playback_overlay == "subtitle_menu":
            if action == Action.UP and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus - 1) % len(self.playback_overlay_items)
                self.playback.show_subtitle_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="subtitle_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.DOWN and self.playback_overlay_items:
                self.playback_overlay_focus = (self.playback_overlay_focus + 1) % len(self.playback_overlay_items)
                self.playback.show_subtitle_menu_overlay(self.playback_overlay_focus, self.playback_overlay_items)
                log_event("overlay_focus", overlay="subtitle_menu", focus=self.playback_overlay_focus)
                return
            if action == Action.ACCEPT:
                action_id = self.playback_overlay_actions[self.playback_overlay_focus] if self.playback_overlay_actions else ""
                source_kind = self.playback_source.kind.value if self.playback_source else "unknown"
                try:
                    if action_id == OVERLAY_ACTION_SUBTITLE_OFF:
                        self.playback.set_subtitle_track(-1)
                        self.playback_state.prefs.subtitles_enabled = False
                        self.playback_state.write_prefs()
                    elif action_id.startswith(OVERLAY_ACTION_SUBTITLE_TRACK_PREFIX):
                        track_id = int(action_id.split(":", 1)[1])
                        self.playback.set_subtitle_track(track_id)
                        self.playback_state.prefs.subtitles_enabled = True
                        self.playback_state.write_prefs()
                    else:
                        raise RuntimeError(f"unknown subtitle action: {action_id}")
                    log_event("overlay_action_ok", action_id=action_id, source_kind=source_kind)
                except Exception as exc:
                    self.message = MessageBox("SUBTITLES", "Subtitle change failed")
                    log_event("overlay_action_failed", action_id=action_id, source_kind=source_kind, error=str(exc))
                self._close_overlay()
                return
        elif self.playback_overlay == "seek":
            if action == Action.LEFT:
                self.playback.seek_relative(-30)
                self.playback.show_seek_overlay(paused=self.playback.pause_state(), step_seconds=30)
                log_event("playback_seek", relative=-30, via="overlay")
                return
            if action == Action.RIGHT:
                self.playback.seek_relative(30)
                self.playback.show_seek_overlay(paused=self.playback.pause_state(), step_seconds=30)
                log_event("playback_seek", relative=30, via="overlay")
                return
            if action == Action.ACCEPT:
                paused = not self.playback.pause_state()
                self.playback.set_pause(paused)
                self.playback.show_seek_overlay(paused=paused, step_seconds=30)
                log_event("playback_pause", paused=paused, via="overlay")
                return
        elif self.playback_overlay == "information":
            if action == Action.ACCEPT:
                self._close_overlay()
                return

    def handle_remote_play_json(self, payload: object):
        if not isinstance(payload, dict):
            log_event("remote_play_json_ignored", reason="payload-not-dict")
            return
        uri = payload.get("url") or payload.get("uri") or payload.get("path")
        if not isinstance(uri, str) or not uri.strip():
            log_event("remote_play_json_ignored", reason="missing-uri")
            return
        title = payload.get("title")
        subtitle = payload.get("subtitle")
        kind_name = str(payload.get("kind", "plex_video")).strip().lower()

        kind_map = {
            "video_file": PlaybackKind.VIDEO_FILE,
            "dvd_folder": PlaybackKind.DVD_FOLDER,
            "dvd_iso": PlaybackKind.DVD_ISO,
            "optical_drive": PlaybackKind.OPTICAL_DRIVE,
            "plex_video": PlaybackKind.PLEX_VIDEO,
            "youtube_video": PlaybackKind.YOUTUBE_VIDEO,
        }
        kind = kind_map.get(kind_name, PlaybackKind.PLEX_VIDEO)
        authored_dvd = bool(payload.get("authored_dvd", kind in {PlaybackKind.DVD_FOLDER, PlaybackKind.DVD_ISO, PlaybackKind.OPTICAL_DRIVE}))
        hint_width_raw = payload.get("hint_width")
        hint_height_raw = payload.get("hint_height")
        hint_fps_raw = payload.get("hint_fps")
        source = PlaybackSource(
            title=str(title) if isinstance(title, str) and title.strip() else Path(uri).name or "Remote video",
            kind=kind,
            uri=uri,
            subtitle=str(subtitle) if isinstance(subtitle, str) else "Remote play request",
            authored_dvd=authored_dvd,
            container=Path(uri).suffix.lower().lstrip(".") or None,
            hint_width=int(hint_width_raw) if isinstance(hint_width_raw, (int, float)) else None,
            hint_height=int(hint_height_raw) if isinstance(hint_height_raw, (int, float)) else None,
            hint_fps=float(hint_fps_raw) if isinstance(hint_fps_raw, (int, float)) else None,
        )
        log_event("remote_play_json", uri=uri, kind=source.kind.value, title=source.title)
        self.start_playback(source, resume_prompt=False)

    def activate_play_dvd(self):
        self.refresh_sources()
        if not self.dvd_candidates:
            self.message = MessageBox("PLAY DVD", "No readable DVD, ISO, or VIDEO_TS source found.")
            log_event("play_dvd_empty")
            return
        if len(self.dvd_candidates) == 1:
            log_event("play_dvd_single", title=self.dvd_candidates[0].title)
            self.start_playback(self.dvd_candidates[0].source)
            return
        self.set_screen(Screen.DVD_PICKER, "PLAY DVD")
        self.dvd_selected = 0
        log_event("play_dvd_picker", count=len(self.dvd_candidates))

    def open_settings_menu(self):
        self.list_items = self._settings_items()
        self.list_selected = 0
        self.set_screen(Screen.LIST, "SETTINGS")
        self._log_list_selection()

    def open_browser_mode(self):
        self.list_items = [
            ListItem(title="LOCAL", subtitle="USB and local storage", kind="browser_local"),
            ListItem(title="NETWORK", subtitle="SMB and NFS", kind="browser_network"),
        ]
        self.list_selected = 0
        self.set_screen(Screen.LIST, "BROWSER")
        self._log_list_selection()

    def open_network_home(self):
        roots = self.network.list_saved_roots()
        self.list_items = [
            ListItem(title=r.display_name, subtitle=f"{r.root_name} {r.path}", kind="network_root", payload={"root": asdict(r)})
            for r in roots
        ]
        self.list_items.append(ListItem(title="ADD NETWORK", subtitle="Scan SMB or NFS", kind="network_add"))
        self.list_selected = 0
        self.set_screen(Screen.LIST, "NETWORK")
        self._log_list_selection()

    def open_network_add(self):
        self.list_items = [
            ListItem(title="NFS", subtitle="Scan for NFS", kind="scan_nfs"),
            ListItem(title="SMB", subtitle="Scan for SMB", kind="scan_smb"),
        ]
        self.list_selected = 0
        self.set_screen(Screen.LIST, "ADD NETWORK")
        self._log_list_selection()

    def _open_smb_auth_popup(self, host: dict):
        self.confirm_context = "smb_auth"
        self.confirm_options = [NETWORK_AUTH_GUEST, NETWORK_AUTH_LOGIN]
        self.confirm_selected = 0
        self.confirm_payload = {
            "host": dict(host),
            "return_section": self.section,
            "return_screen": getattr(getattr(self, "screen", None), "value", Screen.LIST.value),
        }
        self.set_screen(Screen.CONFIRM, "SMB AUTH")

    def _open_resume_popup(self, source: PlaybackSource, resume_seconds: float):
        self.confirm_context = "resume_playback"
        self.confirm_options = ["RESUME PLAYBACK", "START FROM BEGINNING"]
        self.confirm_selected = 0
        self.confirm_payload = {
            "source": source,
            "resume_seconds": float(resume_seconds),
            "return_section": self.section,
            "return_screen": self.screen.value,
        }
        self.set_screen(Screen.CONFIRM, "RESUME")

    def _close_confirm_popup(self):
        return_section = self.confirm_payload.get("return_section", self.section)
        return_screen_raw = str(self.confirm_payload.get("return_screen") or Screen.LIST.value)
        try:
            return_screen = Screen(return_screen_raw)
        except ValueError:
            return_screen = Screen.LIST
        self.confirm_context = None
        self.confirm_options = []
        self.confirm_selected = 0
        self.confirm_payload = {}
        self.set_screen(return_screen, return_section)

    def _open_keyboard_input(
        self,
        context: str,
        title: str,
        host: dict,
        initial: str,
        username: str,
        saved_password: str,
    ):
        self.keyboard_context = context
        self.keyboard_title = title
        self.keyboard_host = dict(host)
        self.keyboard_value = initial or ""
        self.keyboard_username = username
        self.keyboard_saved_password = saved_password
        self.keyboard_selected = 1
        self.keyboard_letter_index = 0
        self.keyboard_number_index = 0
        self.keyboard_symbol_index = 0
        self.set_screen(Screen.KEYBOARD, title)

    def _close_keyboard_input(self):
        self.keyboard_context = None
        self.keyboard_title = ""
        self.keyboard_value = ""
        self.keyboard_host = {}
        self.keyboard_username = ""
        self.keyboard_saved_password = ""
        self.keyboard_selected = 1
        self.set_screen(Screen.CONFIRM, "SMB AUTH")

    def _keyboard_shift(self, direction: int):
        if self.keyboard_selected == 1:
            self.keyboard_letter_index = (self.keyboard_letter_index + direction) % len(KEYBOARD_LETTERS)
            return
        if self.keyboard_selected == 2:
            self.keyboard_number_index = (self.keyboard_number_index + direction) % len(KEYBOARD_NUMBERS)
            return
        if self.keyboard_selected == 3:
            self.keyboard_symbol_index = (self.keyboard_symbol_index + direction) % len(KEYBOARD_SYMBOLS)

    def _submit_keyboard_value(self):
        context = self.keyboard_context
        host = dict(self.keyboard_host)
        value = self.keyboard_value
        if context == "smb_user":
            self._open_keyboard_input(
                context="smb_pass",
                title="SMB PASS",
                host=host,
                initial=self.keyboard_saved_password,
                username=value.strip(),
                saved_password="",
            )
            return
        if context == "smb_pass":
            username = self.keyboard_username.strip()
            password = value
            if username:
                self.network.save_credentials(
                    "SMB",
                    host.get("host", ""),
                    host.get("address", host.get("host", "")),
                    username,
                    password,
                )
            self._start_network_host_browse(host, username or None, password or None)
            return
        self._close_keyboard_input()

    def _keyboard_rows(self) -> list[tuple[str, str, bool]]:
        value = self.keyboard_value
        masked = "*" * len(value) if self.keyboard_context == "smb_pass" else value
        display = masked if masked else "(empty)"
        return [
            ("INPUT", display[-20:], True),
            ("LETTERS", f"[{KEYBOARD_LETTERS[self.keyboard_letter_index]}]", True),
            ("NUMBERS", f"[{KEYBOARD_NUMBERS[self.keyboard_number_index]}]", True),
            ("SYMBOLS", f"[{KEYBOARD_SYMBOLS[self.keyboard_symbol_index]}]", True),
            ("SPACE", "ADD SPACE", True),
            ("DONE", "B=DELETE", True),
        ]

    def _start_network_host_browse(self, host: dict, username: Optional[str], password: Optional[str]):
        protocol = host.get("protocol", "SMB")
        browse_host = dict(host)
        browse_host["username"] = username
        browse_host["password"] = password
        self.confirm_context = None
        self.confirm_options = []
        self.confirm_selected = 0
        self.confirm_payload = {}
        self.keyboard_context = None
        self.keyboard_title = ""
        self.keyboard_value = ""
        self.keyboard_host = {}
        self.keyboard_username = ""
        self.keyboard_saved_password = ""
        self._start_busy(
            "browse_host",
            f"OPENING {protocol}",
            Screen.LIST,
            f"{protocol} {host.get('display_name', host.get('host', 'HOST'))}",
        )
        thread = threading.Thread(
            target=self._browse_network_worker,
            args=({"kind": "host", "host": browse_host, "username": username, "password": password},),
            daemon=True,
        )
        thread.start()

    def _save_selected_network_favorite(self):
        if not self.list_items:
            return
        item = self.list_items[self.list_selected]
        if item.kind != "network_entry":
            return
        entry = item.payload.get("entry", {})
        if not entry.get("is_dir"):
            return
        host = item.payload.get("host", {})
        protocol = entry.get("protocol") or host.get("protocol") or "SMB"
        root_name = entry.get("root_name", "")
        path = entry.get("path", "/")
        username = host.get("username")
        password = host.get("password")
        saved = make_saved_root(
            protocol=protocol,
            display_name=f"{host.get('display_name', host.get('host', 'HOST'))} {root_name}",
            host=host.get("host", ""),
            address=host.get("address", host.get("host", "")),
            root_name=root_name,
            path=path,
            username=username,
            password=password,
        )
        self.network.add_root(saved)
        self.message = MessageBox("NETWORK", "SAVED AS FAV.")
        log_event("network_root_saved", protocol=saved.protocol, host=saved.host, root_name=saved.root_name, path=saved.path)

    def scan_network(self, protocol: str):
        self._start_busy(f"scan_{protocol.lower()}", f"SCANNING FOR {protocol}", Screen.LIST, f"SCAN {protocol}")
        thread = threading.Thread(target=self._scan_network_worker, args=(protocol,), daemon=True)
        thread.start()

    def _start_busy(self, context: str, label: str, return_screen: Screen, return_section: str):
        self.busy_context = context
        self.busy_label = label
        self.busy_started_at = time.time()
        self.busy_frame = 0
        self.busy_return_screen = return_screen
        self.busy_return_section = return_section
        self.set_screen(Screen.BUSY, "BUSY")

    def _scan_network_worker(self, protocol: str):
        try:
            hosts = self.network.discover_hosts(protocol)
            self.background_queue.put(("scan_network_done", protocol, hosts, None))
        except Exception as exc:
            self.background_queue.put(("scan_network_done", protocol, None, str(exc)))

    def _browse_network_worker(self, payload: dict):
        kind = payload.get("kind")
        try:
            if kind == "host":
                host = payload["host"]
                protocol = host.get("protocol", "SMB")
                username = payload.get("username", host.get("username"))
                password = payload.get("password", host.get("password"))
                if protocol == "SMB":
                    entries = self.network.browse_smb_root(host.get("host", ""), username, password)
                else:
                    entries = self.network.browse_nfs_root(host.get("host", ""))
                self.background_queue.put(("browse_network_done", payload, entries, None))
                return
            if kind == "root":
                root = payload["root"]
                protocol = root.get("protocol", "SMB")
                if protocol == "SMB":
                    entries = self.network.browse_smb_share(
                        root.get("host", ""),
                        root.get("root_name", ""),
                        root.get("path", "/"),
                        root.get("username"),
                        root.get("password"),
                    )
                else:
                    entries = self.network.browse_nfs_export(root.get("host", ""), root.get("root_name", ""), root.get("path", "/"))
                self.background_queue.put(("browse_network_done", payload, entries, None))
                return
            if kind == "entry":
                entry = payload["entry"]
                host = payload["host"]
                protocol = entry.get("protocol", host.get("protocol", "SMB"))
                root_name = entry.get("root_name", "")
                browse_path = entry.get("path", "/")
                username = host.get("username")
                password = host.get("password")
                if protocol == "SMB":
                    entries = self.network.browse_smb_share(host.get("host", ""), root_name, browse_path, username, password)
                else:
                    entries = self.network.browse_nfs_export(host.get("host", ""), root_name, browse_path)
                self.background_queue.put(("browse_network_done", payload, entries, None))
                return
        except Exception as exc:
            self.background_queue.put(("browse_network_done", payload, None, str(exc)))

    def _tick_background_work(self, now: float):
        if self.screen == Screen.BUSY and self.busy_context:
            elapsed = max(0.0, now - self.busy_started_at)
            self.busy_frame = int(elapsed / BUSY_ANIMATION_INTERVAL_SECS) % 4

        while True:
            try:
                event, payload, result, error = self.background_queue.get_nowait()
            except queue.Empty:
                break
            if event == "scan_network_done":
                self._finish_network_scan(payload, result, error)
            elif event == "browse_network_done":
                self._finish_network_browse(payload, result, error)
            elif event == "youtube_resolve_done":
                self._finish_youtube_resolve(payload, result, error)

    def _finish_network_scan(self, protocol: str, hosts, error: Optional[str]):
        self._clear_busy()
        if error:
            self.list_items = []
            self.list_selected = 0
            self.set_screen(Screen.LIST, f"SCAN {protocol}")
            self.message = MessageBox("NETWORK", f"{protocol} scan failed")
            log_event("network_scan_failed", protocol=protocol, error=error)
            return
        self.list_items = [
            ListItem(title=h.display_name, subtitle=f"{h.protocol} {h.address}", kind="host", payload={"host": asdict(h)})
            for h in (hosts or [])
        ]
        self.list_selected = 0
        self.set_screen(Screen.LIST, f"SCAN {protocol}")
        if not self.list_items:
            self.message = MessageBox("NETWORK", f"No {protocol} hosts found")
        self._log_list_selection()

    def _finish_network_browse(self, payload: dict, entries, error: Optional[str]):
        kind = payload.get("kind", "root")
        self._clear_busy()
        if error:
            self.list_items = []
            self.list_selected = 0
            self.set_screen(self.busy_return_screen, self.busy_return_section or "NETWORK")
            self.message = MessageBox("NETWORK", "Browse failed")
            log_event("network_browse_failed", kind=kind, error=error)
            return

        items = entries or []
        if kind == "host":
            host = payload["host"]
            protocol = host.get("protocol", "SMB")
            self.list_items = [
                ListItem(title=e.title, subtitle=e.subtitle, kind="network_entry", payload={"entry": asdict(e), "host": host})
                for e in items
            ]
            self.list_selected = 0
            self.set_screen(Screen.LIST, f"{protocol} {host.get('display_name', host.get('host', 'HOST'))}")
            if not self.list_items:
                self.message = MessageBox("NETWORK", f"No {protocol} shares found")
            else:
                self._log_list_selection()
            return

        if kind in {"root", "entry"}:
            root = payload.get("root")
            if not root:
                entry = payload["entry"]
                host = payload["host"]
                root = {
                    "protocol": entry.get("protocol", host.get("protocol", "SMB")),
                    "host": host.get("host", ""),
                    "display_name": host.get("display_name", host.get("host", "")),
                    "address": host.get("address", host.get("host", "")),
                    "root_name": entry.get("root_name", ""),
                    "path": entry.get("path", "/"),
                    "username": host.get("username"),
                    "password": host.get("password"),
                }
            protocol = root.get("protocol", "SMB")
            root_name = root.get("root_name", "")
            self.list_items = [
                ListItem(
                    title=e.title,
                    subtitle=e.subtitle,
                    kind="network_entry",
                    payload={
                        "entry": asdict(e),
                        "host": {
                            "host": root.get("host", ""),
                            "display_name": root.get("display_name", root.get("host", "")),
                            "protocol": protocol,
                            "address": root.get("address", root.get("host", "")),
                            "username": root.get("username"),
                            "password": root.get("password"),
                        },
                    },
                )
                for e in items
            ]
            self.list_selected = 0
            self.set_screen(Screen.LIST, f"{protocol} {root_name}")
            if not self.list_items:
                self.message = MessageBox("NETWORK", "Folder is empty")
            else:
                self._log_list_selection()

    def _clear_busy(self):
        self.busy_context = None
        self.busy_label = ""
        self.busy_started_at = 0.0
        self.busy_frame = 0

    def open_plex(self):
        if self.plex.has_token():
            try:
                nodes = self.plex.library_sections()
                if not nodes:
                    nodes = self.plex.cached_sections()
                self.list_items = [ListItem(title=n.title, subtitle=n.subtitle, kind="plex_node", payload={"node": asdict(n)}) for n in nodes]
                self.list_items.append(
                    ListItem(
                        title="EXIT TO RGB-PI",
                        subtitle="Close DVD player and return",
                        kind="rgbpi_exit",
                    )
                )
                self.list_selected = 0
                self.set_screen(Screen.LIST, "PLEX")
                self._log_list_selection()
            except Exception as exc:
                self.message = MessageBox("PLEX", f"Network error: {exc}")
                log_event("plex_open_failed", error=str(exc))
        else:
            self.status_line = "Press A/START"
            self.set_screen(Screen.PLEX_LINK, "PLEX LINK")

    def open_media_server_menu(self):
        youtube_state = self._youtube_state_obj().link_state
        if youtube_state == YOUTUBE_LINK_LINKED:
            youtube_subtitle = "Linked"
        elif youtube_state == YOUTUBE_LINK_CODE_PENDING:
            youtube_subtitle = "Code pending"
        else:
            youtube_subtitle = "Link with code"
        self.list_items = [
            ListItem(
                title="PLEX",
                subtitle="Open library" if self.plex.has_token() else "Link Plex",
                kind="media_server_plex",
            ),
            ListItem(
                title="YOUTUBE LINK",
                subtitle=youtube_subtitle,
                kind="media_server_youtube_link",
            ),
            ListItem(
                title="UNLINK YOUTUBE",
                subtitle="Reset pairing",
                kind="media_server_youtube_unlink",
            ),
        ]
        self.list_selected = 0
        self.set_screen(Screen.LIST, "MEDIA SERVER")
        self._log_list_selection()

    def activate_list_item(self, item: ListItem):
        if item.kind == "browser_local":
            self.list_items = [
                ListItem(title=p.name or str(p), subtitle=str(p), kind="local_root", path=str(p))
                for p in self.local_roots
            ]
            self.list_selected = 0
            self.section = "LOCAL"
            self._log_list_selection()
            return

        if item.kind == "browser_network":
            self.open_network_home()
            return

        if item.kind == "media_server_plex":
            self.open_plex()
            return

        if item.kind == "media_server_youtube_link":
            self.open_youtube_link()
            return

        if item.kind == "media_server_youtube_unlink":
            self.unlink_youtube()
            return

        if item.kind == "settings_crt_motion":
            self.toggle_crt_motion_mode()
            return

        if item.kind == "settings_cable_smooth_preset":
            self.apply_cable_smooth_preset()
            return

        if item.kind == "settings_resume_playback":
            self.resume_last_playback()
            return

        if item.kind == "settings_volume_normalization":
            self.cycle_volume_normalization()
            return

        if item.kind == "settings_force_43":
            self.toggle_force_43()
            return

        if item.kind == "settings_reset_plex_link":
            self.reset_plex_link()
            return

        if item.kind == "settings_runtime_install":
            self.install_runtime_dependencies()
            return

        if item.kind == "settings_youtube_link":
            self.open_youtube_link()
            return

        if item.kind == "settings_unlink_youtube":
            self.unlink_youtube()
            return

        if item.kind in {"local_root", "dir", "parent"} and item.path:
            path = Path(item.path)
            entries = scan_local_items(path)
            self.list_items = [
                ListItem(title=e["title"], subtitle=e["subtitle"], kind=e["kind"], path=e["path"])
                for e in entries
            ]
            self.list_selected = 0
            self.section = f"LOCAL {path.name or str(path)}"
            self._log_list_selection()
            return

        if item.kind == "video" and item.path:
            p = Path(item.path)
            self.start_playback(
                PlaybackSource(
                    title=p.name,
                    kind=PlaybackKind.VIDEO_FILE,
                    uri=str(p),
                    subtitle=str(p),
                    authored_dvd=False,
                    file_size=p.stat().st_size if p.exists() else None,
                    container=(p.suffix.lower().lstrip(".") or None),
                )
            )
            return

        if item.kind in {"dvd_folder", "iso"} and item.path:
            p = Path(item.path)
            self.start_playback(
                PlaybackSource(
                    title=p.name,
                    kind=PlaybackKind.DVD_ISO if item.kind == "iso" else PlaybackKind.DVD_FOLDER,
                    uri=str(p),
                    subtitle=str(p),
                    authored_dvd=True,
                    file_size=p.stat().st_size if p.exists() and p.is_file() else None,
                    container="iso" if item.kind == "iso" else "dvd",
                )
            )
            return

        if item.kind == "network_add":
            self.open_network_add()
            return

        if item.kind in {"scan_smb", "scan_nfs"}:
            protocol = "SMB" if item.kind == "scan_smb" else "NFS"
            self.scan_network(protocol)
            return

        if item.kind == "host":
            host = item.payload.get("host", {})
            protocol = host.get("protocol", "SMB")
            if protocol == "SMB":
                self._open_smb_auth_popup(host)
            else:
                self._start_network_host_browse(host, None, None)
            return

        if item.kind == "network_entry":
            entry = item.payload.get("entry", {})
            host = item.payload.get("host", {})
            protocol = entry.get("protocol") or host.get("protocol")
            if entry.get("is_dir"):
                title = entry.get("title", "Folder")
                self._start_busy("browse_entry", f"Opening {title}", Screen.LIST, f"{protocol} {entry.get('root_name', '')}")
                thread = threading.Thread(target=self._browse_network_worker, args=({"kind": "entry", "entry": entry, "host": host},), daemon=True)
                thread.start()
            else:
                media_path = self.network.resolve_media_path(
                    protocol or "",
                    host.get("host", ""),
                    entry.get("root_name", ""),
                    entry.get("path", "/"),
                    host.get("username"),
                    host.get("password"),
                )
                if media_path:
                    log_event("network_playback_path", title=entry.get("title", "Network Video"), path=media_path, protocol=protocol)
                    self.start_playback(
                        PlaybackSource(
                            title=entry.get("title", "Network Video"),
                            kind=PlaybackKind.VIDEO_FILE,
                            uri=media_path,
                            subtitle=f"{host.get('display_name', protocol or 'NETWORK')} {entry.get('subtitle','')}",
                            authored_dvd=False,
                            container=Path(media_path).suffix.lower().lstrip(".") or None,
                        )
                    )
                else:
                    log_event("network_playback_path_missing", title=entry.get("title", "Network Video"), protocol=protocol, entry_path=entry.get("path", "/"))
                    self.message = MessageBox("NETWORK", "Could not open file")
            return

        if item.kind == "network_root":
            root = item.payload.get("root", {})
            protocol = root.get("protocol", "SMB")
            self._start_busy("browse_root", f"Opening {protocol}", Screen.LIST, f"{protocol} {root.get('root_name', '')}")
            thread = threading.Thread(target=self._browse_network_worker, args=({"kind": "root", "root": root},), daemon=True)
            thread.start()
            return

        if item.kind == "plex_node":
            node = item.payload.get("node", {})
            kind = node.get("kind")
            if kind in {"section", "directory"}:
                nodes = self.plex.browse_path(node.get("key", ""))
                self.list_items = [ListItem(title=n.title, subtitle=n.subtitle, kind="plex_node", payload={"node": asdict(n)}) for n in nodes]
                self.list_items.append(
                    ListItem(
                        title="EXIT TO RGB-PI",
                        subtitle="Close DVD player and return",
                        kind="rgbpi_exit",
                    )
                )
                self.list_selected = 0
                self.section = f"PLEX {node.get('title','')}"
                self._log_list_selection()
            elif kind == "video":
                from dvdplayer_python.media.plex_client import PlexNode

                url = self.plex.resolve_playback_url(PlexNode(**node))
                self.start_playback(
                    PlaybackSource(
                        title=node.get("title", "Plex Video"),
                        kind=PlaybackKind.PLEX_VIDEO,
                        uri=url,
                        subtitle=node.get("subtitle", "Plex"),
                        authored_dvd=False,
                        container=node.get("container"),
                    )
                )
            return

        if item.kind == "rgbpi_exit":
            log_event("rgbpi_exit_requested", screen=self.screen.value, section=self.section)
            self.running = False

    def start_playback(
        self,
        source: PlaybackSource,
        *,
        resume_prompt: bool = True,
        resume_seconds: Optional[float] = None,
        start_from_beginning: bool = False,
    ):
        bookmark_key = self._bookmark_key(source)
        resume_bookmark = self.playback_state.bookmark(bookmark_key)
        if (
            resume_prompt
            and not start_from_beginning
            and resume_seconds is None
            and self._source_supports_resume(source)
            and resume_bookmark
            and resume_bookmark.position_seconds >= RESUME_MIN_SECONDS
        ):
            self._open_resume_popup(source, resume_bookmark.position_seconds)
            return
        if self.playback:
            previous = self.playback_source
            log_event(
                "playback_replace",
                old_title=previous.title if previous else None,
                old_kind=previous.kind.value if previous else None,
                new_title=source.title,
                new_kind=source.kind.value,
            )
            self._force_playback_cleanup("start_new_playback")
        log_event("playback_start", title=source.title, uri=source.uri, kind=source.kind.value)
        if self.screen == Screen.LIST:
            self.return_screen_after_playback = Screen.LIST
            self.return_section_after_playback = self.section
            self.return_list_items = list(self.list_items)
            self.return_list_selected = self.list_selected
        else:
            self.return_screen_after_playback = Screen.HOME
            self.return_section_after_playback = "HOME"
            self.return_list_items = []
            self.return_list_selected = 0
        try:
            self.playback = PlaybackSession.start(self.app_dir, source, self._playback_prefs_for_session())
        except Exception as exc:
            self.message = MessageBox("PLAYBACK", f"Playback failed: {exc}")
            if self.return_screen_after_playback == Screen.LIST:
                self.list_items = list(self.return_list_items)
                self.list_selected = self.return_list_selected
                self.set_screen(Screen.LIST, self.return_section_after_playback)
            else:
                self.set_screen(Screen.HOME, "HOME")
            log_event("playback_start_failed", error=str(exc))
            return
        self.playback_source = source
        self._reset_playback_overlay_state()
        self.playback_bookmark_key = bookmark_key
        self.set_screen(Screen.PLAYBACK, "PLAYBACK")
        self.status_line = "Playback active"
        if start_from_beginning:
            self.playback_state.clear_bookmark(bookmark_key)
            if self._source_supports_resume(source):
                self.playback_state.clear_last_played()

        effective_resume_seconds: Optional[float] = None
        if resume_seconds is not None and resume_seconds >= RESUME_MIN_SECONDS:
            effective_resume_seconds = resume_seconds
        elif not start_from_beginning and resume_bookmark and resume_bookmark.position_seconds >= RESUME_MIN_SECONDS:
            effective_resume_seconds = resume_bookmark.position_seconds
        if effective_resume_seconds is not None:
            try:
                self.playback.seek_absolute(effective_resume_seconds)
                self.status_line = f"Resumed at {fmt_duration(effective_resume_seconds)}"
                log_event("playback_resumed", seconds=effective_resume_seconds)
            except Exception as exc:
                log_event("playback_resume_failed", error=str(exc))

        try:
            self.playback.set_volume(self.playback_state.prefs.volume)
        except Exception:
            pass
        self._apply_or_prompt_audio_track()

    def stop_playback(self, status: str):
        self.persist_bookmark(force=True)
        if self.playback:
            self.playback.quit()
        self.playback = None
        self.playback_source = None
        self._reset_playback_overlay_state()
        self.playback_bookmark_key = None
        if self.return_screen_after_playback == Screen.LIST and self.return_list_items:
            self.list_items = list(self.return_list_items)
            self.list_selected = min(self.return_list_selected, max(0, len(self.list_items) - 1))
            self.set_screen(Screen.LIST, self.return_section_after_playback or "BROWSER")
        else:
            self.set_screen(Screen.HOME, "HOME")
        self.status_line = status
        self.refresh_sources()
        try:
            self._draw()
        except Exception:
            pass
        log_event("playback_stop", status=status)

    def persist_bookmark(self, force: bool):
        if not self.playback or not self.playback_source or not self.playback_bookmark_key:
            return
        try:
            pos = self.playback.current_time()
            dur = self.playback.duration()
        except Exception as exc:
            log_event("bookmark_read_failed", error=str(exc))
            return
        if not force and time.time() - self.last_bookmark_save < BOOKMARK_SAVE_INTERVAL:
            return
        if dur and dur > 0 and (dur - pos) <= RESUME_CLEAR_THRESHOLD:
            self.playback_state.clear_bookmark(self.playback_bookmark_key)
            if self._source_supports_resume(self.playback_source):
                self.playback_state.clear_last_played()
            log_event("bookmark_clear", key=self.playback_bookmark_key)
            return
        if pos >= RESUME_MIN_SECONDS:
            self.playback_state.save_bookmark(
                self.playback_bookmark_key,
                self.playback_source.title,
                self.playback_source.uri,
                pos,
                dur,
                _now_ms(),
            )
            if self._source_supports_resume(self.playback_source):
                self.playback_state.save_last_played(
                    self.playback_source,
                    pos,
                    dur,
                    _now_ms(),
                )
            log_event("bookmark_save", key=self.playback_bookmark_key, pos=pos, dur=dur)

    def _source_supports_resume(self, source: PlaybackSource) -> bool:
        return source.kind in {PlaybackKind.VIDEO_FILE, PlaybackKind.PLEX_VIDEO, PlaybackKind.YOUTUBE_VIDEO} and not source.authored_dvd

    def _draw(self):
        if self.screen == Screen.HOME:
            rows = [self._home_row(i) for i in range(HOME_MENU_SIZE)]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section="HOME",
                footer="A OPEN   B BACK   START+SELECT EXIT",
                rows=rows,
                selected=self.home_selected,
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.BUSY:
            dots = "." * self.busy_frame
            rows = [
                (self.busy_label or "Working", dots or ".", True),
                ("PLEASE WAIT", "NETWORK DISCOVERY", True),
            ]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section=self.section or "BUSY",
                footer=self.busy_label or "SCANNING...",
                rows=rows,
                selected=0,
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.CONFIRM:
            rows = [(option, "Select", True) for option in self.confirm_options]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section=self.section or "CONFIRM",
                footer="A SELECT   B BACK",
                rows=rows,
                selected=min(self.confirm_selected, max(0, len(rows) - 1)),
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.KEYBOARD:
            rows = self._keyboard_rows()
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section=self.keyboard_title or "KEYBOARD",
                footer="L/R CHAR  A ADD  START DONE  B DEL",
                rows=rows,
                selected=min(self.keyboard_selected, max(0, len(rows) - 1)),
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.DVD_PICKER:
            visible_candidates = self.dvd_candidates[:VISIBLE_LIST_ROWS]
            rows = [(c.title, c.subtitle, True) for c in visible_candidates]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section="PLAY DVD",
                footer="A PLAY   B BACK",
                rows=rows,
                selected=min(self.dvd_selected, max(0, len(rows) - 1)),
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.PLEX_CODE:
            rows = [
                ("Go to", "plex.tv/link", True),
                ("Enter code", self.plex_link_code or "....", True),
                ("Then wait", "Link will finish automatically", True),
            ]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section="PLEX CODE",
                footer="B BACK   START+SELECT EXIT",
                rows=rows,
                selected=1,
                message_title=None,
                message_body=None,
            )
            self.renderer.draw_model(model)
            return

        if self.screen == Screen.YOUTUBE_LINK:
            youtube_state = self._youtube_state_obj()
            code = youtube_state.code or "...."
            link_state = youtube_state.link_state
            if link_state == YOUTUBE_LINK_LINKED:
                status = "LINKED"
            elif link_state == YOUTUBE_LINK_CODE_PENDING:
                status = "WAITING"
            else:
                status = "UNLINKED"
            rows = [
                ("LINK WITH TV CODE", code, True),
                ("STATUS", status, True),
                ("SCREEN", youtube_state.screen_name or "YouTube on RGBPI", True),
            ]
            model = RenderModel(
                title="DVD MEDIAPLAYER",
                section="YOUTUBE LINK",
                footer="A REFRESH   B BACK",
                rows=rows,
                selected=0,
                message_title=self.message.title if self.message else None,
                message_body=self.message.body if self.message else None,
            )
            self.renderer.draw_model(model)
            return

        section = self.section
        rows = []
        if self.screen == Screen.PLAYBACK and self.playback_source:
            rows = [
                ("Playback Active", self.playback_source.title, True),
                ("Source", self.playback_source.subtitle, True),
                ("Mode", self.playback.display_mode_badge_text() if self.playback else "CRT", True),
            ]
            footer = "START MENU   SELECT RETURN   B STOP"
            selected = 0
        elif self.screen == Screen.PLEX_LINK:
            rows = [("Link Plex", self.status_line or "Press A to request code", True)]
            footer = "A CODE   B BACK   START+SELECT EXIT"
            selected = 0
        else:
            visible_items, visible_selected = self._visible_list_window(self.list_items, self.list_selected)
            rows = [(item.title, item.subtitle, True) for item in visible_items]
            footer = "A OPEN   B BACK"
            if self.section == "SETTINGS":
                footer = "L/R CHANGE   A OPEN   B BACK"
            if any(item.kind == "network_entry" for item in self.list_items):
                footer = "A OPEN   X SAVE FAV   B BACK"
            selected = visible_selected

        model = RenderModel(
            title="DVD MEDIAPLAYER",
            section=section,
            footer=footer,
            rows=rows,
            selected=selected,
            message_title=self.message.title if self.message else None,
            message_body=self.message.body if self.message else None,
        )
        self.renderer.draw_model(model)

    def _home_row(self, idx: int):
        if idx == 0:
            if not self.dvd_candidates:
                return ("PLAY DVD", "Insert DVD/ISO/VIDEO_TS source", False)
            if len(self.dvd_candidates) == 1:
                return ("PLAY DVD", "Disc ready", True)
            return ("PLAY DVD", f"{len(self.dvd_candidates)} DVD sources found", True)
        if idx == 1:
            return ("MEDIA LIBRARY", "Files and network", True)
        if idx == 2:
            if self._youtube_state_obj().link_state == YOUTUBE_LINK_LINKED:
                server_subtitle = "Plex + YouTube linked"
            elif self.plex.has_token():
                server_subtitle = "Plex ready"
            else:
                server_subtitle = "Plex / YouTube"
            return ("MEDIA SERVER", server_subtitle, True)
        if idx == 3:
            return ("SETTINGS", self._crt_motion_subtitle(), True)
        last = self.playback_state.last_played
        if last and last.position_seconds >= RESUME_MIN_SECONDS:
            return ("RESUME PLAYBACK", f"{last.source.title} {fmt_duration(last.position_seconds)}", True)
        return ("RESUME PLAYBACK", "No resumable playback", False)

    def go_home(self):
        self.list_items = []
        self.list_selected = 0
        self.set_screen(Screen.HOME, "HOME")

    def _write_runtime_state(self):
        snapshot = self.runtime_snapshot()
        payload = json.dumps(asdict(snapshot), indent=2)
        targets = [
            Path(CONTROL_STATE_PATH),
            self.runtime_dir / "rgbpi-dvdplayer-state.json",
            Path(f"/tmp/rgbpi-dvdplayer-state.{os.getuid()}.json"),
        ]
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(target)
                return
            except Exception as exc:
                log_event("runtime_state_write_failed", path=str(target), error=str(exc))
                continue

    def runtime_snapshot(self) -> RuntimeSnapshot:
        selected_index = None
        item_count = None
        items = []
        if self.screen == Screen.HOME:
            selected_index = self.home_selected
            item_count = HOME_MENU_SIZE
            items = [self._home_row(i)[0] for i in range(HOME_MENU_SIZE)]
        elif self.screen == Screen.LIST:
            selected_index = self.list_selected
            item_count = len(self.list_items)
            items = [it.title for it in self.list_items]
        elif self.screen == Screen.DVD_PICKER:
            selected_index = self.dvd_selected
            item_count = len(self.dvd_candidates)
            items = [it.title for it in self.dvd_candidates]
        elif self.screen == Screen.CONFIRM:
            selected_index = self.confirm_selected
            item_count = len(self.confirm_options)
            items = list(self.confirm_options)
        elif self.screen == Screen.KEYBOARD:
            selected_index = self.keyboard_selected
            item_count = 6
            items = [row[0] for row in self._keyboard_rows()]

        playback_pos = None
        playback_dur = None
        playback_paused = None
        playback_speed = None
        volume = self.playback_state.prefs.volume
        audio_track_id = None
        subtitle_track_id = None
        ipc_path = None
        kind = None
        ptitle = None
        drm_mode_target = None
        drm_mode_effective = None
        drm_mode_label = None
        drm_connector = None
        playback_backend = None
        playback_profile = None
        playback_degraded = False

        if self.playback and self.playback_source:
            try:
                playback_pos = self.playback.current_time()
                playback_dur = self.playback.duration()
                playback_paused = self.playback.pause_state()
                playback_speed = self.playback.speed()
                volume = self.playback.volume()
                audio_track_id = self.playback.current_audio_track()
                subtitle_track_id = self.playback.current_subtitle_track()
            except Exception:
                pass
            ipc_path = str(self.playback.ipc_path)
            kind = self.playback_source.kind.value
            ptitle = self.playback_source.title
            drm_mode_target = self.playback.target_mode
            drm_mode_effective = self.playback.effective_mode
            drm_mode_label = self.playback.display_mode_badge_text()
            drm_connector = self.playback.drm_target.connector if self.playback.drm_target else None
            playback_backend = self.playback.backend
            playback_profile = self.playback.backend_profile
            playback_degraded = self.playback.degraded

        bookmark_seconds = None
        if self.playback_bookmark_key:
            bookmark = self.playback_state.bookmark(self.playback_bookmark_key)
            if bookmark:
                bookmark_seconds = bookmark.position_seconds

        youtube_state = self._youtube_state_obj()
        youtube_queue = getattr(self, "youtube_queue", [])
        return RuntimeSnapshot(
            pid=os.getpid(),
            started_at_unix_ms=self.started_at_ms,
            updated_at_unix_ms=_now_ms(),
            control_socket=self.control.endpoint,
            screen=self.screen.value,
            title="DVD MEDIAPLAYER",
            section=self.section,
            status_line=self.status_line,
            screensaver=self.screensaver,
            message_title=self.message.title if self.message else None,
            message_body=self.message.body if self.message else None,
            selected_index=selected_index,
            item_count=item_count,
            overlay=self.playback_overlay,
            playback_ipc_path=ipc_path,
            playback_kind=kind,
            playback_title=ptitle,
            playback_position_seconds=playback_pos,
            playback_duration_seconds=playback_dur,
            playback_paused=playback_paused,
            playback_speed=playback_speed,
            bookmark_seconds=bookmark_seconds,
            volume=volume,
            audio_track_id=audio_track_id,
            subtitle_track_id=subtitle_track_id,
            drm_mode_target=drm_mode_target,
            drm_mode_effective=drm_mode_effective,
            drm_mode_label=drm_mode_label,
            drm_connector=drm_connector,
            playback_backend=playback_backend,
            playback_profile=playback_profile,
            playback_degraded=playback_degraded,
            youtube_link_state=youtube_state.link_state,
            youtube_screen_name=youtube_state.screen_name,
            youtube_queue_size=max(youtube_state.queue_size, len(youtube_queue)),
            youtube_receiver_healthy=bool(youtube_state.receiver_healthy),
            overlay_focus=self.playback_overlay_focus if self.playback_overlay in {"start_menu", "seek", "audio_menu", "subtitle_menu"} else None,
            overlay_items=list(self.playback_overlay_items),
            active_tty=self.active_tty,
            items=items,
        )

    def toggle_crt_motion_mode(self):
        order = ["authentic", "smooth_tv", "cable_smooth"]
        current = str(self.playback_state.prefs.motion_mode or "smooth_tv").lower()
        try:
            idx = order.index(current)
        except ValueError:
            idx = 1
        next_mode = order[(idx + 1) % len(order)]
        self.playback_state.prefs.motion_mode = next_mode
        self.playback_state.write_prefs()
        self.status_line = f"CRT motion set to {self._crt_motion_label(next_mode)}"
        self._refresh_settings_items()
        self.message = MessageBox("SETTINGS", self._crt_motion_label(next_mode))
        log_event("settings_motion_mode", motion_mode=next_mode)

    def apply_cable_smooth_preset(self):
        self.playback_state.prefs.motion_mode = "cable_smooth"
        self.playback_state.prefs.deinterlace_mode = "bob"
        self.playback_state.prefs.volume_normalization = "light"
        self.playback_state.prefs.stereo_downmix = True
        self.playback_state.write_prefs()
        self.status_line = "Cable smooth preset active"
        self.list_items = self._settings_items()
        self.list_selected = 0
        self.message = MessageBox("SETTINGS", "CABLE SMOOTH ACTIVE")
        log_event(
            "settings_cable_smooth_preset",
            motion_mode=self.playback_state.prefs.motion_mode,
            deinterlace_mode=self.playback_state.prefs.deinterlace_mode,
            volume_normalization=self.playback_state.prefs.volume_normalization,
        )

    def resume_last_playback(self, *, open_settings_on_missing: bool = True):
        last = self.playback_state.last_played
        if not last:
            self.message = MessageBox("RESUME", "No resumable playback")
            if open_settings_on_missing:
                self.open_settings_menu()
            return
        self.start_playback(
            last.source,
            resume_prompt=False,
            resume_seconds=last.position_seconds,
        )
        log_event(
            "settings_resume_playback",
            title=last.source.title,
            kind=last.source.kind.value,
            position_seconds=last.position_seconds,
        )

    def cycle_volume_normalization(self):
        current = str(self.playback_state.prefs.volume_normalization or "light").lower()
        order = ["off", "light", "high"]
        try:
            idx = order.index(current)
        except ValueError:
            idx = 1
        next_mode = order[(idx + 1) % len(order)]
        self.playback_state.prefs.volume_normalization = next_mode
        self.playback_state.write_prefs()
        self.status_line = f"Volume normalization {self._volume_normalization_label(next_mode)}"
        self._refresh_settings_items()
        self.message = MessageBox("SETTINGS", f"VOLUME {self._volume_normalization_label(next_mode)}")
        log_event("settings_volume_normalization", value=next_mode)

    def toggle_force_43(self):
        next_value = not bool(self.playback_state.prefs.force_43)
        self.playback_state.prefs.force_43 = next_value
        self.playback_state.write_prefs()
        self.status_line = f"Force 4:3 {'ON' if next_value else 'OFF'}"
        self._refresh_settings_items()
        self.message = MessageBox("SETTINGS", f"FORCE 4:3 {'ON' if next_value else 'OFF'}")
        log_event("settings_force_43", enabled=next_value)

    def toggle_deinterlace_mode(self):
        current = str(self.playback_state.prefs.deinterlace_mode or "weave").lower()
        next_mode = "bob" if current == "weave" else "weave"
        self.playback_state.prefs.deinterlace_mode = next_mode
        self.playback_state.write_prefs()
        self.status_line = f"Deinterlace {self._deinterlace_label(next_mode)}"
        self.list_items = self._settings_items()
        self.list_selected = 0
        self.message = MessageBox("SETTINGS", f"DEINTERLACE {self._deinterlace_label(next_mode)}")
        log_event("settings_deinterlace_mode", mode=next_mode)

    def reset_plex_link(self):
        self.plex.reset_link()
        self.status_line = "Plex link reset"
        self._refresh_settings_items()
        self.message = MessageBox("SETTINGS", "Plex link reset")
        log_event("settings_reset_plex_link")

    def install_runtime_dependencies(self):
        installer = self.app_dir / "runtime" / "check_runtime_bundle.sh"
        if not installer.exists():
            self.message = MessageBox("SETTINGS", "Runtime checker missing")
            log_event("settings_runtime_install_missing", path=str(installer))
            self.open_settings_menu()
            return
        self._start_busy("settings_install_runtime", "Checking runtime bundle", Screen.LIST, "SETTINGS")
        self._draw()
        try:
            proc = subprocess.run(
                [str(installer), "--check"],
                cwd=str(self.app_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            output = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 0:
                self.status_line = "Bundled runtime ready"
                self.message = MessageBox("SETTINGS", "Runtime bundle OK")
                log_event("settings_runtime_install_ok", output=output[-320:])
            else:
                self.message = MessageBox("SETTINGS", "Runtime bundle missing")
                log_event(
                    "settings_runtime_install_failed",
                    code=proc.returncode,
                    output=output[-600:],
                )
        except Exception as exc:
            self.message = MessageBox("SETTINGS", "Runtime bundle missing")
            log_event("settings_runtime_install_failed", error=str(exc))
        self._clear_busy()
        self.open_settings_menu()

    def _youtube_state_obj(self):
        manager = getattr(self, "youtube", None)
        state = getattr(manager, "state", None)
        if state is None:
            class _Fallback:
                link_state = YOUTUBE_LINK_UNLINKED
                code = ""
                screen_name = None
                queue_size = 0
                receiver_healthy = False
                receiver_version = None

            return _Fallback()
        return state

    def _crt_motion_label(self, value: str) -> str:
        if value == "cable_smooth":
            return "CABLE SMOOTH"
        return "SMOOTH TV" if value == "smooth_tv" else "AUTHENTIC"

    def _crt_motion_subtitle(self) -> str:
        return self._crt_motion_label(self.playback_state.prefs.motion_mode)

    def _default_mode_label(self, value: str) -> str:
        return "50HZ (576I)" if str(value).lower() == "50hz" else "60HZ (480I)"

    def _default_mode_subtitle(self) -> str:
        return self._default_mode_label(self.playback_state.prefs.default_mode)

    def _force_43_subtitle(self) -> str:
        return "ON" if self.playback_state.prefs.force_43 else "OFF"

    def _deinterlace_label(self, value: str) -> str:
        return "BOB" if str(value).lower() == "bob" else "WEAVE"

    def _deinterlace_subtitle(self) -> str:
        return self._deinterlace_label(self.playback_state.prefs.deinterlace_mode)

    def _volume_normalization_label(self, value: str) -> str:
        labels = {"off": "OFF", "light": "LIGHT", "high": "HIGH"}
        return labels.get(str(value).lower(), "LIGHT")

    def _volume_normalization_subtitle(self) -> str:
        return self._volume_normalization_label(self.playback_state.prefs.volume_normalization)

    def _switchable_settings_kinds(self) -> set[str]:
        return {
            "settings_crt_motion",
            "settings_default_mode",
            "settings_volume_normalization",
            "settings_force_43",
        }

    def _is_switchable_setting_item(self, item: ListItem) -> bool:
        return item.kind in self._switchable_settings_kinds()

    def _refresh_settings_items(self):
        selected_kind = None
        if self.list_items and 0 <= self.list_selected < len(self.list_items):
            selected_kind = self.list_items[self.list_selected].kind
        self.list_items = self._settings_items()
        if selected_kind:
            for idx, candidate in enumerate(self.list_items):
                if candidate.kind == selected_kind:
                    self.list_selected = idx
                    break
            else:
                self.list_selected = min(self.list_selected, max(0, len(self.list_items) - 1))
        else:
            self.list_selected = min(self.list_selected, max(0, len(self.list_items) - 1))

    def _adjust_switchable_setting(self, item: ListItem, action: Action) -> bool:
        if not self._is_switchable_setting_item(item):
            return False
        step = 1 if action == Action.RIGHT else -1
        if item.kind == "settings_crt_motion":
            order = ["authentic", "smooth_tv", "cable_smooth"]
            current = str(self.playback_state.prefs.motion_mode or "smooth_tv").lower()
            try:
                idx = order.index(current)
            except ValueError:
                idx = 1
            next_mode = order[(idx + step) % len(order)]
            self.playback_state.prefs.motion_mode = next_mode
            self.playback_state.write_prefs()
            self._refresh_settings_items()
            self.status_line = f"CRT motion set to {self._crt_motion_label(next_mode)}"
            self.message = MessageBox("SETTINGS", self._crt_motion_label(next_mode))
            log_event("settings_motion_mode", motion_mode=next_mode, via=action.value)
            return True
        if item.kind == "settings_default_mode":
            order = ["60hz", "50hz"]
            current = str(self.playback_state.prefs.default_mode or "60hz").lower()
            try:
                idx = order.index(current)
            except ValueError:
                idx = 0
            next_mode = order[(idx + step) % len(order)]
            self.playback_state.prefs.default_mode = next_mode
            self.playback_state.write_prefs()
            self._refresh_settings_items()
            self.status_line = f"Default mode {self._default_mode_label(next_mode)}"
            self.message = MessageBox("SETTINGS", f"DEFAULT MODE {self._default_mode_label(next_mode)}")
            log_event("settings_default_mode", value=next_mode, via=action.value)
            return True
        if item.kind == "settings_volume_normalization":
            order = ["off", "light", "high"]
            current = str(self.playback_state.prefs.volume_normalization or "light").lower()
            try:
                idx = order.index(current)
            except ValueError:
                idx = 1
            next_mode = order[(idx + step) % len(order)]
            self.playback_state.prefs.volume_normalization = next_mode
            self.playback_state.write_prefs()
            self._refresh_settings_items()
            self.status_line = f"Volume normalization {self._volume_normalization_label(next_mode)}"
            self.message = MessageBox("SETTINGS", f"VOLUME {self._volume_normalization_label(next_mode)}")
            log_event("settings_volume_normalization", value=next_mode, via=action.value)
            return True
        if item.kind == "settings_force_43":
            next_value = action == Action.RIGHT
            self.playback_state.prefs.force_43 = next_value
            self.playback_state.write_prefs()
            self._refresh_settings_items()
            self.status_line = f"Force 4:3 {'ON' if next_value else 'OFF'}"
            self.message = MessageBox("SETTINGS", f"FORCE 4:3 {'ON' if next_value else 'OFF'}")
            log_event("settings_force_43", enabled=next_value, via=action.value)
            return True
        return False

    def _settings_items(self) -> list[ListItem]:
        return [
            ListItem(
                title="CRT MOTION",
                subtitle=self._crt_motion_subtitle(),
                kind="settings_crt_motion",
            ),
            ListItem(
                title="CABLE SMOOTH PRESET",
                subtitle="Apply 1999 cable profile",
                kind="settings_cable_smooth_preset",
            ),
            ListItem(
                title="DEFAULT MODE",
                subtitle=self._default_mode_subtitle(),
                kind="settings_default_mode",
            ),
            ListItem(
                title="VOLUME NORMALIZATION",
                subtitle=self._volume_normalization_subtitle(),
                kind="settings_volume_normalization",
            ),
            ListItem(
                title="FORCE 4:3",
                subtitle=self._force_43_subtitle(),
                kind="settings_force_43",
            ),
            ListItem(
                title="RESET PLEX LINK",
                subtitle="Require relink",
                kind="settings_reset_plex_link",
            ),
            ListItem(
                title="YOUTUBE LINK WITH CODE",
                subtitle="Start TV code pairing",
                kind="settings_youtube_link",
            ),
            ListItem(
                title="UNLINK YOUTUBE",
                subtitle="Reset TV pairing",
                kind="settings_unlink_youtube",
            ),
            ListItem(
                title="RUNTIME CHECK",
                subtitle="Bundled MPV + libdvdcss",
                kind="settings_runtime_install",
            ),
        ]

    def _flush_screenshots(self):
        if not self.pending_screenshots:
            return
        while self.pending_screenshots:
            path = self.pending_screenshots.pop(0)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                log_event("screenshot_failed", path=str(path), stage="mkdir", error=str(exc))
                continue
            if self.playback:
                try:
                    self.playback.screenshot_to_file(path)
                    log_event("screenshot_written", path=str(path), source="mpv")
                    continue
                except Exception:
                    log_event("screenshot_fallback_ui", path=str(path), source="mpv_failed")
            try:
                self.renderer.screenshot(str(path))
                log_event("screenshot_written", path=str(path), source="ui")
            except Exception as exc:
                log_event("screenshot_failed", path=str(path), stage="renderer", error=str(exc))

    def _bookmark_key(self, source: PlaybackSource) -> str:
        prefix = {
            PlaybackKind.VIDEO_FILE: "video",
            PlaybackKind.DVD_FOLDER: "dvd-folder",
            PlaybackKind.DVD_ISO: "dvd-iso",
            PlaybackKind.OPTICAL_DRIVE: "dvd-drive",
            PlaybackKind.PLEX_VIDEO: "plex",
            PlaybackKind.YOUTUBE_VIDEO: "youtube",
        }[source.kind]
        return f"{prefix}:{source.uri}"


def fmt_duration(seconds: float) -> str:
    value = max(0, int(round(seconds)))
    m, s = divmod(value, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02}:{m:02}:{s:02}"
    return f"{m:02}:{s:02}"


def _map_key(key: int) -> Optional[Action]:
    mapping = {
        pygame.K_UP: Action.UP,
        pygame.K_DOWN: Action.DOWN,
        pygame.K_LEFT: Action.LEFT,
        pygame.K_RIGHT: Action.RIGHT,
        pygame.K_RETURN: Action.ACCEPT,
        pygame.K_SPACE: Action.ACCEPT,
        pygame.K_ESCAPE: Action.BACK,
        pygame.K_b: Action.BACK,
        pygame.K_s: Action.START,
        pygame.K_TAB: Action.SELECT,
        pygame.K_BACKSPACE: Action.SELECT,
        pygame.K_x: Action.X,
        pygame.K_h: Action.HOME,
        pygame.K_q: Action.QUIT,
    }
    return mapping.get(key)


def _map_joystick_button(number: int) -> Optional[Action]:
    mapping = {
        0: Action.ACCEPT,  # A
        1: Action.BACK,  # B
        2: Action.X,  # X
        6: Action.SELECT,  # Back
        7: Action.START,  # Start
        8: Action.HOME,  # Guide
        9: Action.ACCEPT,  # L3
        10: Action.BACK,  # R3
    }
    return mapping.get(int(number))


def _map_joystick_axis(number: int) -> Optional[tuple[Action, Action]]:
    mapping = {
        0: (Action.LEFT, Action.RIGHT),  # left stick X
        1: (Action.UP, Action.DOWN),  # left stick Y
        6: (Action.LEFT, Action.RIGHT),  # dpad X (xbox/xpad)
        7: (Action.UP, Action.DOWN),  # dpad Y (xbox/xpad)
    }
    return mapping.get(int(number))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _detect_tty() -> Optional[str]:
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except Exception:
            continue
    try:
        import subprocess

        out = subprocess.check_output(["fgconsole"], stderr=subprocess.DEVNULL, text=True, timeout=0.5).strip()
        if out.isdigit():
            return f"/dev/tty{out}"
    except Exception:
        pass
    return os.environ.get("DVDPLAYER_ACTIVE_TTY")


def main() -> int:
    try:
        app = App()
    except RuntimeError as exc:
        log_event("app_start_skipped", reason=str(exc))
        return 0

    def _sigterm(_sig, _frm):
        log_event("app_stop_requested", reason="signal", signal=int(_sig))
        app.running = False

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGHUP, _sigterm)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
