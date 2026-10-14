"""Platform golden harness: record every platform-visible decision the app makes.

Runs the REAL app code (App + PlaybackSession) against fakes at the OS
boundary — a recording fake ``pygame``, fake ffprobe/ffmpeg answers, a fake
DRM connector and a fake mpv child — and prints a deterministic text report:

  * the full mpv command line + child environment for a matrix of sources
    (PAL/NTSC/film/HD/4K/DVD x jack/optical/prefs variants),
  * every display-mutating pygame call around each playback start/stop,
  * the IPC commands sent to mpv right after start,
  * the SETTINGS menu (labels/subtitles) and the AUDIO OUTPUT cycle,
  * the environment the launcher script hands to python.

One process = one platform (module-level env reads happen at import time).
``check.py`` runs this once per platform and diffs against the frozen
``*.golden.txt`` references. The harness only uses APIs that exist on every
branch it has to compare, so the same file can snapshot old and new code.

Usage: python3 harness.py <os4|replayos> <repo_root>
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

PLATFORM = sys.argv[1]
REPO = Path(sys.argv[2]).resolve()
OUT: list[str] = []
_REAL_POPEN = subprocess.Popen


def emit(line: str = "") -> None:
    OUT.append(line)


# --------------------------------------------------------------------------
# Environment: start from nothing, then apply exactly what the platform's
# launcher exports (copied from start_rgbpi_dvdplayer_python.sh and
# deploy/replayos/replay_launch.sh).
# --------------------------------------------------------------------------
for key in list(os.environ):
    if key.startswith(("DVDPLAYER_", "SDL_")) or key == "LD_LIBRARY_PATH":
        del os.environ[key]

TMP = Path(tempfile.mkdtemp(prefix="pg"))
APP = TMP / "app"
ROOT = TMP / "root"  # fake filesystem root for platform auto-detection
(APP / "state" / "runtime").mkdir(parents=True)
(APP / "bin").mkdir()
for tool in ("mpv", "ffprobe", "ffmpeg"):
    p = APP / "bin" / tool
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)

PLATFORM_ENV = {
    "os4": {
        "DVDPLAYER_DRM_CONNECTOR_EXPECTED": "VGA-1",
    },
    "replayos": {
        "SDL_VIDEODRIVER": "kmsdrm",
        "DVDPLAYER_DISPLAY_W": "2560",
        "DVDPLAYER_DISPLAY_H": "240",
        "DVDPLAYER_DRM_CONNECTOR": "HDMI-A-1",
        "DVDPLAYER_MPV_DRM_MODE": "2560x240",
        "DVDPLAYER_MPV_DRM_MODE_PAL": "2560x576",
        "DVDPLAYER_MPV_DRM_MODE_NTSC": "2560x480",
        "DVDPLAYER_RUNTIME_LIBS": "mpv-only",
        "DVDPLAYER_ALSA_DEVICE": "default:CARD=vc4hdmi0",
        "DVDPLAYER_MPV_LD_LIBRARY_PATH": "<RUNTIME_LIBS>",
        "DVDPLAYER_DRM_CONNECTOR_EXPECTED": "HDMI-A-1",
    },
}[PLATFORM]
EXPECTED_CONNECTOR = PLATFORM_ENV.pop("DVDPLAYER_DRM_CONNECTOR_EXPECTED")
os.environ.update(PLATFORM_ENV)
if PLATFORM == "os4":
    os.environ["LD_LIBRARY_PATH"] = "<RUNTIME_LIBS>:<APP>/lib"
os.environ.update(
    {
        "DVDPLAYER_APP_DIR": str(APP),
        "DVDPLAYER_MPV_BIN": str(APP / "bin" / "mpv"),
        "DVDPLAYER_FFPROBE_BIN": str(APP / "bin" / "ffprobe"),
        "DVDPLAYER_FFMPEG_BIN": str(APP / "bin" / "ffmpeg"),
        "DVDPLAYER_CONTROL_SOCKET": str(APP / "state" / "runtime" / "api.sock"),
        "DVDPLAYER_STATE_PATH": str(APP / "state" / "runtime" / "state.json"),
        "DVDPLAYER_DEBUG_LOG": str(APP / "state" / "runtime" / "debug.log"),
        "DVDPLAYER_MPV_LOG": str(APP / "state" / "runtime" / "mpv.log"),
        "PATH": str(APP / "bin"),
    }
)

# Fake root: the marker each OS really has (see core/platform.py).
if PLATFORM == "replayos":
    (ROOT / "opt" / "replay").mkdir(parents=True)
    (ROOT / "opt" / "replay" / "replay").write_text("")
else:
    (ROOT / "opt" / "rgbpi").mkdir(parents=True)


def norm(text: str) -> str:
    text = str(text).replace(str(APP), "<APP>").replace(str(TMP), "<TMP>")
    return re.sub(r"rgbpi-dvdplayer-ipc-\d+\.sock", "rgbpi-dvdplayer-ipc-<N>.sock", text)


# --------------------------------------------------------------------------
# Recording fake pygame
# --------------------------------------------------------------------------
UI_EVENTS: list[str] = []
UI_LOCK = threading.Lock()


def ui(event: str) -> None:
    with UI_LOCK:
        UI_EVENTS.append(event)


def take_ui() -> list[str]:
    with UI_LOCK:
        events = list(UI_EVENTS)
        UI_EVENTS.clear()
    # Collapse runs of identical frames ("flip" x30) to keep the report stable.
    out: list[str] = []
    for e in events:
        if out and out[-1].split(" x")[0] == e:
            n = int(out[-1].split(" x")[1]) + 1 if " x" in out[-1] else 2
            out[-1] = f"{e} x{n}"
        else:
            out.append(e)
    return out


class FakeRect:
    def __init__(self, x=0, y=0, w=0, h=0):
        self.x, self.y, self.w, self.h = x, y, w, h

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class FakeSurface:
    def __init__(self, size, kind="surface"):
        self._size = (int(size[0]), int(size[1]))
        self.kind = kind

    def fill(self, *a, **k):
        pass

    def blit(self, *a, **k):
        pass

    def get_size(self):
        return self._size

    def get_width(self):
        return self._size[0]

    def get_height(self):
        return self._size[1]

    def get_rect(self, **kw):
        r = FakeRect(0, 0, *self._size)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


class FakeFont:
    def __init__(self, size):
        self._px = int(size)

    def size(self, text):
        return (len(str(text)) * max(1, self._px // 2), self._px)

    def render(self, text, *a, **k):
        return FakeSurface(self.size(text), "text")

    def get_linesize(self):
        return self._px + 2

    def get_height(self):
        return self._px


def _build_fake_pygame() -> types.ModuleType:
    pg = types.ModuleType("pygame")
    state = {"display": False, "mixer": True}
    pg.FULLSCREEN = -2147483648
    pg.QUIT = 256
    pg.KEYDOWN = 768
    for i, name in enumerate(
        "BACKSPACE DOWN ESCAPE LEFT PAGEDOWN PAGEUP RETURN RIGHT SPACE TAB UP b h q s x".split()
    ):
        setattr(pg, f"K_{name}", 1000 + i)
    pg.Rect = FakeRect
    pg.Surface = lambda size, *a, **k: FakeSurface(size, "offscreen")

    def init():
        state["display"] = True
        ui("pygame.init")

    def quit_():
        state["display"] = False
        ui("pygame.quit")

    pg.init = init
    pg.quit = quit_

    display = types.ModuleType("pygame.display")

    def set_mode(size, flags=0, *a, **k):
        state["display"] = True
        ui(f"display.set_mode {tuple(size)} fullscreen={bool(flags & pg.FULLSCREEN)}")
        return FakeSurface(size, "display")

    def d_quit():
        state["display"] = False
        ui("display.quit")

    def d_init():
        state["display"] = True
        ui("display.init")

    display.set_mode = set_mode
    display.quit = d_quit
    display.init = d_init
    display.get_init = lambda: state["display"]
    display.flip = lambda: ui("display.flip")
    pg.display = display

    mixer = types.ModuleType("pygame.mixer")

    def m_quit():
        state["mixer"] = False
        ui("mixer.quit")

    mixer.quit = m_quit
    mixer.get_init = lambda: state["mixer"]
    pg.mixer = mixer

    font = types.ModuleType("pygame.font")
    font.Font = FakeFont
    font.SysFont = lambda name, size, *a, **k: FakeFont(size)
    pg.font = font

    draw = types.ModuleType("pygame.draw")
    draw.rect = lambda *a, **k: None
    pg.draw = draw

    transform = types.ModuleType("pygame.transform")

    def scale(src, size, dest=None):
        ui(f"transform.scale {src.kind}{src.get_size()} -> {tuple(size)} into={getattr(dest, 'kind', None)}")
        return dest or FakeSurface(size)

    transform.scale = scale
    pg.transform = transform

    image = types.ModuleType("pygame.image")
    image.save = lambda *a, **k: None
    pg.image = image

    mouse = types.ModuleType("pygame.mouse")
    mouse.set_visible = lambda v: ui(f"mouse.set_visible {v}")
    pg.mouse = mouse

    ptime = types.ModuleType("pygame.time")

    class Clock:
        def tick(self, *a):
            return 0

    ptime.Clock = Clock
    pg.time = ptime

    event = types.ModuleType("pygame.event")
    event.get = lambda *a, **k: []
    pg.event = event

    version = types.ModuleType("pygame.version")
    version.ver = "fake"
    pg.version = version
    for sub in ("display", "mixer", "font", "draw", "transform", "image", "mouse", "time", "event", "version"):
        sys.modules[f"pygame.{sub}"] = getattr(pg, sub)
    return pg


sys.modules["pygame"] = _build_fake_pygame()


def _build_fake_requests() -> types.ModuleType:
    """No network in the harness: any real request fails loudly."""
    rq = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    for name in ("ConnectionError", "Timeout", "HTTPError", "TooManyRedirects"):
        setattr(rq, name, type(name, (RequestException,), {}))
    rq.RequestException = RequestException
    exc_mod = types.ModuleType("requests.exceptions")
    for name in ("RequestException", "ConnectionError", "Timeout", "HTTPError", "TooManyRedirects"):
        setattr(exc_mod, name, getattr(rq, name))
    rq.exceptions = exc_mod

    def _blocked(*a, **k):
        raise rq.ConnectionError("network disabled in the golden harness")

    class Session:
        def __init__(self, *a, **k):
            self.headers = {}

        def __getattr__(self, name):
            return _blocked

    rq.Session = Session
    for name in ("get", "post", "put", "delete", "head", "request"):
        setattr(rq, name, _blocked)
    sys.modules["requests.exceptions"] = exc_mod
    return rq


sys.modules["requests"] = _build_fake_requests()

# --------------------------------------------------------------------------
# Fake media: what ffprobe "sees" for each source URI.
# --------------------------------------------------------------------------
MEDIA: dict[str, dict] = {}


def media(uri, *, w, h, fps, field="progressive", audio=(("ac3", 6),), real_fps_durs=None):
    MEDIA[uri] = {
        "w": w,
        "h": h,
        "fps": fps,
        "field": field,
        "audio": [{"codec_name": c, "channels": n} for c, n in audio],
        "durs": real_fps_durs,
    }


def fake_check_output(args, *a, **k):
    argv = [str(x) for x in args]
    tool = Path(argv[0]).name
    if tool == "ffmpeg" and "-filters" in argv:
        return " ... loudnorm  A->A  EBU R128 loudness normalization\n ... lavcac3enc\n"
    if tool == "ffprobe":
        uri = argv[-1]
        m = MEDIA.get(uri)
        if m is None:
            raise subprocess.CalledProcessError(1, argv)
        entries = argv[argv.index("-show_entries") + 1] if "-show_entries" in argv else ""
        if entries.startswith("frame=pkt_duration_time"):
            if not m["durs"]:
                return ""
            return "\n".join(f"{d}," for d in m["durs"]) + "\n"
        if "-select_streams" in argv and argv[argv.index("-select_streams") + 1] == "a":
            return json.dumps({"streams": m["audio"]})
        if entries.startswith("stream=width"):
            return json.dumps(
                {
                    "streams": [
                        {
                            "width": m["w"],
                            "height": m["h"],
                            "avg_frame_rate": m["fps"],
                            "r_frame_rate": m["fps"],
                            "field_order": m["field"],
                        }
                    ]
                }
            )
        if entries.startswith("stream=avg_frame_rate"):
            return json.dumps({"streams": [{"avg_frame_rate": m["fps"], "r_frame_rate": m["fps"]}]})
    raise FileNotFoundError(argv[0])


def fake_run(args, *a, **k):
    return subprocess.CompletedProcess(args, 1, "", "")


SPAWNS: list[dict] = []


class FakeChild:
    pid = 4242
    returncode = None

    def __init__(self, argv):
        self.argv = argv

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9


def fake_popen(args, *a, **k):
    argv = [str(x) for x in args]
    env = k.get("env")
    if env is None:
        env_diff = {}
    else:
        env_diff = {key: val for key, val in env.items() if os.environ.get(key) != val}
    SPAWNS.append({"argv": argv, "env": env_diff})
    return FakeChild(argv)


subprocess.check_output = fake_check_output
subprocess.run = fake_run
subprocess.Popen = fake_popen

# --------------------------------------------------------------------------
# Import the app (after the fakes are in place).
# --------------------------------------------------------------------------
sys.path.insert(0, str(REPO / "src"))
try:  # merged code only: point auto-detection at the fake root
    from dvdplayer_python.core import platform as _platform_mod  # type: ignore

    _platform_mod.ROOT = ROOT
except ImportError:
    _platform_mod = None

from dvdplayer_python.playback import session as S  # noqa: E402
from dvdplayer_python import main as M  # noqa: E402
from dvdplayer_python.core.models import PlaybackKind, PlaybackSource, PlaybackPrefs  # noqa: E402

IPC: list[str] = []


def fake_send(self, payload):
    IPC.append(json.dumps(payload.get("command"), sort_keys=True))
    return {"error": "success", "data": None}


def fake_drm_target(target_mode, connector=S.RGBPI_CONNECTOR_NAME):
    if connector != EXPECTED_CONNECTOR:
        return None
    return S.DrmLaunchTarget(card="card0", connector=connector, mode_name=target_mode)


S.PlaybackSession._send = fake_send
S.PlaybackSession._ipc_get_property = staticmethod(lambda *a, **k: None)
S.PlaybackSession._wait_for_ipc = staticmethod(lambda *a, **k: None)
S.PlaybackSession._verify_playback_session = staticmethod(lambda *a, **k: None)
S.PlaybackSession._verify_session_stability = staticmethod(lambda *a, **k: None)
S._resolve_drm_launch_target = fake_drm_target
S._read_drm_mode = lambda *a, **k: None
M.App._start_joystick_listener = lambda self: None
M.App._autostart_youtube_receiver = lambda self: None
M.ControlServer.start = lambda self: None
M.scan_dvd_candidates = lambda *a, **k: []
M._detect_tty = lambda: None

# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
NAS = "/media/nas/Movies"
media(f"{NAS}/film_1080p_23976_ac3.mkv", w=1920, h=1080, fps="24000/1001", audio=(("ac3", 6), ("aac", 2)))
media(f"{NAS}/uhd_2160p_hevc_truehd.mkv", w=3840, h=2160, fps="24000/1001", audio=(("truehd", 8), ("ac3", 6)))
media(f"{NAS}/pal_576i_tff_mp2.ts", w=720, h=576, fps="25/1", field="tt", audio=(("mp2", 2),))
media(f"{NAS}/ntsc_480i_tff_ac3.ts", w=720, h=480, fps="30000/1001", field="tt", audio=(("ac3", 2),))
media(
    f"{NAS}/anime_24p_labelled_2997.mkv",
    w=1280,
    h=720,
    fps="30000/1001",
    audio=(("aac", 2),),
    real_fps_durs=[0.042] * 60 + [0.041] * 40,
)
media(f"{NAS}/pal_25p_progressive_aac.mp4", w=1024, h=576, fps="25/1", audio=(("aac", 2),))
media(f"{NAS}/sport_720p50_flac51.mkv", w=1280, h=720, fps="50/1", audio=(("flac", 6),))
media(f"{NAS}/tv_1080i25_dts.ts", w=1920, h=1080, fps="25/1", field="tb", audio=(("dts", 6),))
media("http://192.0.2.10:32400/library/parts/1/file.mkv", w=1920, h=800, fps="24000/1001", audio=(("dts", 6),))
media("/media/sd/roms/dvd/MOVIE.iso", w=720, h=576, fps="25/1", field="tt", audio=(("ac3", 6),))

# Authored VIDEO_TS folder with a real MPEG-2 sequence header (720x576).
VIDEO_TS = TMP / "media" / "DVD_FOLDER" / "VIDEO_TS"
VIDEO_TS.mkdir(parents=True)
seq = b"\x00\x00\x01\xb3" + bytes([0x2D, 0x02, 0x40, 0x33, 0xFF, 0xFF, 0xE0])
(VIDEO_TS / "VTS_01_1.VOB").write_bytes(b"\x00" * 64 + seq + b"\x00" * 64)


def vf(name):
    return PlaybackSource(title=name, kind=PlaybackKind.VIDEO_FILE, uri=f"{NAS}/{name}")


SOURCES = {
    "film_1080p": vf("film_1080p_23976_ac3.mkv"),
    "uhd_hevc": vf("uhd_2160p_hevc_truehd.mkv"),
    "pal_576i": vf("pal_576i_tff_mp2.ts"),
    "ntsc_480i": vf("ntsc_480i_tff_ac3.ts"),
    "anime_24p_as_2997": vf("anime_24p_labelled_2997.mkv"),
    "pal_25p": vf("pal_25p_progressive_aac.mp4"),
    "sport_720p50": vf("sport_720p50_flac51.mkv"),
    "tv_1080i25": vf("tv_1080i25_dts.ts"),
    "plex_film": PlaybackSource(
        title="Plex Film",
        kind=PlaybackKind.PLEX_VIDEO,
        uri="http://192.0.2.10:32400/library/parts/1/file.mkv",
    ),
    "dvd_iso_title3": PlaybackSource(
        title="MOVIE",
        kind=PlaybackKind.DVD_ISO,
        uri="/media/sd/roms/dvd/MOVIE.iso",
        authored_dvd=True,
        dvd_title=3,
    ),
    "dvd_folder_root": PlaybackSource(
        title="DVD_FOLDER",
        kind=PlaybackKind.DVD_FOLDER,
        uri=str(VIDEO_TS.parent),
        authored_dvd=True,
    ),
}

BASE_PREFS = PlaybackPrefs()
PREF_VARIANTS = {
    "default": {},
    "optical": {"audio_output": "optical_5_1"},
    "50hz+bob": {"default_mode": "50hz", "deinterlace_mode": "bob"},
    "zoom+nonorm+nospeedup": {
        "aspect_mode": "zoom",
        "volume_normalization": "off",
        "pal_speedup": False,
        "ntsc_speedup": False,
    },
    "stale_hdmi_pref": {"audio_output": "hdmi"},
}
CASES = []
for src_name in SOURCES:
    CASES.append((src_name, "default"))
    CASES.append((src_name, "optical"))
for src_name in ("film_1080p", "pal_576i", "ntsc_480i", "anime_24p_as_2997", "dvd_iso_title3"):
    CASES.append((src_name, "50hz+bob"))
for src_name in ("film_1080p", "pal_25p", "plex_film"):
    CASES.append((src_name, "zoom+nonorm+nospeedup"))
for src_name in ("film_1080p", "dvd_folder_root"):
    CASES.append((src_name, "stale_hdmi_pref"))


def apply_prefs(app, variant):
    fresh = PlaybackPrefs()
    for field, value in vars(fresh).items():
        setattr(app.playback_state.prefs, field, value)
    for field, value in PREF_VARIANTS[variant].items():
        setattr(app.playback_state.prefs, field, value)
    app.playback_state.write_prefs()


def pump_until(app, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app._tick_background_work(time.time())
        if predicate():
            return True
        time.sleep(0.005)
    return False


def run_case(app, src_name, variant):
    src = SOURCES[src_name]
    # Fresh probe caches so every case is independent of execution order.
    for cache in ("_VIDEO_FPS_CACHE", "_VIDEO_INTERLACED_CACHE"):
        getattr(S, cache).clear()
    apply_prefs(app, variant)
    SPAWNS.clear()
    IPC.clear()
    take_ui()
    emit(f"### case {src_name} / prefs={variant}")
    app.start_playback(src, resume_prompt=False, start_from_beginning=True)
    ok = pump_until(app, lambda: app.playback is not None or app.message is not None)
    if not ok:
        emit("  START: TIMEOUT")
    elif app.playback is None:
        emit(f"  START: FAILED message={norm(getattr(app.message, 'body', app.message))}")
    else:
        pb = app.playback
        emit(
            f"  session: target_mode={pb.target_mode} backend_profile={pb.backend_profile} "
            f"audio_mode={pb.audio_mode} effective_mode={pb.effective_mode} degraded={pb.degraded}"
        )
    for i, spawn in enumerate(SPAWNS):
        emit(f"  spawn[{i}] argv:")
        for arg in spawn["argv"]:
            emit(f"    {norm(arg)}")
        for key in sorted(spawn["env"]):
            emit(f"  spawn[{i}] env {key}={norm(spawn['env'][key])}")
    for e in take_ui():
        emit(f"  ui(start): {e}")
    for cmd in IPC:
        emit(f"  ipc: {norm(cmd)}")
    IPC.clear()
    if app.playback is not None:
        try:
            app.stop_playback("done")
        except Exception as exc:  # pragma: no cover - recorded, not hidden
            emit(f"  STOP: EXCEPTION {type(exc).__name__}: {exc}")
        for e in take_ui():
            emit(f"  ui(stop): {e}")
        app.message = None
    else:
        app.message = None
        for e in take_ui():
            emit(f"  ui(fail): {e}")
    emit()


def launcher_env():
    """Run the real launcher with a fake python3 and record what it exports."""
    work = TMP / "launcher"
    app_dir = work / "app"
    rootfs = app_dir / "runtime" / "linux-arm64-rootfs"
    (rootfs / "lib" / "aarch64-linux-gnu").mkdir(parents=True)
    (app_dir / "state" / "runtime").mkdir(parents=True)
    fakebin = work / "bin"
    fakebin.mkdir()
    dump = work / "env.dump"
    py = fakebin / "python3"
    py.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f'env > "{dump}"\n'
    )
    for name in ("systemctl", "pkill", "fgconsole"):
        stub = fakebin / name
        stub.write_text("#!/bin/sh\nexit 1\n")
    for f in fakebin.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(work), "DVDPLAYER_APP_DIR": str(app_dir)}
    for key, value in PLATFORM_ENV.items():
        if key != "DVDPLAYER_MPV_LD_LIBRARY_PATH":
            env[key] = value
    proc = _REAL_POPEN(
        ["/bin/sh", str(REPO / "start_rgbpi_dvdplayer_python.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    proc.communicate(timeout=30)
    emit(f"### launcher (exit={proc.returncode})")
    if not dump.exists():
        emit("  python was NOT launched")
        return
    seen = {}
    for line in dump.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            seen[k] = v
    for key in sorted(seen):
        if key.startswith(("DVDPLAYER_", "SDL_", "LD_LIBRARY_PATH", "PYTHONPATH", "XDG_RUNTIME_DIR")):
            val = seen[key].replace(str(app_dir), "<APP>").replace(str(REPO), "<REPO>")
            emit(f"  {key}={val}")
    emit()


def main():
    emit(f"# platform golden — {PLATFORM}")
    emit()
    launcher_env()

    # App init: what the UI does to the display before any playback.
    app = M.App()
    emit("### app init")
    for e in take_ui():
        emit(f"  ui: {e}")
    app._draw()
    for e in take_ui():
        emit(f"  ui(draw): {e}")
    emit()

    emit("### settings menu")
    app._refresh_settings_items() if hasattr(app, "_refresh_settings_items") else None
    for item in app._settings_items():
        emit(f"  {item.kind}: {norm(item.title)} | {norm(item.subtitle)}")
    emit()

    emit("### audio output subtitle per stored pref")
    for stored in ("jack", "optical_5_1", "optical", "hdmi", "bogus"):
        app.playback_state.prefs.audio_output = stored
        emit(f"  {stored} -> {app._audio_output_subtitle()}")
    emit()

    emit("### audio output cycle")
    for start in ("jack", "hdmi"):
        app.playback_state.prefs.audio_output = start
        item = next(i for i in app._settings_items() if i.kind == "settings_audio_output")
        app._adjust_switchable_setting(item, M.Action.RIGHT)
        emit(f"  from {start}: right -> {app.playback_state.prefs.audio_output}")
    item = next(i for i in app._settings_items() if i.kind == "settings_audio_output")
    app.playback_state.prefs.audio_output = "jack"
    for action in [M.Action.RIGHT] * 4 + [M.Action.LEFT] * 4:
        app._adjust_switchable_setting(item, action)
        value = app.playback_state.prefs.audio_output
        emit(f"  {action.value:>5} -> {value} ({app._audio_output_label(value)})")
    app.message = None
    take_ui()
    emit()

    for src_name, variant in CASES:
        run_case(app, src_name, variant)

    sys.stdout.write("\n".join(OUT) + "\n")
    sys.stdout.flush()
    shutil.rmtree(TMP, ignore_errors=True)
    os._exit(0)  # daemon threads (youtube/plex helpers) must not block exit


if __name__ == "__main__":
    main()
