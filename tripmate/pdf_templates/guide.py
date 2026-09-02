"""Guide 模板：慢游图文路书风（复刻高分参考 PDF 的视觉语言）。

墨蓝夜空封面（金色圆月发光 + 金描边胶囊 + 底部统计与实拍条）+ 米白纸底正文
+ 金色时间线逐日卡 + 墨蓝章节编号「壹/贰/…」与浅金双线。页码为底部居中
「n / 总页数」，用 reportlab 经典 NumberedCanvas 两遍构建技巧实现。

版式独立于 Classic，但七个章节的数据读取完全一致；诚实原则：黑板没有精确
时刻/单点票价时时间线只用「上午/下午/晚上」标签。所有 PIL/图片操作失败均
回退（跳图/纯色/文字卡），绝不让 PDF 生成崩溃。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from PIL import Image as PILImage
from PIL import ImageDraw
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                Spacer, Table, TableStyle)

from ..models import TravelProfile
from .base import (CONTENT_W, CROP_DIR, BaseTripTemplate, bold_font_name,
                   crop_ratio, day_photo, draw_center, pil_font, weekday)

GOLD_HEX = "#c9a05a"          # 金（大字号/描边/圆点）
GOLD_DEEP_HEX = "#a5793a"     # 深金（小号金字，保证米白底上的可读性）
INK_HEX = "#3a3325"           # 墨字（正文）
PAPER_HEX = "#faf6ec"         # 米白纸底
PAGE_NUM_RGB = (138, 129, 114)


class _NumberedCanvas(rl_canvas.Canvas):
    """两遍构建：showPage 缓存每页状态，save 时统一补画「n / 总页数」。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict] = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_num(total)
            super().showPage()
        super().save()

    def _draw_page_num(self, total: int) -> None:
        self.saveState()
        try:
            self.setFont("MSYH", 8.5)
        except Exception:  # noqa: BLE001 — 字体未注册回退内置字体
            self.setFont("Helvetica", 8.5)
        self.setFillColorRGB(*PAGE_NUM_RGB)
        self.drawCentredString(A4[0] / 2, 7.5 * mm, f"{self._pageNumber} / {total}")
        self.restoreState()


class GuideTemplate(BaseTripTemplate):
    name = "guide"
    display_name = "慢游图文路书"
    description = "墨蓝夜空金月封面 + 米白纸底 + 金色垂直时间线的图文路书风"
    scenes = "休闲慢游、深度体验、纪念分享"
    footer_text = "TripMate · 慢游图文路书"

    # ---- 路书配色（覆写基类主题）----
    PRIMARY = colors.HexColor("#16233f")       # 墨蓝
    PRIMARY_HEX = "#16233f"
    ACCENT = colors.HexColor(GOLD_HEX)         # 金
    ACCENT_RGB = (201, 160, 90)
    SUCCESS = colors.HexColor("#4a7c59")
    SUCCESS_HEX = "#4a7c59"
    WARN = colors.HexColor(GOLD_DEEP_HEX)      # 预警行金色（深金保证可读）
    GRAY = colors.HexColor("#8a8172")          # 暖灰小字
    INK = colors.HexColor(INK_HEX)             # 墨字正文
    LIGHT = colors.HexColor("#f3ead6")         # 浅金底（提示框/合计行）
    BG_LIGHT = colors.HexColor(PAPER_HEX)      # 米白纸底（整页背景）
    HAIRLINE = colors.HexColor("#e5d9bd")      # 细线浅金
    CARD = colors.HexColor("#fdfaf2")          # 卡片米白
    COVER_TOP_RGB = (11, 18, 40)               # 封面夜空渐变（上深下浅）
    COVER_BOT_RGB = (26, 42, 76)
    COVER_GOLD_RGB = (201, 160, 90)

    # ---- 渲染入口：基类 render + NumberedCanvas 两遍构建总页码 ----

    def render(self, profile: TravelProfile, run_id: str) -> str:
        return super().render(profile, run_id, canvasmaker=_NumberedCanvas)

    # ---- 页面装饰：全页米白底，无顶部色带（页码由 NumberedCanvas 统一绘制）----

    def _paint_paper(self, canvas) -> None:
        canvas.saveState()
        canvas.setFillColor(self.BG_LIGHT)
        w, h = A4
        canvas.rect(0, 0, w, h, fill=1, stroke=0)
        canvas.restoreState()

    def decorate_first(self, canvas, _doc) -> None:
        self._paint_paper(canvas)

    def decorate_later(self, canvas, _doc) -> None:
        self._paint_paper(canvas)

    # ---- 样式：默认墨字 ----

    def style(self, name: str, size: int = 10, bold: bool = False, color=None,
              leading: int | None = None, **kw):
        return super().style(name, size, bold,
                             self.INK if color is None else color, leading, **kw)

    # ---- PIL 夜景积木 ----

    def _night_gradient(self, w: int, h: int) -> PILImage.Image:
        """深蓝夜空竖向渐变（1px 列拉伸，避免逐像素慢循环）。"""
        col = PILImage.new("RGB", (1, h))
        px = col.load()
        top, bot = self.COVER_TOP_RGB, self.COVER_BOT_RGB
        for y in range(h):
            t = y / max(h - 1, 1)
            px[0, y] = tuple(int(a + (b - a) * t) for a, b in zip(top, bot))
        return col.resize((w, h))

    def _draw_moon(self, base: PILImage.Image, cx: int, cy: int, r: int) -> PILImage.Image:
        """金色圆月：多圈半透明圆近似径向发光 + 双层月面。"""
        gold = self.COVER_GOLD_RGB
        glow = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for i in range(26, 0, -1):
            rr = r + i * max(4, r // 12)
            a = int(6 + 50 * (1 - i / 27) ** 2)
            gd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=gold + (a,))
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(240, 217, 158, 255))
        gd.ellipse((cx - r + r // 6, cy - r + r // 9, cx + r - r // 6, cy + r - r // 5),
                   fill=(247, 232, 190, 255))
        return PILImage.alpha_composite(base, glow)

    def _draw_stars(self, d: ImageDraw.ImageDraw, w: int, h: int,
                    moon: tuple | None = None, count: int = 46) -> None:
        """确定性伪随机星点（黄金分割布点，避开月亮区域）。"""
        for i in range(count):
            sx = int(w * ((i * 0.6180339887) % 1))
            sy = int(h * 0.62 * ((i * 0.7548776662) % 1))
            if moon and (sx - moon[0]) ** 2 + (sy - moon[1]) ** 2 < (moon[2] + 150) ** 2:
                continue
            a = 70 + (i * 37) % 90
            r = 1 + i % 2
            d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=(226, 234, 248, a))

    def _fit_font(self, d: ImageDraw.ImageDraw, text: str, size: int,
                  max_w: float, min_size: int = 30, step: int = 5):
        """超长自动缩字号，返回 (font, size)。"""
        f = pil_font(size)
        while size > min_size and d.textlength(text, font=f) > max_w:
            size -= step
            f = pil_font(size)
        return f, size

    # ---- 封面：夜空 + 金月 + 胶囊 + 统计 + 实拍条 ----

    def make_cover(self, profile: TravelProfile) -> str:
        basic, detail = profile.basic_info, profile.detail_info
        dest = basic.destination or "旅行"
        days = basic.days
        nights = max(days - 1, 0) if isinstance(days, int) and days > 0 else 0
        out = CROP_DIR / ("guide_cover_" + hashlib.md5(
            f"{dest}|{days}|{basic.origin}|{'|'.join(basic.style or [])}|{basic.budget}|v1"
            .encode()).hexdigest()[:12] + ".jpg")
        gold = self.COVER_GOLD_RGB
        W, H = self.COVER_W, self.COVER_H
        try:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            base = self._night_gradient(W, H).convert("RGBA")
            moon = (W - 210, 225, 92)
            base = self._draw_moon(base, *moon)
            d = ImageDraw.Draw(base, "RGBA")
            self._draw_stars(d, W, H, moon=moon)

            # 顶部金描边胶囊：出发地 · 风格 tags
            f_tag = pil_font(27)
            tagline = f"{basic.origin or '出发地'} 出发 · {' · '.join(basic.style or []) or '慢节奏休闲'}"
            f_tag, _ = self._fit_font(d, tagline, 27, W - 180, min_size=18, step=2)
            tw = d.textlength(tagline, font=f_tag)
            x0, y0 = (W - tw) / 2 - 36, 296
            d.rounded_rectangle((x0, y0, x0 + tw + 72, y0 + 60), radius=30,
                                outline=gold, width=2)
            draw_center(d, W, y0 + 13, tagline, f_tag, gold)

            # 白色大字目的地标题（超长自动缩字号）
            f_title, tsize = self._fit_font(d, dest, 104, W - 150, min_size=44, step=6)
            title_y = 430
            draw_center(d, W, title_y, dest, f_title, (255, 255, 255))

            # 白色小字副题
            sub = (f"{days} 天 {nights} 晚旅行路书" if nights > 0
                   else (f"{days} 天旅行路书" if days else "旅行路书"))
            sub_y = title_y + tsize + 26
            draw_center(d, W, sub_y, sub, pil_font(32), (226, 233, 246))

            # 一行风格徽章胶囊（金边白字，最多 4 个）
            tags = (basic.style or [])[:4] or ["休闲慢游"]
            chip_size, pad, chip_gap, chip_h = 24, 44, 20, 52
            f_chip = pil_font(chip_size)
            widths = [d.textlength(t, font=f_chip) + pad * 2 for t in tags]
            while sum(widths) + chip_gap * (len(tags) - 1) > W - 70 and chip_size > 16:
                chip_size, pad = chip_size - 2, max(20, pad - 6)
                f_chip = pil_font(chip_size)
                widths = [d.textlength(t, font=f_chip) + pad * 2 for t in tags]
            total_w = sum(widths) + chip_gap * (len(tags) - 1)
            cx, cy = (W - total_w) / 2, sub_y + 70
            for t, cw in zip(tags, widths):
                d.rounded_rectangle((cx, cy, cx + cw, cy + chip_h), radius=chip_h // 2,
                                    outline=gold, width=2)
                d.text((cx + (cw - d.textlength(t, font=f_chip)) / 2, cy + 10),
                       t, font=f_chip, fill=(245, 247, 250))
                cx += cw + chip_gap

            # 英文风小字行
            en = f"{basic.origin or '出发'} — {dest} · A SLOW TRAVEL GUIDE"
            draw_center(d, W, cy + chip_h + 30, en, pil_font(20), (150, 163, 190))

            # 底部实拍照片条（顶缘渐变融入；无实景照片回退纯色渐变）
            strip_h, blend = 400, 170
            photo_ok = False
            for item in profile.images[:8]:
                if any(k in (item.source or "") for k in ("示意", "非实景", "占位")) or not item.path:
                    continue
                try:
                    with PILImage.open(item.path) as im:
                        photo = crop_ratio(im.convert("RGB"), W / strip_h).resize((W, strip_h))
                    gcol = PILImage.new("L", (1, blend))
                    for y in range(blend):
                        gcol.putpixel((0, y), int(255 * y / blend))
                    mask = PILImage.new("L", (W, strip_h), 255)
                    mask.paste(gcol.resize((W, blend)), (0, 0))
                    base.paste(photo, (0, H - strip_h), mask)
                    photo_ok = True
                    break
                except Exception:  # noqa: BLE001 — 换下一张
                    continue
            d = ImageDraw.Draw(base, "RGBA")

            # 4 项金色大数字统计 + 白色小标签
            party = detail.party_size or basic.party_size
            date_txt = (basic.travel_dates[0] if basic.travel_dates
                        else (basic.date_text or "日期待定"))
            stats = [
                (f"{days}天{nights}晚" if nights > 0 else f"{days or '-'}天", "行程天数"),
                (date_txt, "出行日期"),
                (f"{party}人" if party else "-", "同行人数"),
                (f"¥{int(basic.budget)}" if basic.budget else "待定", "预算参考"),
            ]
            col_w = W / len(stats)
            iy = H - strip_h - 195
            f_lab = pil_font(20)
            for i, (num, lab) in enumerate(stats):
                cxc = col_w * i + col_w / 2
                f_num, _ = self._fit_font(d, num, 46, col_w - 26, min_size=24, step=4)
                d.text((cxc - d.textlength(num, font=f_num) / 2, iy), num,
                       font=f_num, fill=gold)
                d.text((cxc - d.textlength(lab, font=f_lab) / 2, iy + 70), lab,
                       font=f_lab, fill=(232, 238, 248))

            # 金细线
            d.rectangle((60, H - strip_h - 46, W - 60, H - strip_h - 44), fill=gold)
            d.rectangle((0, H - strip_h - 3, W, H - strip_h), fill=gold + (200,))

            # 底部署名小字（有照片时垫半透明深色带保证可读）
            now = datetime.now()
            sig = f"TripMate 多 Agent 协同规划 · {now.year} 年 {now.month} 月"
            f_sig = pil_font(19)
            if photo_ok:
                d.rectangle((0, H - 88, W, H), fill=(8, 14, 30, 120))
            draw_center(d, W, H - 64, sig, f_sig, (236, 240, 250))

            base.convert("RGB").save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001 — 封面失败回退纯色夜空封面
            try:
                base = self._night_gradient(W, H)
                d = ImageDraw.Draw(base)
                draw_center(d, W, 620, f"{dest} · 慢游路书", pil_font(76), (255, 255, 255))
                draw_center(d, W, 740, "TripMate 多 Agent 协同规划", pil_font(26),
                            self.COVER_GOLD_RGB)
                CROP_DIR.mkdir(parents=True, exist_ok=True)
                base.save(out, quality=90)
                return str(out)
            except Exception:  # noqa: BLE001
                return ""

    # ---- 结尾页：深蓝渐变 + 居中 slogan + 署名 + 免责 ----

    def _make_ending(self, profile: TravelProfile) -> str:
        basic = profile.basic_info
        dest = basic.destination or "此行"
        out = CROP_DIR / ("guide_end_" + hashlib.md5(
            f"{dest}|{'|'.join(basic.style or [])}|v1".encode()).hexdigest()[:12] + ".jpg")
        W, H = self.COVER_W, self.COVER_H
        try:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            base = self._night_gradient(W, H).convert("RGBA")
            moon = (W - 170, 190, 56)
            base = self._draw_moon(base, *moon)
            d = ImageDraw.Draw(base, "RGBA")
            self._draw_stars(d, W, H, moon=moon, count=34)
            gold = self.COVER_GOLD_RGB

            slogan = f"{dest} · 把日子过慢"
            f_sl, ssize = self._fit_font(d, slogan, 84, W - 140, min_size=40, step=6)
            draw_center(d, W, 540, slogan, f_sl, (255, 255, 255))
            tags = " · ".join(basic.style or []) or "慢节奏 · 轻装出行"
            f_tags, _ = self._fit_font(d, tags, 30, W - 120, min_size=18, step=2)
            draw_center(d, W, 540 + ssize + 52, tags, f_tags, gold)
            draw_center(d, W, 860, "本路书由 TripMate 多 Agent 系统统筹汇编",
                        pil_font(26), (240, 244, 252))
            d.rectangle(((W - 220) / 2, 940, (W + 220) / 2, 943), fill=gold)
            draw_center(d, W, 990, "票价、班次信息以官方渠道实时查询为准",
                        pil_font(19), (196, 205, 222))
            draw_center(d, W, 1024, "实景图片版权归原作者", pil_font(19), (196, 205, 222))
            now = datetime.now()
            draw_center(d, W, 1092, f"{now.year} 年 {now.month} 月", pil_font(20), gold)
            base.convert("RGB").save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001 — 结尾图失败回退文字结尾（build_story 处理）
            return ""

    # ---- 章节标题：墨蓝大字编号 + 金色小方块 + 浅金双线 ----

    def section(self, num: str, title: str) -> Table:
        st = self.style
        t = Table([
            [Paragraph(f'<font color="{GOLD_HEX}">■</font>', st("gsq", 11, leading=16)),
             Paragraph(num, st("gnum", 19, bold=True, color=self.PRIMARY, leading=24)),
             Paragraph(title, st("gtitle", 13.5, bold=True, color=self.PRIMARY, leading=24))],
            ["", "", ""],
        ], colWidths=[7 * mm, 15 * mm, CONTENT_W - 22 * mm],
            rowHeights=[None, 2.6])
        t.setStyle(TableStyle([
            ("SPAN", (0, 1), (-1, 1)),
            ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.4, self.ACCENT),      # 粗金线
            ("LINEBELOW", (0, 1), (-1, 1), 0.5, self.HAIRLINE),    # 细浅金线
            ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
            ("TOPPADDING", (0, 1), (-1, 1), 0), ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("LEFTPADDING", (1, 0), (1, 0), 3),
            ("LEFTPADDING", (2, 0), (2, 0), 7),
        ]))
        return t

    # ---- 图片卡：实景速览（横图 + 金色景点名 + 浅灰来源）----

    def _photo_card(self, spot: str, path: str, source: str) -> Table:
        st = self.style
        cells: list = []
        try:
            p = self.crop_43(path)
            cells.append([Image(p, width=76 * mm, height=76 * mm * 3 / 4)])
        except Exception:  # noqa: BLE001 — 图片缺失降级为文字卡
            cells.append([Paragraph(f"【{spot}】图片暂缺", st("gnoimg", 9, color=self.GRAY))])
        cells.append([Paragraph(spot, st("gspot", 10, bold=True, color=self.ACCENT))])
        src = source if len(source) <= 80 else source[:77] + "..."
        cells.append([Paragraph(f"来源：{src}", st("gsrc", 7.5, color=self.GRAY, leading=10))])
        t = Table(cells, colWidths=[84 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.CARD),
            ("BOX", (0, 0), (-1, -1), 0.6, self.HAIRLINE),
            ("LINEABOVE", (0, 1), (-1, 1), 0.5, self.HAIRLINE),
            ("ALIGN", (0, 0), (0, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    # ---- 逐日：金色日期头条 + 照片条 + 垂直时间线 ----

    def _day_block(self, i: int, day, profile: TravelProfile) -> list:
        st = self.style
        basic = profile.basic_info
        wk = weekday(day.date, basic.travel_dates)
        label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
        flow: list = []
        bar = Table([[Paragraph(label, st("gday", 11.5, bold=True, color=self.PRIMARY))]],
                    colWidths=[CONTENT_W])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.ACCENT),
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, self.WARN),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ]))
        flow.append(bar)
        photo = day_photo(day, profile)
        if photo:
            spots_txt = " · ".join(day.spots[:3])[:20] or label
            strip = self.day_strip(photo, spots_txt)
            if strip:
                flow.append(Image(strip, width=CONTENT_W, height=CONTENT_W * 185 / 1100))
        dot_style = st("gdot", 15, color=self.ACCENT, alignment=TA_CENTER, leading=18)
        rows = []
        for slot, text in (("上午", day.morning), ("下午", day.afternoon), ("晚上", day.evening)):
            content = (f'<font face="{bold_font_name()}" color="{GOLD_DEEP_HEX}">{slot}</font>'
                       f'<font color="{GOLD_DEEP_HEX}">　</font>{text or "—"}')
            rows.append([Paragraph("●", dot_style), Paragraph(content, st(f"gd{slot}", 9.5, leading=14))])
        t = Table(rows, colWidths=[7 * mm, CONTENT_W - 7 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (0, -1), "TOP"),
            ("LINEAFTER", (0, 0), (0, -1), 0.9, self.HAIRLINE),        # 时间线竖细线
            ("BACKGROUND", (1, 0), (1, -1), self.CARD),
            ("BOX", (1, 0), (1, -1), 0.6, self.HAIRLINE),
            ("LINEBELOW", (1, 0), (1, -2), 0.5, self.HAIRLINE),
            ("TOPPADDING", (0, 0), (0, -1), 7), ("BOTTOMPADDING", (0, 0), (0, -1), 4),
            ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 0),
            ("TOPPADDING", (1, 0), (1, -1), 7), ("BOTTOMPADDING", (1, 0), (1, -1), 7),
            ("LEFTPADDING", (1, 0), (1, -1), 9), ("RIGHTPADDING", (1, 0), (1, -1), 7),
        ]))
        flow.append(t)
        return flow

    # ---- 订单横卡 ----

    def _order_card(self, kind: str, name: str, info: str, reason: str,
                    selected: bool, link_html: str) -> Table:
        st = self.style
        badge_txt = kind
        badge_bg = self.PRIMARY if kind == "车票" else self.ACCENT
        badge_color = colors.white if kind == "车票" else self.PRIMARY
        badge = Paragraph(badge_txt, st(f"ob{kind}", 9.5, bold=True, color=badge_color,
                                        alignment=TA_CENTER, leading=13))
        mid = [Paragraph(name, st(f"on{name[:6]}", 10.5, bold=True, color=self.PRIMARY, leading=15)),
               Paragraph(info, st(f"oi{name[:6]}", 8.3, color=self.GRAY, leading=12))]
        tag = (f'<font face="{bold_font_name()}" color="{GOLD_DEEP_HEX}">√ 已勾选</font>　'
               if selected else "")
        right = [Paragraph(tag + (reason or "—"), st(f"or{name[:6]}", 8.5, leading=12.5)),
                 Paragraph(link_html, st(f"ol{name[:6]}", 8.5, leading=12.5))]
        t = Table([[badge, mid, right]], colWidths=[15 * mm, 68 * mm, CONTENT_W - 83 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), badge_bg),
            ("BACKGROUND", (1, 0), (-1, -1), self.CARD),
            ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE),
            ("LINEBEFORE", (2, 0), (2, 0), 0.5, self.HAIRLINE),
            ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
            ("VALIGN", (1, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (1, 0), (-1, -1), 8), ("RIGHTPADDING", (1, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (0, -1), 2), ("RIGHTPADDING", (0, 0), (0, -1), 2),
        ]))
        return t

    # ---- 酒店横卡 ----

    def _hotel_card(self, h) -> Table:
        st = self.style
        left: list = []
        if h.image_path:  # 空路径直接走文字卡
            try:
                left.append([Image(self.crop_43(h.image_path), width=56 * mm, height=42 * mm)])
            except Exception:  # noqa: BLE001 — 图缺退文字
                left.append([Paragraph("酒店图片暂缺", st(f"gnh{h.name[:5]}", 9, color=self.GRAY))])
        else:
            left.append([Paragraph("酒店图片暂缺", st(f"gnh2{h.name[:5]}", 9, color=self.GRAY))])
        meta = (f'<font color="{GOLD_DEEP_HEX}">★ {h.rating:g}</font>｜'
                f'{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km'
                + (f'　<font face="{bold_font_name()}" color="{GOLD_DEEP_HEX}">√ 已勾选</font>'
                   if h.selected else ""))
        right = [
            [Paragraph(h.name, st(f"ghn{h.name[:5]}", 12, bold=True, color=self.PRIMARY, leading=16))],
            [Paragraph(meta, st(f"ghm{h.name[:5]}", 9, leading=13))],
            [Paragraph(f"网络评价：{h.review_digest}" if h.review_digest else "网络评价：暂无",
                       st(f"ghr{h.name[:5]}", 8.5, color=self.GRAY, leading=12))],
        ]
        card = Table([[left, right]], colWidths=[64 * mm, CONTENT_W - 64 * mm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.CARD),
            ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, self.ACCENT),          # 卡底浅金线
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return card

    # ---- 七章正文（数据读取与 Classic 一致，版式为路书重设计）----

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        st = self.style
        basic, detail = profile.basic_info, profile.detail_info
        bf = bold_font_name()
        story: list = []

        # ---- 封面页（夜空金月路书封面；绘制失败自动回退纯色封面）----
        cover = self.make_cover(profile)
        if cover:
            story.append(Image(cover, width=CONTENT_W,
                               height=CONTENT_W * self.COVER_H / self.COVER_W))
            story.append(PageBreak())

        # ---- 公共派生数据（与 Classic 相同读取方式）----
        party = detail.party_size or basic.party_size or 1
        d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
        d1 = basic.travel_dates[-1] if basic.travel_dates else ""
        dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
        budget_txt = f"¥{basic.budget:g}" if basic.budget else "-"
        hotel_pref = detail.hotel.location_pref or "-"
        must = "、".join(detail.must_visit[:6]) or "-"
        styles = " / ".join(basic.style or []) or "休闲"
        rhythm = {"快": "4-5 个景点/天", "慢": "1-2 个景点/天"}.get(detail.pace or "", "2-3 个景点/天")

        # ---- 壹 行程总览（键值表 + 4 张原则卡）----
        story.append(self.section("壹", "行程总览"))
        story.append(Spacer(1, 5))
        overview_rows = [
            ["目的地", basic.destination or "-", "出发地", basic.origin or "-"],
            ["出行时间", dates_txt, "行程天数", f"{basic.days or '-'} 天"],
            ["交通方式", basic.travel_mode or "-", "同行人数", f"{party} 人"],
            ["预算参考", f"{budget_txt}（上限 {basic.budget_max:g}）" if basic.budget_max else budget_txt,
             "旅行风格", styles],
            ["酒店偏好", hotel_pref, "必去景点", must],
            ["游览节奏", rhythm, "数据说明", "车票/酒店以订单清单为准"],
        ]
        info_cells = []
        for row in overview_rows:
            info_cells.append([
                Paragraph(row[0], st("gk1", 9, bold=True, color=self.ACCENT)),
                Paragraph(str(row[1]), st("gv1", 9)),
                Paragraph(row[2], st("gk2", 9, bold=True, color=self.ACCENT)),
                Paragraph(str(row[3]), st("gv2", 9)),
            ])
        t = Table(info_cells, colWidths=[20 * mm, 69 * mm, 20 * mm, 69 * mm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        if profile.draft:  # 每日节奏一行速览（真实草稿数据）
            rhythm_line = " ｜ ".join(
                f"D{i + 1} {'→'.join(d.spots[:3]) or (d.morning or '')[:12]}"
                for i, d in enumerate(profile.draft.days))
            story.append(Spacer(1, 4))
            story.append(Paragraph(f"每日节奏：{rhythm_line}", st("grhythm", 8.5, color=self.GRAY)))
        story.append(Spacer(1, 8))

        # 4 张原则卡（内容全部由真实数据派生）
        pr = detail.hotel.price_range
        hotel_card_txt = hotel_pref if hotel_pref != "-" else "位置待定"
        if len(pr) == 2:
            hotel_card_txt += f" · {pr[0]:g}-{pr[1]:g} 元/晚"
        budget_card_txt = (f"全团口径 {budget_txt}，含 8% 备用金"
                           + (f"，上限 ¥{basic.budget_max:g}" if basic.budget_max else ""))
        cards = [
            ("缓 · 节奏", f"每天 {rhythm}，张弛有度，留足拍照与休息时间"),
            ("居 · 住宿", hotel_card_txt),
            ("必 · 必去", must if must != "-" else "暂无指定，由 Agent 按风格推荐"),
            ("省 · 预算", budget_card_txt),
        ]
        inner = []
        for j, (ct, cb) in enumerate(cards):
            c = Table([[Paragraph(f'<font face="{bf}">{ct}</font>',
                                  st(f"gct{j}", 10, color=self.ACCENT, leading=14))],
                       [Paragraph(cb, st(f"gcb{j}", 8.3, leading=12.5))]],
                      colWidths=[40.5 * mm])
            c.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), self.CARD),
                ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE),
                ("LINEBELOW", (0, 0), (0, 0), 0.5, self.HAIRLINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            inner.append(c)
        grid = Table([inner], colWidths=[44.5 * mm] * 4)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(grid)
        story.append(Spacer(1, 10))

        # ---- 贰 订单清单（每条订单一张横卡 + 金底合计行；每章另起一页）----
        story.append(PageBreak())
        story.append(self.section("贰", "订单清单（Agent 已按您的要求筛选勾选）"))
        story.append(Spacer(1, 5))

        def _link_cell(url: str, label: str) -> str:
            """友好锚文本超链接：原始长 URL 会把窄列按字符硬换行撑爆版面。"""
            return f'<a href="{url}" color="{self.PRIMARY_HEX}"><u>{label}</u></a>' if url else "—"

        for tk in profile.tickets:
            info = f"{tk.depart_time} 出发 / {tk.arrive_time} 到达 · {tk.price:g} 元 · {tk.source}"
            flow = [self._order_card("车票", tk.train_no, info, tk.reason, tk.selected,
                                     _link_cell(tk.link, "12306 购票")), Spacer(1, 5)]
            story.append(KeepTogether(flow))
        for h in profile.hotels:
            info = (f"{h.price_per_night:g} 元/晚 · 距地标 {h.distance_km:g}km · "
                    f"评分 {h.rating:g} · {h.source}")
            flow = [self._order_card("酒店", h.name, info, h.reason, h.selected,
                                     _link_cell(h.link, "携程订房")), Spacer(1, 5)]
            story.append(KeepTogether(flow))
        if not profile.tickets and not profile.hotels:
            story.append(Paragraph("暂无候选订单（车票/酒店通道未返回）",
                                   st("gonoorder", 9, color=self.GRAY)))
        total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                          for tk in profile.tickets if tk.selected)
        total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                           for h in profile.hotels if h.selected)
        tot = Table([[Paragraph(f'<font face="{bf}">已勾选订单合计（交通往返 + 住宿）</font>',
                                st("gtot1", 9.5, color=self.PRIMARY)),
                      Paragraph(f'<font face="{bf}">约 {total_order:g} 元</font>',
                                st("gtot2", 10, color=self.PRIMARY, alignment=2))]],
                    colWidths=[CONTENT_W - 42 * mm, 42 * mm])
        tot.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.append(tot)
        ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
        if ref_notes:
            story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                                   st("grefnote", 8.5, color=self.WARN, spaceBefore=4, spaceAfter=4)))
        story.append(Spacer(1, 10))

        # ---- 叁 逐日行程（垂直时间线；无精确时刻，只用上午/下午/晚上标签）----
        story.append(PageBreak())
        story.append(self.section("叁", "逐日行程"))
        story.append(Spacer(1, 5))
        if profile.draft:
            for i, day in enumerate(profile.draft.days):
                block = self._day_block(i, day, profile)
                story.append(KeepTogether(block + [Spacer(1, 8)]))
        else:
            story.append(Paragraph("暂无逐日草稿数据", st("gnodraft", 9, color=self.GRAY)))
        story.append(Spacer(1, 4))

        # ---- 肆 预算核算（米白卡表格 + 金字墨蓝表头 + 金色占用条）----
        story.append(PageBreak())
        story.append(self.section("肆", "预算核算（全团口径）"))
        story.append(Spacer(1, 5))
        hdr = lambda s: Paragraph(f'<font face="{bf}" color="{GOLD_HEX}">{s}</font>',  # noqa: E731
                                  st("gbh", 9, color=self.ACCENT))
        b_rows = [[hdr("项目"), hdr("说明"), hdr("金额（元）")]]
        for k, r in enumerate(budget["items"]):
            b_rows.append([Paragraph(r["item"], st(f"gbi{k}", 9)),
                           Paragraph(r["note"], st(f"gbn{k}", 8.5, color=self.GRAY)),
                           Paragraph(f"{r['amount']:g}", st(f"gba{k}", 9, alignment=2))])
        note = (f"预算 {budget['budget']:g}｜上限 {budget['budget_max']:g}｜"
                f"占用 {budget['occupancy']:.0%}") if budget["occupancy"] else "—"
        b_rows.append([Paragraph(f'<font face="{bf}">合计</font>', st("gbsum", 9.5)),
                       Paragraph(note, st("gbsumn", 8.5)),
                       Paragraph(f'<font face="{bf}">{budget["total"]:g}</font>',
                                 st("gbsuma", 9.5, alignment=2))])
        t = Table(b_rows, colWidths=[26 * mm, 112 * mm, 40 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), self.PRIMARY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [self.CARD, self.BG_LIGHT]),
            ("BACKGROUND", (0, -1), (-1, -1), self.LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, self.HAIRLINE),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, self.ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        occ = max(0.0, min(float(budget["occupancy"] or 0), 1.0))
        if occ > 0:
            bar = Table([["", ""]], colWidths=[CONTENT_W * occ, CONTENT_W * (1 - occ)],
                        rowHeights=[3.2 * mm])
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), self.ACCENT if occ < 0.9 else self.WARN),
                ("BACKGROUND", (1, 0), (1, 0), self.LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.4, self.HAIRLINE),
            ]))
            story.append(Spacer(1, 4))
            story.append(KeepTogether([
                bar,
                Paragraph(f"预算占用 {float(budget['occupancy'] or 0):.0%}（合计 {budget['total']:g} 元）",
                          st("gbarcap", 8, color=self.GRAY, spaceBefore=2)),
            ]))
        for k, w in enumerate(budget["warnings"]):
            story.append(Paragraph(f'<font color="{GOLD_DEEP_HEX}">※</font> ' + w,
                                   st(f"gbw{k}", 9.5, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 10))

        # ---- 伍 实景速览（每行 2 张横图 + 金色景点名 + 来源小字）----
        if profile.images:
            story.append(PageBreak())
            story.append(self.section("伍", "实景速览（均标注来源）"))
            story.append(Spacer(1, 5))
            imgs = profile.images[:8]
            for r in range(0, len(imgs), 2):
                row_cells = [self._photo_card(img.spot, img.path, img.source)
                             for img in imgs[r:r + 2]]
                if len(row_cells) == 1:
                    row_cells.append("")
                t = Table([row_cells], colWidths=[89 * mm, 89 * mm])
                t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                story.append(t)
                story.append(Spacer(1, 5))
            story.append(Spacer(1, 4))

        # ---- 陆 住宿方案（每家酒店一张横向卡：左图右文）----
        if profile.hotels:
            ordered = ([h for h in profile.hotels if h.selected]
                       + [h for h in profile.hotels if not h.selected])[:3]
            hotel_flow: list = []
            for h in ordered:
                hotel_flow.append(self._hotel_card(h))
                hotel_flow.append(Spacer(1, 6))
            # 标题与首卡绑定，避免章节头孤立在页底；本章另起一页
            story.append(PageBreak())
            story.append(KeepTogether([self.section("陆", "住宿方案（实景 + 网络评价）"),
                                       Spacer(1, 5), hotel_flow[0], hotel_flow[1]]))
            story.extend(hotel_flow[2:])
            story.append(Spacer(1, 4))

        # ---- 柒 美食与注意事项 ----
        story.append(PageBreak())
        story.append(self.section("柒", "美食与注意事项"))
        story.append(Spacer(1, 5))
        story.append(Paragraph("美食寻味", st("gfoodt", 11, bold=True, color=self.ACCENT)))
        foods: list[str] = []
        for g in profile.guide_digest:
            foods += g.foods
        seen_f: set[str] = set()
        uniq_foods = [f for f in foods if not (f in seen_f or seen_f.add(f))]
        foods_text = "、".join(uniq_foods[:12])
        if not foods_text:
            # 结构化字段为空时回退展示目的地相关搜索结果标题（诚实标注）
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods_text = ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles)) if titles \
                else "暂无（攻略通道未返回）"
        story.append(Paragraph(foods_text, st("gfoods", 9.5, spaceBefore=3)))
        story.append(Spacer(1, 8))

        story.append(Paragraph("注意事项", st("gwarnt", 11, bold=True, color=self.ACCENT)))
        story.append(Spacer(1, 3))
        warns: list[str] = []
        for g in profile.guide_digest:
            warns += g.warnings
        seen_w: set[str] = set()
        warn_items = [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]
        warn_rows = [[Paragraph("• " + w, st(f"gwi{k}", 9.5, leading=14))]
                     for k, w in enumerate(warn_items)]
        if not warn_rows:
            warn_rows = [[Paragraph("暂无（攻略通道未返回注意事项）", st("gwi0", 9, color=self.GRAY))]]
        wt = Table(warn_rows, colWidths=[CONTENT_W])
        wt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.8, self.HAIRLINE),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(wt)
        for i, g in enumerate(profile.guide_digest[:3]):
            story.append(Paragraph(f"攻略来源[{i + 1}]：{g.source_name} {g.source_url}（抓取 {g.fetched_at}）",
                                   st(f"ggsrc{i}", 8, color=self.GRAY, spaceBefore=3)))
        if profile.weather.get("days"):
            wline = "；".join(f"{d['date']} {d['day_text']} {d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃"
                             for d in profile.weather["days"])
            story.append(Paragraph(f"天气参考（{profile.weather.get('source', '')}）：{wline}",
                                   st("ggwx", 8, color=self.GRAY, spaceBefore=3)))
        if basic.defaults_applied:
            story.append(Paragraph("默认值说明：以下字段由系统按默认值补齐——" + "、".join(basic.defaults_applied),
                                   st("ggdef", 8, color=self.WARN, spaceBefore=3)))

        # ---- 结尾页（深蓝渐变整页图；失败回退米白底文字结尾）----
        story.append(PageBreak())
        end_img = self._make_ending(profile)
        if end_img:
            story.append(Image(end_img, width=CONTENT_W,
                               height=CONTENT_W * self.COVER_H / self.COVER_W))
        else:
            now = datetime.now()
            story.append(Spacer(1, 90))
            story.append(Paragraph(f"{basic.destination or '此行'} · 把日子过慢",
                                   st("gend1", 22, bold=True, color=self.PRIMARY,
                                      alignment=TA_CENTER)))
            story.append(Paragraph(" · ".join(basic.style or []) or "慢节奏 · 轻装出行",
                                   st("gend2", 12, color=self.ACCENT, alignment=TA_CENTER,
                                      spaceBefore=10)))
            story.append(Paragraph("本路书由 TripMate 多 Agent 系统统筹汇编",
                                   st("gend3", 11, alignment=TA_CENTER, spaceBefore=24)))
            story.append(Paragraph("票价、班次信息以官方渠道实时查询为准；实景图片版权归原作者",
                                   st("gend4", 8, color=self.GRAY, alignment=TA_CENTER,
                                      spaceBefore=14)))
            story.append(Paragraph(f"{now.year} 年 {now.month} 月",
                                   st("gend5", 9, color=self.ACCENT, alignment=TA_CENTER,
                                      spaceBefore=8)))
        return story
