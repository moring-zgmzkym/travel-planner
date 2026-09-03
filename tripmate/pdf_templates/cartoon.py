"""Cartoon 模板：亮色粉黄卡通风（继承 classic 全部模块与重叠修复，只换主题气质）。

差异点：珊瑚粉/暖黄/薄荷亮色主题、封面走白色圆角面板模式（COVER_PHOTO_MODE="panel"）、
章节标题带 emoji 徽标（emoji_png 栅格化，缺字体自动省略）。文字全部水平，不旋转。
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import (Image, Paragraph, Table, TableStyle)

from .base import CONTENT_W, emoji_png
from .classic import ClassicTemplate


class CartoonTemplate(ClassicTemplate):
    name = "cartoon"
    display_name = "卡通游记风"
    description = "亮色粉黄配色 + 全粗体标题 + emoji 徽标，活泼可爱的游记风"
    scenes = "休闲 / 年轻化 / 亲子"

    # ---- 主题配色（亮色卡通风）----
    PRIMARY = colors.HexColor("#f2568c")
    PRIMARY_HEX = "#f2568c"
    ACCENT = colors.HexColor("#ffb020")
    ACCENT_RGB = (255, 176, 32)
    SUCCESS = colors.HexColor("#2fbf8f")
    SUCCESS_HEX = "#2fbf8f"
    LIGHT = colors.HexColor("#fff1e6")
    BG_LIGHT = colors.HexColor("#fff7e6")
    HAIRLINE = colors.HexColor("#ffd9c2")
    COVER_TOP_RGB = (255, 236, 210)
    COVER_BOT_RGB = (255, 170, 190)
    COVER_GOLD_RGB = (255, 176, 32)
    COVER_PHOTO_MODE = "panel"
    COVER_TITLE_RGB = (233, 78, 119)
    COVER_SUB_RGB = (140, 96, 160)
    COVER_MUTED_RGB = (165, 140, 168)
    COVER_STAT_RGB = (255, 140, 60)
    COVER_STATLAB_RGB = (150, 130, 155)
    COVER_CHIP_LINE_RGB = (255, 112, 150)
    COVER_CHIP_TEXT_RGB = (233, 78, 119)
    COVER_PANEL_RGB = (255, 255, 255)
    COVER_PANEL_ALPHA = 236

    # 章节标题关键词 → emoji 徽标（「酒店」须排在「实景」前：柒节标题同时含两词）
    _SECTION_EMOJI = [("总览", "🗺️"), ("天气", "⛅"), ("订单", "🎫"), ("逐日", "📅"),
                      ("预算", "💰"), ("酒店", "🏨"), ("实景", "📸"), ("美食", "🍜"),
                      ("注意", "⚠️")]

    def section(self, num: str, title: str) -> Table:
        """卡通徽标章节头：主题色编号块 + 粗体标题 + 章节对应 emoji 小图
        （emoji 栅格化失败自动省略该列，不阻塞）。"""
        emoji = next((e for k, e in self._SECTION_EMOJI if k in title), "")
        icon = emoji_png(emoji, 72)
        cells = [Paragraph(escape(num), self.style("secnum-c", 13, bold=True, color=colors.white,
                                                   alignment=TA_CENTER, leading=16)),
                 Paragraph(escape(title), self.style("sect-c", 15, bold=True, color=self.PRIMARY))]
        widths = [11 * mm, CONTENT_W - 11 * mm]
        if icon:
            cells.append(Image(icon, width=8 * mm, height=8 * mm))
            widths.append(10 * mm)
        t = Table([cells], colWidths=widths)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), self.PRIMARY),
            ("LINEBELOW", (1, 0), (1, 0), 0.8, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
        return t
