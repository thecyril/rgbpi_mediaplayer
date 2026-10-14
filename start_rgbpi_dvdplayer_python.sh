#!/bin/sh
set -eu

APP_DIR="${DVDPLAYER_APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
VENV="$APP_DIR/.venv"
TIMINGS_FILE="/opt/rgbpi/ui/data/timings.dat"
RUNTIME_DIR="$APP_DIR/state/runtime"
RUNTIME_ROOT="$APP_DIR/runtime/linux-arm64-rootfs"
LOG_FILE="$RUNTIME_DIR/rgbpi-dvdplayer-python-launch.log"
export DVDPLAYER_APP_DIR="$APP_DIR"
export SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-alsa}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
export DVDPLAYER_MPV_BIN="${DVDPLAYER_MPV_BIN:-$APP_DIR/bin/mpv}"
EXTRA_LIB_DIRS="$APP_DIR/lib"
if [ -d "$RUNTIME_ROOT/lib/aarch64-linux-gnu" ]; then
  RUNTIME_LIB_DIRS="$RUNTIME_ROOT/lib/aarch64-linux-gnu:$RUNTIME_ROOT/usr/lib/aarch64-linux-gnu:$RUNTIME_ROOT/usr/lib/aarch64-linux-gnu/pulseaudio:$RUNTIME_ROOT/usr/lib/aarch64-linux-gnu/samba"
  if [ "${DVDPLAYER_RUNTIME_LIBS:-all}" = "mpv-only" ]; then
    # RePlayOS (Debian trixie): the bundled bullseye rootfs must stay invisible
    # to the system python/pygame (SDL + glibc version clashes) — only the
    # bundled mpv child gets it, via _child_env in playback/session.py.
    export DVDPLAYER_MPV_LD_LIBRARY_PATH="$RUNTIME_LIB_DIRS"
  else
    EXTRA_LIB_DIRS="$RUNTIME_LIB_DIRS:$EXTRA_LIB_DIRS"
  fi
fi
if [ -n "${LD_LIBRARY_PATH:-}" ]; then
  export LD_LIBRARY_PATH="$EXTRA_LIB_DIRS:$LD_LIBRARY_PATH"
else
  export LD_LIBRARY_PATH="$EXTRA_LIB_DIRS"
fi
export DVDPLAYER_CONTROL_SOCKET="${DVDPLAYER_CONTROL_SOCKET:-$RUNTIME_DIR/rgbpi-dvdplayer-api.sock}"
export DVDPLAYER_STATE_PATH="${DVDPLAYER_STATE_PATH:-$RUNTIME_DIR/rgbpi-dvdplayer-state.json}"
export DVDPLAYER_DEBUG_LOG="${DVDPLAYER_DEBUG_LOG:-$RUNTIME_DIR/rgbpi-dvdplayer-python.log}"
export DVDPLAYER_MPV_LOG="${DVDPLAYER_MPV_LOG:-$RUNTIME_DIR/rgbpi-dvdplayer-mpv.log}"
export TERM="${TERM:-linux}"

if [ -z "${DVDPLAYER_ACTIVE_TTY:-}" ] && command -v fgconsole >/dev/null 2>&1; then
  FG_TTY="$(fgconsole 2>/dev/null || true)"
  case "$FG_TTY" in
    ''|*[!0-9]*)
      ;;
    *)
      export DVDPLAYER_ACTIVE_TTY="/dev/tty$FG_TTY"
      ;;
  esac
fi

if [ -n "${DVDPLAYER_ACTIVE_TTY:-}" ]; then
  if [ -c "$DVDPLAYER_ACTIVE_TTY" ]; then
    exec </dev/null
    exec <"$DVDPLAYER_ACTIVE_TTY" >"$DVDPLAYER_ACTIVE_TTY" 2>&1
  fi
fi

disable_rgbpi_services() {
  for svc in \
    virtual-controller.service \
    xboxdrv.service
  do
    if command -v systemctl >/dev/null 2>&1; then
      systemctl stop "$svc" >/dev/null 2>&1 || true
      systemctl disable "$svc" >/dev/null 2>&1 || true
    fi
  done
  pkill -f 'virtual.*controller' >/dev/null 2>&1 || true
}

disable_rgbpi_services

if [ -f "$TIMINGS_FILE" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    grep -qF "$line" "$TIMINGS_FILE" || echo "$line" >>"$TIMINGS_FILE"
  done <<'EOF'
320 1 20 32 45 240 1 2 3 16 0 0 0 60.000000 0 6514560 1
720 1 29 69 117 480 1 3 6 34 0 0 0 30 1 14670150 1
720 1 29 69 117 576 1 7 6 38 0 0 0 25 1 14656125 1
EOF
fi

export PYTHONPATH="$APP_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

choose_log_file() {
  uid="$(id -u 2>/dev/null || echo 0)"
  mkdir -p "$RUNTIME_DIR" 2>/dev/null || true
  for candidate in \
    "$RUNTIME_DIR/rgbpi-dvdplayer-python-launch.log" \
    "/tmp/rgbpi-dvdplayer-python-launch.${uid}.log" \
    "/tmp/rgbpi-dvdplayer-python-launch.log"
  do
    dir="$(dirname "$candidate")"
    mkdir -p "$dir" 2>/dev/null || true
    if ( : >> "$candidate" ) 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  echo "/dev/null"
  return 0
}

mkdir -p "$RUNTIME_DIR" 2>/dev/null || true
chmod 777 "$RUNTIME_DIR" 2>/dev/null || true
rm -f \
  "$RUNTIME_DIR/rgbpi-dvdplayer-api.sock" \
  "$RUNTIME_DIR/rgbpi-dvdplayer.lock" \
  "$RUNTIME_DIR/rgbpi-dvdplayer-state.json" \
  "$RUNTIME_DIR/state.json" \
  2>/dev/null || true

can_run() {
  py="$1"
  [ -x "$py" ] || return 1
  "$py" -c "import pygame,requests" >/dev/null 2>&1
}

LOG_FILE="$(choose_log_file)"
pkill -f 'python.*-m dvdplayer_python.main' >/dev/null 2>&1 || true

if [ -x "$APP_DIR/runtime/check_runtime_bundle.sh" ]; then
  "$APP_DIR/runtime/check_runtime_bundle.sh" --check >>"$LOG_FILE" 2>&1
fi

PYTHON=""
if can_run "$VENV/bin/python"; then
  PYTHON="$VENV/bin/python"
elif can_run "$(command -v python3 2>/dev/null || true)"; then
  PYTHON="$(command -v python3)"
else
  echo "No Python interpreter with pygame+requests found." >>"$LOG_FILE" 2>/dev/null || true
  echo "No Python interpreter with pygame+requests found." >&2
  exit 1
fi

echo "[$(date -Iseconds)] launch python=$PYTHON app_dir=$APP_DIR" >>"$LOG_FILE" 2>/dev/null || true
exec "$PYTHON" -m dvdplayer_python.main >>"$LOG_FILE" 2>&1
