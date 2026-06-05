from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import pygame

from dvdplayer_python.core.models import WINDOW_H, WINDOW_W


THEME_BG = (20, 10, 6)
THEME_PANEL = (30, 16, 10)
THEME_BORDER = (200, 150, 110)
THEME_TEXT = (248, 238, 226)
THEME_TEXT_DIM = (204, 162, 118)
THEME_ROW = (56, 30, 16)
THEME_ROW_ACTIVE = (242, 234, 224)
THEME_ROW_ACTIVE_TEXT = (98, 58, 34)

SAFE_X = 26
SAFE_Y = 20
SAFE_W = 268
SAFE_H = 206
HEADER_H = 24
CONTENT_Y = SAFE_Y + 30
CONTENT_H = 146
ROW_X = SAFE_X + 12
ROW_W = SAFE_W - 24
ROW_H = 22
ROW_STEP = 24
FOOTER_Y = SAFE_Y + SAFE_H - 18
FOOTER_H = 18


@dataclass
class RenderModel:
    title: str
    section: str
    footer: str
    rows: list[tuple[str, str, bool]]
    selected: int
    message_title: Optional[str] = None
    message_body: Optional[str] = None


class Renderer:
    def __init__(self):
        pygame.init()
        flags = 0
        if __import__("os").environ.get("DVDPLAYER_WINDOWED") != "1":
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((WINDOW_W, WINDOW_H), flags)
        pygame.mouse.set_visible(False)
        self.font_l = pygame.font.SysFont("DejaVu Sans", 16, bold=True)
        self.font_m = pygame.font.SysFont("DejaVu Sans", 12, bold=True)
        self.font_s = pygame.font.SysFont("DejaVu Sans", 10)

    @staticmethod
    def _fit_text(font: pygame.font.Font, text: str, max_width: int) -> str:
        if max_width <= 0:
            return ""
        raw = str(text or "")
        if font.size(raw)[0] <= max_width:
            return raw
        ellipsis = "..."
        if font.size(ellipsis)[0] > max_width:
            return ""
        trimmed = raw
        while trimmed:
            trimmed = trimmed[:-1]
            candidate = trimmed.rstrip() + ellipsis
            if font.size(candidate)[0] <= max_width:
                return candidate
        return ellipsis

    def clear(self) -> None:
        self.screen.fill(THEME_BG)

    def draw_panel(self, x: int, y: int, w: int, h: int, fill=THEME_PANEL, border=THEME_BORDER) -> None:
        pygame.draw.rect(self.screen, fill, pygame.Rect(x, y, w, h))
        pygame.draw.rect(self.screen, border, pygame.Rect(x, y, w, h), 1)

    def text(self, text: str, x: int, y: int, color=THEME_TEXT, size="m", center=False) -> None:
        font = {"l": self.font_l, "m": self.font_m, "s": self.font_s}[size]
        surf = font.render(text, True, color)
        rect = surf.get_rect()
        if center:
            rect.center = (x, y)
        else:
            rect.topleft = (x, y)
        self.screen.blit(surf, rect)

    def draw_model(self, model: RenderModel) -> None:
        self.clear()
        self.draw_panel(SAFE_X, SAFE_Y, SAFE_W, HEADER_H)
        title = self._fit_text(self.font_m, model.title, SAFE_W - 118)
        section = self._fit_text(self.font_s, model.section, 84)
        self.text(title, SAFE_X + 6, SAFE_Y + 6, size="m")
        section_surf = self.font_s.render(section, True, THEME_TEXT_DIM)
        self.screen.blit(section_surf, section_surf.get_rect(topright=(SAFE_X + SAFE_W - 6, SAFE_Y + 8)))

        self.draw_panel(SAFE_X, CONTENT_Y, SAFE_W, CONTENT_H)
        y = CONTENT_Y + 8
        # Show a 6-row window. Screens that already window their own list
        # (LIST, DVD picker) pass <=6 rows with a relative `selected`, so
        # win_start stays 0 and nothing changes. Screens that pass the full
        # list with an absolute `selected` (e.g. the 7-row keyboard) scroll
        # minimally so the selected row — and DONE — stay visible.
        visible = 6
        total = len(model.rows)
        sel = model.selected
        if total > visible and sel >= visible:
            win_start = min(sel - visible + 1, total - visible)
        else:
            win_start = 0
        window = model.rows[win_start : win_start + visible]
        for offset, (title, subtitle, _active) in enumerate(window):
            i = win_start + offset
            selected = i == model.selected
            fill = THEME_ROW_ACTIVE if selected else THEME_ROW
            self.draw_panel(ROW_X, y, ROW_W, ROW_H, fill=fill, border=THEME_BORDER)
            title_text = self._fit_text(self.font_m, title, 138)
            subtitle_text = self._fit_text(self.font_s, subtitle, 108)
            title_color = THEME_ROW_ACTIVE_TEXT if selected else THEME_TEXT
            subtitle_color = THEME_ROW_ACTIVE_TEXT if selected else THEME_TEXT_DIM
            self.text(title_text, ROW_X + 2, y + 4, color=title_color, size="m")
            subtitle_surf = self.font_s.render(subtitle_text, True, subtitle_color)
            self.screen.blit(subtitle_surf, subtitle_surf.get_rect(topright=(ROW_X + ROW_W - 8, y + 6)))
            y += ROW_STEP

        self.draw_panel(SAFE_X, FOOTER_Y, SAFE_W, FOOTER_H)
        footer_text = self._fit_text(self.font_s, model.footer, SAFE_W - 18)
        self.text(footer_text, WINDOW_W // 2, FOOTER_Y + 8, color=THEME_TEXT_DIM, size="s", center=True)

        if model.message_title:
            msg_w = 236
            msg_h = 92
            msg_x = (WINDOW_W - msg_w) // 2
            msg_y = (WINDOW_H - msg_h) // 2
            self.draw_panel(msg_x, msg_y, msg_w, msg_h)
            self.text(self._fit_text(self.font_m, model.message_title, msg_w - 24), WINDOW_W // 2, msg_y + 12, size="m", center=True)
            self.text(self._fit_text(self.font_s, model.message_body or "", msg_w - 24), WINDOW_W // 2, msg_y + 40, size="s", center=True)
            self.text("A/B CLOSE", WINDOW_W // 2, msg_y + 70, color=THEME_TEXT_DIM, size="s", center=True)

        pygame.display.flip()

    def screenshot(self, path: str) -> None:
        pygame.image.save(self.screen, path)
