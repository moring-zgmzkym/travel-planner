"""PDF 模板公共积木：字体注册、主题化样式工厂、表格/章节、页眉页脚、图片处理。

模板 = 配色主题（类属性）+ build_story() 版式。LLM 只提供内容，不参与排版；
所有图像处理失败均回退纯色/原图/文字卡，不影响 PDF 生成本身。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Table,
                                TableStyle)

from ..config import OUTPUT_DIR
from ..models import TravelProfile
from ..planning import compute_budget

MARGIN = 16 * mm                    # 左右页边距
CONTENT_W = 210 * mm - 2 * MARGIN   # 内容区宽 178mm
CROP_DIR = OUTPUT_DIR / "images" / "crops"

_FONT = "MSYH"
_bold_name = "MSYH"             # _register_font() 后指向真粗体；注册失败回退常规体
_FONT_FILES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
]
_BOLD_FILES = [
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\msyh.ttc", 0),  # 粗体缺失时以常规体兜底（不崩、仅损失字重）
]
_registered = False


def register_font() -> None:
    global _registered, _bold_name
    if _registered:
        return
    for path, idx in _FONT_FILES:
        try:
            pdfmetrics.registerFont(TTFont(_FONT, path, subfontIndex=idx))
            break
        except Exception:  # noqa: BLE001 — 逐个字体兜底
            continue
    else:
        raise RuntimeError("未找到可用中文字体（msyh/simhei/simsun）")
    for path, idx in _BOLD_FILES:
        try:
            name = "MSYH-BD"
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            _bold_name = name
            break
        except Exception:  # noqa: BLE001
            continue
    _registered = True


def bold_font_name() -> str:
    return _bold_name


def pil_font(size: int) -> ImageFont.FreeTypeFont:
    for path, idx in _BOLD_FILES:
        try:
            return ImageFont.truetype(path, size, index=idx)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def crop_ratio(im: PILImage.Image, ratio: float, anchor: float = 0.45) -> PILImage.Image:
    """居中（可调锚点）裁剪到指定宽高比；anchor=0 顶对齐、1 底对齐。"""
    w, h = im.size
    th = int(w / ratio)
    if th <= h:
        top = int((h - th) * anchor)
        return im.crop((0, top, w, top + th))
    tw = int(h * ratio)
    left = (w - tw) // 2
    return im.crop((left, 0, left + tw, h))


def weekday(date_str: str, travel_dates: list) -> str:
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = datetime.strptime(date_str, fmt)
            if fmt == "%m-%d" and travel_dates and travel_dates[0][:4].isdigit():
                d = d.replace(year=int(travel_dates[0][:4]))
            return "周" + "一二三四五六日"[d.weekday()]
        except ValueError:
            continue
    return ""


def day_photo(day, profile: TravelProfile) -> str:
    """当天行程景点的实拍图路径（精确→包含模糊匹配；跳过示意/占位卡），无则空串。"""
    real = [i for i in profile.images
            if i.path and not any(k in (i.source or "") for k in ("示意", "非实景", "占位"))]
    if not real:
        return ""
    paths = {i.spot: i.path for i in real}
    for s in day.spots or []:
        if s in paths:
            return paths[s]
        for spot, p in paths.items():
            if s and (s in spot or spot in s):
                return p
    return ""


def draw_center(draw: ImageDraw.ImageDraw, x_center_total: int, y: int,
                text: str, font, fill) -> None:
    x = (x_center_total - draw.textlength(text, font=font)) / 2
    draw.text((x, y), text, font=font, fill=fill)


class BaseTripTemplate:
    """模板基类：主题配色（类属性覆写）+ 通用积木方法 + render() 统一渲染流程。"""

    name: str = "base"
    display_name: str = "基类（不可用）"
    description: str = ""
    scenes: str = ""
    footer_text: str = "TripMate · 多 Agent 协同旅游规划"

    # ---- 主题配色（子类覆写）----
    PRIMARY = colors.HexColor("#1a5fb4")
    PRIMARY_HEX = "#1a5fb4"
    ACCENT = colors.HexColor("#e66100")
    ACCENT_RGB = (230, 97, 0)
    SUCCESS = colors.HexColor("#1a7f37")
    SUCCESS_HEX = "#1a7f37"
    WARN = colors.HexColor("#c01c28")
    GRAY = colors.HexColor("#6b7280")
    LIGHT = colors.HexColor("#eaf1fb")     # 日程卡时段列底色
    BG_LIGHT = colors.HexColor("#f4f7fb")  # 斑马纹/chips 底色
    HAIRLINE = colors.HexColor("#d9e2ee")
    COVER_TOP_RGB = (27, 40, 74)
    COVER_BOT_RGB = (32, 54, 104)
    COVER_GOLD_RGB = (212, 175, 105)
    COVER_W, COVER_H = 1100, 1528  # ≈178×247mm（A4 内容区整页）

    # ---- 渲染入口 ----

    def render(self, profile: TravelProfile, run_id: str, canvasmaker=None) -> str:
        register_font()
        basic = profile.basic_info
        budget = compute_budget(profile, profile.draft)
        dest = re.sub(r'[\\/:*?"<>|\r\n]', "_", (basic.destination or "行程").strip()) or "行程"
        out = OUTPUT_DIR / f"行程计划_{dest}_{run_id[:8]}.pdf"
        doc = SimpleDocTemplate(str(out), pagesize=A4,
                                leftMargin=MARGIN, rightMargin=MARGIN,
                                topMargin=15 * mm, bottomMargin=18 * mm,
                                title=f"TripMate 旅行路书 · {basic.destination}")
        story = self.build_story(profile, budget)
        kw = {"canvasmaker": canvasmaker} if canvasmaker else {}
        doc.build(story, onFirstPage=self.decorate_first, onLaterPages=self.decorate_later, **kw)
        return str(out)

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        raise NotImplementedError

    # ---- 页面装饰（页脚 + 后续页顶部色带；子类可覆写换风格）----

    def decorate_first(self, canvas, _doc) -> None:
        self.draw_footer(canvas)

    def decorate_later(self, canvas, _doc) -> None:
        w, h = A4
        canvas.saveState()
        canvas.setFillColor(self.PRIMARY)
        canvas.rect(0, h - 3.2 * mm, w, 3.2 * mm, fill=1, stroke=0)
        canvas.setFillColor(self.ACCENT)
        canvas.rect(0, h - 3.2 * mm, 28 * mm, 3.2 * mm, fill=1, stroke=0)
        canvas.restoreState()
        self.draw_footer(canvas)

    def draw_footer(self, canvas) -> None:
        canvas.saveState()
        canvas.setStrokeColor(self.HAIRLINE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, 12.5 * mm, 210 * mm - MARGIN, 12.5 * mm)
        canvas.setFont(_FONT, 7.5)
        canvas.setFillColor(self.GRAY)
        canvas.drawString(MARGIN, 8.5 * mm, self.footer_text)
        canvas.drawRightString(210 * mm - MARGIN, 8.5 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    # ---- 样式与结构积木 ----

    def style(self, name: str, size: int = 10, bold: bool = False, color=colors.black,
              leading: int | None = None, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=_bold_name if bold else _FONT, fontSize=size,
                              leading=leading or int(size * 1.5),
                              textColor=color, **kw)

    def section(self, num: str, title: str) -> Table:
        """编号分区标题：品牌色编号方块 + 粗体标题 + 基线细横线。"""
        t = Table([[Paragraph(num, self.style("secnum", 12, bold=True, color=colors.white,
                                              alignment=TA_CENTER, leading=15)),
                    Paragraph(title, self.style("sect", 14, bold=True, color=self.PRIMARY))]],
                  colWidths=[9 * mm, CONTENT_W - 9 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), self.PRIMARY),
            ("LINEBELOW", (1, 0), (1, 0), 0.7, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
        return t

    def table(self, rows: list, widths: list, font_size: int = 9, right_cols: tuple = ()) -> Table:
        """表头品牌色白粗体；正文无竖线 + 斑马纹 + 行间细横线。单元格可传 Paragraph（原样使用）。"""
        body = []
        for i, row in enumerate(rows):
            cells = []
            for j, c in enumerate(row):
                if isinstance(c, Paragraph):
                    cells.append(c)
                    continue
                st = self.style(f"tc{i}-{j}", font_size)
                if j in right_cols:
                    st.alignment = 2  # TA_RIGHT
                cells.append(Paragraph(str(c), st))
            body.append(cells)
        t = Table(body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), self.PRIMARY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, self.BG_LIGHT]),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    # ---- 图片积木 ----

    def crop_43(self, path: str) -> str:
        """统一 4:3 居中裁剪（缓存于 outputs/images/crops/）；失败回退原图。"""
        # 缓存名含源路径哈希：不同目录的同名图片（下载图常见 1.jpg）不互相覆盖
        digest = hashlib.md5(str(path).encode()).hexdigest()[:6]
        out = CROP_DIR / f"{Path(path).stem}_{digest}_43.jpg"
        try:
            if not out.exists():
                CROP_DIR.mkdir(parents=True, exist_ok=True)
                with PILImage.open(path) as im:
                    crop_ratio(im.convert("RGB"), 4 / 3).save(out, quality=88)
            return str(out)
        except Exception:  # noqa: BLE001 — 裁剪失败退回原图（现状行为）
            return path

    def day_strip(self, path: str, title: str) -> str:
        """日程卡照片头条：实拍图裁 178×30mm + 左深右透渐变压暗 + 上下缘羽化 + 白色粗体标题。
        失败返回空串（调用方回退纯色日头条）。"""
        w, h = 1100, 185
        # 缓存键含模板名与强调色：不同模板同图同标题不互相覆盖（v4，2026-09-01 审查修复）
        out = CROP_DIR / ("day_" + hashlib.md5(
            f"{self.name}|{self.ACCENT_RGB}|{path}|{title}|v4".encode()).hexdigest()[:12] + ".jpg")
        try:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            with PILImage.open(path) as im:
                photo = crop_ratio(im.convert("RGB"), w / h).resize((w, h))
            grad = PILImage.new("L", (w, 1))
            for x in range(w):
                grad.putpixel((x, 0), int(190 - (190 - 30) * x / (w - 1)))
            overlay = PILImage.new("RGBA", (w, h), (13, 36, 66, 0))
            overlay.putalpha(grad.resize((w, h)))
            base = PILImage.alpha_composite(photo.convert("RGBA"), overlay)
            fade_top, fade_bot = int(h * 0.10), int(h * 0.14)
            alpha = PILImage.new("L", (w, h), 255)
            px = alpha.load()
            for y in range(fade_top):
                v = int(255 * y / fade_top)
                for x in range(w):
                    px[x, y] = v
            for y in range(fade_bot):
                v = int(255 * (fade_bot - y) / fade_bot)
                yy = h - fade_bot + y
                for x in range(w):
                    px[x, yy] = v
            base.putalpha(alpha)
            base = PILImage.alpha_composite(PILImage.new("RGBA", (w, h), (255, 255, 255, 255)), base)
            base = base.convert("RGB")
            d = ImageDraw.Draw(base)
            d.rectangle((0, 0, 9, h), fill=self.ACCENT_RGB)
            f = pil_font(40)
            ty = (h - 46) // 2
            d.text((32, ty + 2), title, font=f, fill=(10, 25, 45))   # 投影
            d.text((30, ty), title, font=f, fill=(255, 255, 255))
            base.save(out, quality=88)
            return str(out)
        except Exception:  # noqa: BLE001 — 失败回退纯色日头条
            return ""

    def image_cell(self, spot: str, path: str, source: str) -> Table:
        cells = [[Paragraph(f"{spot}", self.style("imgspot", 9.5, bold=True))]]
        try:
            p = self.crop_43(path)
            cells.append([Image(p, width=79 * mm, height=79 * mm * 3 / 4)])
        except Exception:  # noqa: BLE001 — 图片缺失降级为文字卡片（§5.2）
            cells.append([Paragraph(f"【{spot}】图片暂缺", self.style("noimg", 10, color=self.GRAY))])
        src = source if len(source) <= 96 else source[:93] + "..."
        cells.append([Paragraph(f"来源：{src}", self.style("imgsrc", 7.5, color=self.GRAY, leading=10))])
        t = Table(cells, colWidths=[85 * mm])
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, self.HAIRLINE),
            ("BACKGROUND", (0, 0), (0, 0), self.BG_LIGHT),
            ("LINEBELOW", (0, 0), (0, 0), 0.5, self.HAIRLINE),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    # ---- 美食卡片网格（柒 美食章节专用积木）----

    def food_grid(self, foods: list[str], per_row: int = 4, limit: int = 12) -> Table | None:
        """美食卡片网格：每格主题色顶条 + 浅底细描边卡 + 居中粗体菜名。

        菜名来自攻略搜索（外部文本），先做 XML 转义再进 Paragraph 标记，
        防止含 <、& 的名称导致 paraparser 崩溃（审查建议 4 的局部落实）。
        空列表返回 None（调用方走诚实回退文案）。"""
        items = [str(f).strip() for f in foods[:limit] if str(f).strip()]
        if not items:
            return None
        from xml.sax.saxutils import escape
        cell_style = self.style("foodcell", 9.5, bold=True, alignment=TA_CENTER)
        rows, cmds = [], [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        for r in range(0, len(items), per_row):
            chunk = items[r:r + per_row]
            cells = [Paragraph(escape(f), cell_style) for f in chunk]
            for ci in range(len(cells)):
                ri = r // per_row
                cmds += [("BACKGROUND", (ci, ri), (ci, ri), self.BG_LIGHT),
                         ("LINEABOVE", (ci, ri), (ci, ri), 2, self.ACCENT),
                         ("BOX", (ci, ri), (ci, ri), 0.5, self.HAIRLINE)]
            while len(cells) < per_row:
                cells.append("")
            rows.append(cells)
        t = Table(rows, colWidths=[CONTENT_W / per_row] * per_row)
        t.setStyle(TableStyle(cmds))
        return t

    # ---- 封面（主题渐变底 + 实拍条；子类可覆写）----

    def cover_base(self) -> PILImage.Image:
        """主题色竖向渐变底。"""
        base = PILImage.new("RGB", (self.COVER_W, self.COVER_H))
        px = base.load()
        for y in range(self.COVER_H):
            t = y / (self.COVER_H - 1)
            px_row = tuple(int(a + (b - a) * t) for a, b in zip(self.COVER_TOP_RGB, self.COVER_BOT_RGB))
            for x in range(self.COVER_W):
                px[x, y] = px_row
        return base

    def make_cover(self, profile: TravelProfile) -> str:
        """封面：主题底 + 底部实拍条（渐变融入）+ 大字标题 + 风格 tag + 底部信息行。

        全程回退安全：无实拍图/绘制失败均回退纯色封面。"""
        basic = profile.basic_info
        dest = basic.destination or "旅行"
        days = basic.days or "-"
        out = CROP_DIR / ("cover_" + hashlib.md5(
            f"{self.name}|{dest}|{days}|{self.COVER_TOP_RGB}|v1".encode()).hexdigest()[:12] + ".jpg")
        gold = self.COVER_GOLD_RGB
        try:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            base = self.cover_base().convert("RGBA")
            strip_h, blend = 420, 160
            for item in profile.images[:8]:
                if any(k in (item.source or "") for k in ("示意", "非实景", "占位")) or not item.path:
                    continue
                try:
                    with PILImage.open(item.path) as im:
                        photo = crop_ratio(im.convert("RGB"), self.COVER_W / strip_h).resize((self.COVER_W, strip_h))
                    mask = PILImage.new("L", (self.COVER_W, strip_h), 255)
                    mpx = mask.load()
                    for y in range(blend):
                        v = int(255 * y / blend)
                        for x in range(self.COVER_W):
                            mpx[x, y] = v
                    base.paste(photo, (0, self.COVER_H - strip_h), mask)
                    break
                except Exception:  # noqa: BLE001 — 换下一张
                    continue
            d = ImageDraw.Draw(base)
            d.rectangle((0, self.COVER_H - strip_h - 3, self.COVER_W, self.COVER_H - strip_h), fill=gold)
            d.rectangle((0, 0, 14, self.COVER_H), fill=gold)
            f_tag = pil_font(26)
            f_title = pil_font(88)
            f_sub = pil_font(34)
            f_chip = pil_font(24)
            tagline = " · ".join(basic.style or []) or "轻松出行"
            tagline = f"{basic.origin or '出发地'} 出发 · {tagline}"
            tw = d.textlength(tagline, font=f_tag)
            x0, y0 = (self.COVER_W - tw) / 2 - 34, 300
            d.rounded_rectangle((x0, y0, x0 + tw + 68, y0 + 58), radius=29,
                                outline=gold, width=2)
            draw_center(d, self.COVER_W, y0 + 12, tagline, f_tag, gold)
            draw_center(d, self.COVER_W, 470, f"{dest}", f_title, (255, 255, 255))
            sub = f"{days} 天旅行路书".replace(" -1 天", "")
            draw_center(d, self.COVER_W, 590, sub, f_sub, (222, 230, 242))
            sub2 = f"{basic.origin or ''} — {dest} · TRIPMATE ITINERARY".strip(" —")
            draw_center(d, self.COVER_W, 648, sub2, pil_font(22), (150, 168, 196))
            tags = (basic.style or [])[:4] or ["休闲"]
            chip_gap, pad = 18, 46
            widths = [d.textlength(t, font=f_chip) + pad * 2 for t in tags]
            total = sum(widths) + chip_gap * (len(tags) - 1)
            cx, cy = (self.COVER_W - total) / 2, 760
            for t, w in zip(tags, widths):
                d.rounded_rectangle((cx, cy, cx + w, cy + 52), radius=26,
                                    outline=(150, 168, 196), width=2)
                tx = cx + (w - d.textlength(t, font=f_chip)) / 2
                d.text((tx, cy + 10), t, font=f_chip, fill=(200, 212, 230))
                cx += w + chip_gap
            info_y = self.COVER_H - strip_h - 96
            f_num = pil_font(46)
            f_lab = pil_font(20)
            party = basic.party_size or detail.party_size if (detail := profile.detail_info) else basic.party_size
            stats = [(f"{days}天", f"{basic.travel_mode or '高铁'} 往返"),
                     (basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定"), "出行时间"),
                     (f"{party or '-'} 人", "同行人数"),
                     (f"¥{int(basic.budget)}" if basic.budget else "-", "预算参考")]
            col_w = self.COVER_W / len(stats)
            for i, (num, lab) in enumerate(stats):
                cx = col_w * i + col_w / 2
                d.text((cx - d.textlength(num, font=f_num) / 2, info_y), num, font=f_num, fill=gold)
                d.text((cx - d.textlength(lab, font=f_lab) / 2, info_y + 66), lab, font=f_lab,
                       fill=(180, 194, 216))
            base = base.convert("RGB")
            base.save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001 — 封面失败回退纯色
            try:
                base = self.cover_base()
                d = ImageDraw.Draw(base)
                draw_center(d, self.COVER_W, 600, f"{dest} · 旅行路书", pil_font(80), (255, 255, 255))
                base.save(out, quality=90)
                return str(out)
            except Exception:  # noqa: BLE001
                return ""
