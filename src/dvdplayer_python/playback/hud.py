"""Playback HUD overlay (Plex-style) rendered via mpv ``osd-overlay`` + ASS.

The HUD is a thin, controller-friendly band at the bottom of the screen
showing the media title, a progress bar, play/pause state, current time /
duration, and a hint for opening the START menu. It is shown on user
activity (pause toggle, seek, etc.) via :meth:`PlaybackHUD.flash` and
auto-hides after a few seconds of inactivity, the same way every modern
media player works.

Design notes
------------
* **Single mpv overlay slot.**  The HUD owns ``osd-overlay`` id
  :data:`HUD_OVERLAY_ID` (slots 1 and 2 are taken by the existing
  badge/info overlays in :mod:`playback.session`). Hiding the HUD
  re-sends ``osd-overlay`` with ``format="none"``, which removes it
  from the screen with no flicker.
* **Reference resolution: 720-line baseline, aspect-adapted width.**
  ``res_y`` is fixed at 720 so font sizes and Y coordinates render
  identically on a 240p CRT and a 1080p LCD (same baseline as the
  ``--osd-font-size=36`` constant in :mod:`playback.session`).
  ``res_x`` is recomputed from mpv's current OSD aspect ratio on every
  flash — 1280 on a 16:9 LCD, 960 on a 4:3 CRT — and the right-anchored
  HUD elements (title-side glyph, hint, progress extent) are laid out
  against that dynamic width. This avoids both the auto-aspect clipping
  bug (when ``res_x=0`` made mpv silently shrink the canvas to 960×720
  on 4:3 outputs and clip everything past x≈960) and the alternative
  "fixed 1280×720" stretching that would squish rectangles on 4:3.
* **Standalone, mpv-agnostic.**  The class takes two callables —
  ``send_command`` (the mpv IPC shim) and ``get_state`` (returns the
  current pause / position / duration) — so it can be unit-tested
  without an mpv subprocess.
* **Snapshot semantics.**  ``flash()`` renders the HUD once with the
  current state; ``tick()`` only handles auto-hide. mpv keeps the
  overlay on screen until we tell it otherwise — *provided the
  underlying IPC socket stays open*. (mpv ties ``osd-overlay`` lifetime
  to the libmpv client that issued it: closing the socket destroys the
  overlay on the next frame. The persistent socket lives on
  :class:`PlaybackSession`.)
* **Robust on mpv shutdown.**  IPC errors during render are swallowed
  after a single ``playback_hud_render_failed`` debug event; the next
  ``flash()`` will retry transparently. The HUD never raises into the
  main loop.
* **mpv 0.32 compatibility.**  The bundled mpv on the Pi is 0.32, where
  ``osd-overlay`` takes exactly 6 positional args (id, format, data,
  res_x, res_y, z). The ``hidden`` / ``compute_bounds`` flags added in
  0.34+ would make 0.32 silently reject the command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from dvdplayer_python.core.debuglog import log_event

# -- Overlay slot ---------------------------------------------------------------
# OVERLAY_MAIN_ID (1) and OVERLAY_BADGE_ID (2) are taken by playback.session.
# Pick a separate id so the HUD never fights with badge/info overlays.
HUD_OVERLAY_ID = 7

# -- Layout (mpv "scaled" units, baseline 1280x720) -----------------------------
_HUD_RES_X = 1280
_HUD_RES_Y = 720

_BAR_PADDING_X = 80           # left/right margin from the canvas edges
_BAR_Y = 624                  # top of the progress bar
_BAR_HEIGHT = 12
_KNOB_RADIUS = 9

_TITLE_Y = 552
_TITLE_FONT_SIZE = 36
_ICON_FONT_SIZE = 44
_TIME_FONT_SIZE = 26
_HINT_FONT_SIZE = 22
_TIME_LINE_Y = _BAR_Y + _BAR_HEIGHT + 8

_PANEL_TOP_Y = _TITLE_Y - 28
_PANEL_BOTTOM_Y = _TIME_LINE_Y + _TIME_FONT_SIZE + 8

# -- Timing ---------------------------------------------------------------------
_AUTOHIDE_SECONDS = 4.0

# -- Glyphs ---------------------------------------------------------------------
# Unicode glyphs that render in mpv's bundled OSD font (DejaVu / Roboto-style).
_PLAY_ICON = "▶"
_PAUSE_ICON = "❚❚"


def _esc(text: object) -> str:
    """Escape user-supplied text for inclusion in an ASS dialogue body."""
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
    )


def _fmt_time(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _color(bgr_hex: str, alpha: int = 0) -> str:
    """ASS override pair for primary fill colour and alpha.

    Args:
        bgr_hex: 6-hex BGR string (e.g. ``"FFFFFF"`` for white).
        alpha:   0 (opaque) to 255 (transparent).
    """
    return f"\\1c&H{bgr_hex}&\\1a&H{max(0, min(255, int(alpha))):02X}&"


# Pre-computed colour overrides used by _build_ass(). Keeping them as module
# constants avoids rebuilding the strings on every frame.
_COL_TEXT = _color("FFFFFF")
_COL_DIM = _color("C0C0C0")
_COL_FILL = _color("FFFFFF")
_COL_TRACK = _color("808080", alpha=0x80)
_COL_PANEL = _color("000000", alpha=0xA0)
_BORDER = "\\3c&H000000&\\3a&H00&\\bord2\\shad0"


@dataclass
class HUDState:
    """Runtime state for :class:`PlaybackHUD`.

    All fields are owned by the HUD; nothing outside ``hud.py`` should mutate
    them directly.
    """

    visible: bool = False
    last_shown_at: float = 0.0
    title: str = ""
    paused: bool = False
    position: float = 0.0
    duration: Optional[float] = None
    canvas_w: int = _HUD_RES_X


class PlaybackHUD:
    """Plex-style playback HUD driven via mpv's ``osd-overlay`` IPC command.

    The HUD is **passive**: the caller (typically ``main.py``) calls
    :meth:`flash` when the user does something (pause toggle, seek) and
    :meth:`tick` every frame from the main loop. The HUD owns its own
    visibility timer; no separate thread is involved.

    Args:
        send_command: Callable forwarding to ``PlaybackSession.command``.
            Taking a callable (rather than the session) keeps this module
            self-contained and trivially testable.
        get_state:   Callable returning ``(paused, position_s, duration_s)``.
            ``duration_s`` may be ``None`` when mpv has not reported a
            duration yet (e.g. live streams).
        get_canvas_aspect: Optional callable returning the OSD output's
            display aspect ratio (``width / (height * par)`` — see
            :meth:`PlaybackSession._osd_display_aspect` for why
            ``osd-par`` is *inverse* of the classical PAR convention in
            mpv). When provided (and returning a positive float), the
            HUD recomputes its canvas width on every flash so the
            720-line baseline layout stays proportional on 4:3 CRT
            modes as well as 16:9 LCDs. Returning ``None`` (or omitting
            the callable) keeps the previous 1280×720 baseline.
        title:       Initial media title.
        autohide_seconds: Time after last activity before the HUD auto-hides.
    """

    def __init__(
        self,
        send_command: Callable[[list[Any]], dict],
        get_state: Callable[[], tuple[bool, float, Optional[float]]],
        *,
        get_canvas_aspect: Optional[Callable[[], Optional[float]]] = None,
        title: str = "",
        autohide_seconds: float = _AUTOHIDE_SECONDS,
    ) -> None:
        self._send = send_command
        self._get_state = get_state
        self._get_canvas_aspect = get_canvas_aspect
        self._autohide_seconds = float(autohide_seconds)
        self._state = HUDState(title=str(title or ""))
        self._closed = False
        # Log the first IPC failure so a silent "HUD not appearing" bug can
        # be diagnosed from the debug log without rebuilding. Subsequent
        # failures stay silent to avoid log spam.
        self._error_logged = False

    # -- Public API ----------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._state.visible

    def set_title(self, title: str) -> None:
        self._state.title = str(title or "")

    def flash(self, *, now: Optional[float] = None) -> None:
        """Show the HUD (or extend its visibility) and render once.

        The render is a *snapshot* of the current state. mpv keeps the
        overlay on screen until we hide it; the next user input (pause,
        seek) calls :meth:`flash` again to take a fresh snapshot.
        """
        if self._closed:
            return
        ts = float(now if now is not None else time.time())
        was_visible = self._state.visible
        self._state.last_shown_at = ts
        self._state.visible = True
        self._refresh_state()
        self._render()
        log_event("playback_hud_flash", was_visible=was_visible)

    def hide(self, *, now: Optional[float] = None, reason: str = "manual") -> None:
        """Remove the HUD from the screen if it is currently visible."""
        if self._closed or not self._state.visible:
            return
        self._state.visible = False
        self._clear_overlay()
        log_event("playback_hud_hide", reason=reason)

    def tick(self, now: float) -> None:
        """Called from the main loop. Handles auto-hide only.

        Because the persistent IPC socket keeps the libmpv client alive
        between requests, mpv does *not* drop our overlay between frames —
        so we don't need to re-render it. One IPC per ``flash()``, one
        IPC per auto-hide. Nothing in between.
        """
        if self._closed or not self._state.visible:
            return
        if now - self._state.last_shown_at >= self._autohide_seconds:
            self.hide(now=now, reason="autohide")

    def close(self) -> None:
        """Idempotent teardown. Removes the overlay and disables the HUD."""
        if self._closed:
            return
        self._closed = True
        try:
            self._clear_overlay()
        except Exception:
            pass

    # -- Internals -----------------------------------------------------------

    def _refresh_state(self) -> None:
        try:
            paused, pos, dur = self._get_state()
        except Exception:
            return
        self._state.paused = bool(paused)
        self._state.position = float(pos or 0.0)
        self._state.duration = float(dur) if dur and dur > 0 else None
        self._state.canvas_w = self._compute_canvas_w()

    def _compute_canvas_w(self) -> int:
        """Canvas width in 720-baseline units, derived from the OSD aspect.

        Returns the 1280 fallback when the aspect cannot be queried (no
        callable wired, mpv hasn't reported OSD dimensions yet, etc.).
        Clamped to 1:1..21:9 so a bogus property value can't push the
        layout off-screen.
        """
        if self._get_canvas_aspect is None:
            return _HUD_RES_X
        try:
            aspect = self._get_canvas_aspect()
        except Exception:
            return _HUD_RES_X
        if not aspect or float(aspect) <= 0:
            return _HUD_RES_X
        clamped = max(1.0, min(2.5, float(aspect)))
        return int(round(_HUD_RES_Y * clamped))

    def _render(self) -> None:
        try:
            self._send(self._overlay_command("ass-events", self._build_ass()))
        except Exception as exc:
            if not self._error_logged:
                log_event("playback_hud_render_failed", error=str(exc))
                self._error_logged = True

    def _clear_overlay(self) -> None:
        try:
            self._send(self._overlay_command("none", ""))
        except Exception:
            pass

    def _overlay_command(self, fmt: str, data: str) -> list[Any]:
        """Build an ``osd-overlay`` IPC command targeting this HUD's slot.

        The 6-positional-arg form is the only one mpv 0.32 accepts (the
        ``hidden`` / ``compute_bounds`` flags were added in 0.34+ and would
        make 0.32 silently reject the command). ``res_x`` is passed
        explicitly — set to the same dynamic value the layout was built
        against (see :meth:`_compute_canvas_w`) so mpv doesn't silently
        narrow the canvas on non-16:9 outputs and clip the right edge.
        """
        return [
            "osd-overlay",
            HUD_OVERLAY_ID,
            fmt,
            data,
            self._state.canvas_w,
            _HUD_RES_Y,
            0,            # z-order
        ]

    # -- ASS construction ----------------------------------------------------

    def _build_ass(self) -> str:
        canvas_w = self._state.canvas_w
        bar_x0 = _BAR_PADDING_X
        bar_x1 = canvas_w - _BAR_PADDING_X
        bar_w = bar_x1 - bar_x0

        dur = self._state.duration
        if dur and dur > 0:
            progress = max(0.0, min(1.0, self._state.position / dur))
        else:
            progress = 0.0
        filled_w = int(bar_w * progress)

        icon = _PAUSE_ICON if self._state.paused else _PLAY_ICON
        title = _esc(self._state.title or "PLAYBACK")
        time_text = f"{_fmt_time(self._state.position)} / {_fmt_time(self._state.duration)}"
        hint = "A pause/play   <- -> seek   START menu"

        events: list[str] = [
            # Scrim band behind the HUD content.
            self._draw_rect(0, _PANEL_TOP_Y, canvas_w, _PANEL_BOTTOM_Y, _COL_PANEL),
            # Title (top-left of the band).
            self._text(
                _BAR_PADDING_X,
                _TITLE_Y,
                title,
                size=_TITLE_FONT_SIZE,
                colour=_COL_TEXT,
                align=7,
            ),
            # Play/pause glyph (top-right of the band).
            self._text(
                canvas_w - _BAR_PADDING_X,
                _TITLE_Y,
                icon,
                size=_ICON_FONT_SIZE,
                colour=_COL_TEXT,
                align=9,
            ),
            # Progress bar — track.
            self._draw_rect(bar_x0, _BAR_Y, bar_x1, _BAR_Y + _BAR_HEIGHT, _COL_TRACK),
        ]

        # Progress bar — filled portion.
        if filled_w > 0:
            events.append(
                self._draw_rect(bar_x0, _BAR_Y, bar_x0 + filled_w, _BAR_Y + _BAR_HEIGHT, _COL_FILL)
            )

        # Playhead knob (only when we have a real duration). User-preferred
        # square design via drawing-mode (matches the bar's rendering
        # primitive — same _draw_rect path, same anchor semantics, so any
        # bug in libass affects both identically and they stay aligned).
        if dur and dur > 0:
            knob_cx = bar_x0 + filled_w
            knob_cy = _BAR_Y + _BAR_HEIGHT // 2
            events.append(
                self._draw_rect(
                    knob_cx - _KNOB_RADIUS,
                    knob_cy - _KNOB_RADIUS,
                    knob_cx + _KNOB_RADIUS,
                    knob_cy + _KNOB_RADIUS,
                    _COL_FILL,
                )
            )

        # Bottom row: time on the left, hint on the right.
        events.append(
            self._text(
                _BAR_PADDING_X,
                _TIME_LINE_Y,
                _esc(time_text),
                size=_TIME_FONT_SIZE,
                colour=_COL_DIM,
                align=7,
            )
        )
        events.append(
            self._text(
                canvas_w - _BAR_PADDING_X,
                _TIME_LINE_Y,
                _esc(hint),
                size=_HINT_FONT_SIZE,
                colour=_COL_DIM,
                align=9,
            )
        )

        # Drop any empty events (e.g. zero-width rects when filled_w == 0).
        return "\n".join(event for event in events if event)

    @staticmethod
    def _text(x: int, y: int, body: str, *, size: int, colour: str, align: int) -> str:
        """Return an ASS dialogue body rendering ``body`` at (x, y).

        ``align`` follows the ASS numpad convention (7 = top-left,
        9 = top-right, 5 = centre, etc.). Caller is responsible for escaping
        ``body`` if it came from user input.
        """
        return (
            f"{{\\an{align}\\pos({x},{y})"
            f"\\fs{size}{colour}{_BORDER}}}"
            f"{body}"
        )

    @staticmethod
    def _draw_rect(x0: int, y0: int, x1: int, y1: int, colour: str) -> str:
        """Return an ASS dialogue body that fills a rectangle.

        The shape is anchored at ``(x0, y0)`` using ``\\an7`` (top-left) and
        drawn with ASS drawing-mode commands so the actual width/height are
        relative to the position. ``\\bord0\\shad0`` disables outline /
        shadow which would otherwise smear the rectangle edges.
        """
        w = max(0, x1 - x0)
        h = max(0, y1 - y0)
        if w == 0 or h == 0:
            return ""
        return (
            f"{{\\an7\\pos({x0},{y0})\\bord0\\shad0{colour}\\p1}}"
            f"m 0 0 l {w} 0 {w} {h} 0 {h}"
            "{\\p0}"
        )
