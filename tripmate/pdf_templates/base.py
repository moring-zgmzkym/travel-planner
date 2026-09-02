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
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from xml.sax.saxutils import escape as xml_escape

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

    # ---- 美食地图（左图右文卡；数据全部来自检索原文的确定性提取，不编造）----

    _SENT_SPLIT = re.compile(r"[。；;！!\n]")

    def _price_in(self, text: str) -> str:
        m = re.search(r"人均\s*[¥￥]?\s*(\d{1,4}(?:\s*[–—~-]\s*\d{1,4})?)", text)
        if m:
            return f"人均 ¥{m.group(1).replace(' ', '')}"
        m = re.search(r"[¥￥]\s*(\d{1,4}(?:\s*[–—~-]\s*\d{1,4})?)", text)
        return f"¥{m.group(1).replace(' ', '')}" if m else ""

    def extract_food_entries(self, profile: TravelProfile) -> list[dict]:
        """从黑板确定性提取美食条目（渲染层后处理，不新增外部调用）。

        两级来源：① guide_digest.foods 菜名 → 在 raw_answer 原文找含该菜名的句子
        作描述、就近提取 ¥ 金额作人均；② foods 为空时 → 从美食相关来源的
        raw_answer 摘取含 人均/¥/必吃/推荐/招牌 的原句作线索卡。全部为检索原文
        内容并标注来源，宁缺毋滥（提不出就返回空，调用方回退 food_grid）。"""
        entries: list[dict] = []
        seen: set[str] = set()
        for g in profile.guide_digest:
            if not g.foods:
                continue
            sents = [s.strip() for s in self._SENT_SPLIT.split(g.raw_answer or "") if s.strip()]
            for f in g.foods[:8]:
                f = str(f).strip()
                if not f or f in seen:
                    continue
                hit = next((s for s in sents if f in s), "")
                seen.add(f)
                entries.append({"name": f, "desc": hit[:96], "price": self._price_in(hit),
                                "source": g.source_name})
        if not entries:
            for g in profile.guide_digest:
                if "美食" not in (g.source_name or "") and not any("美食" in (t or "") for t in g.raw_titles):
                    continue
                sents = [s.strip() for s in self._SENT_SPLIT.split(g.raw_answer or "") if s.strip()]
                for s in sents:
                    if not re.search(r"人均|[¥￥]\d|必吃|推荐|招牌", s) or len(s) < 8:
                        continue
                    m = re.search(r"[「『《“\"']([^」』》”\"']{2,14})[」』》”\"']", s)
                    name = (m.group(1) if m else re.split(r"[，,、：:]", s)[0][:14]).strip()
                    if name in seen:
                        continue
                    seen.add(name)
                    entries.append({"name": name, "desc": s[:96], "price": self._price_in(s),
                                    "source": g.source_name})
                    if len(entries) >= 6:
                        break
                if len(entries) >= 6:
                    break
        return entries[:6]

    def food_photo(self, name: str, profile: TravelProfile) -> str:
        """按名称与实拍图分区模糊匹配美食图；无匹配返回空串（走占位卡）。"""
        for img in profile.images:
            spot = (img.spot or "").strip()
            if img.path and name and spot and (name in spot or spot in name):
                try:
                    return self.crop_43(img.path)
                except Exception:  # noqa: BLE001
                    return ""
        return ""

    def food_placeholder(self, name: str) -> str:
        """美食占位卡（PIL）：主题渐变底 + 菜名首字大字 + 「示意」角标（诚实标注非实拍）。"""
        w, h = 520, 390
        key = hashlib.md5(f"{self.name}|{self.PRIMARY_HEX}|{name}|v1".encode()).hexdigest()[:12]
        out = CROP_DIR / f"foodph_{key}.jpg"
        try:
            if not out.exists():
                CROP_DIR.mkdir(parents=True, exist_ok=True)
                base = PILImage.new("RGB", (w, h))
                px = base.load()
                for y in range(h):
                    t = y / (h - 1)
                    row = tuple(int(a + (b - a) * t) for a, b in zip(self.COVER_TOP_RGB, self.COVER_BOT_RGB))
                    for x in range(w):
                        px[x, y] = row
                d = ImageDraw.Draw(base)
                ch = (name or "美食")[0]
                draw_center(d, w, (h - 170) // 2 - 16, ch, pil_font(170), (255, 255, 255))
                d.rounded_rectangle((w - 122, h - 60, w - 18, h - 18), radius=10,
                                    outline=(255, 255, 255), width=2)
                d.text((w - 104, h - 52), "示意", font=pil_font(26), fill=(255, 255, 255))
                base.save(out, quality=88)
            return str(out)
        except Exception:  # noqa: BLE001 — 占位卡失败走文字降级
            return ""

    def food_card(self, entry: dict, profile: TravelProfile) -> Table:
        """左图右文美食卡（复刻参考版式）：左 52mm 图（实拍或示意占位卡），
        右侧菜名大字 + 人均金标 + 原文描述 + 来源小字。"""
        name = str(entry.get("name") or "美食推荐")[:20]
        photo = self.food_photo(name, profile) or self.food_placeholder(name)
        left = []
        if photo:
            try:
                left.append([Image(photo, width=52 * mm, height=39 * mm)])
            except Exception:  # noqa: BLE001
                left.append([Paragraph("图片暂缺", self.style("fnoimg", 9, color=self.GRAY))])
        else:
            left.append([Paragraph("图片暂缺", self.style("fnoimg", 9, color=self.GRAY))])
        right: list = [[Paragraph(xml_escape(name), self.style("fname", 12, bold=True,
                                                               color=self.PRIMARY))]]
        if entry.get("price"):
            right.append([Paragraph(xml_escape(entry["price"]),
                                    self.style("fprice", 9.5, bold=True, color=self.ACCENT))])
        if entry.get("desc"):
            right.append([Paragraph(xml_escape(entry["desc"]),
                                    self.style("fdesc", 9, leading=13, spaceBefore=2))])
        right.append([Paragraph(f"来源：{xml_escape(str(entry.get('source') or ''))}",
                                self.style("fsrc", 7.5, color=self.GRAY, leading=10))])
        card = Table([[left, right]], colWidths=[56 * mm, CONTENT_W - 56 * mm])
        card.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, self.HAIRLINE),
            ("LINEABOVE", (0, 0), (-1, 0), 2, self.ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return card

    def food_map(self, profile: TravelProfile) -> list:
        """美食地图区块：提取到条目 → 左图右文卡列表；提不出返回 []（调用方回退 food_grid）。"""
        blocks = []
        for e in self.extract_food_entries(profile):
            blocks.append(KeepTogether([self.food_card(e, profile), Spacer(1, 4)]))
        return blocks

    # ---- 预算双图表（PIL 绘制，无新依赖）----

    def _chart_rgb(self, c) -> tuple:
        return (int(c.red * 255), int(c.green * 255), int(c.blue * 255))

    def budget_charts(self, budget: dict, party: int, days: int,
                      dates: list | None = None) -> str:
        """左「人均实花构成」环图 + 右「分日支出节奏」柱状图（复刻参考版式）。

        分日节奏为预算口径的确定性估算：交通计 D1、住宿按晚分摊（首晚计 D1）、
        其余科目按天均摊——图内明示「按预算口径估算」，不冒充实测数据。
        返回图片路径；数据不足/绘制失败返回空串（调用方跳过图表）。"""
        items = [r for r in (budget.get("items") or []) if r.get("amount")]
        party = max(int(party or 1), 1)
        days = max(int(days or 1), 1)
        if not items:
            return ""
        key = hashlib.md5((f"{self.name}|{party}|{days}|" +
                           "|".join(f"{r['item']}:{r['amount']:g}" for r in items)).encode()
                          ).hexdigest()[:12]
        out = CROP_DIR / f"budget_chart_{key}.jpg"
        try:
            if out.exists():
                return str(out)
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            W, H = 1500, 560
            img = PILImage.new("RGB", (W, H), (255, 255, 255))
            d = ImageDraw.Draw(img)
            palette = [self._chart_rgb(c) for c in (self.PRIMARY, self.ACCENT, self.SUCCESS,
                                                    self.WARN, colors.HexColor("#7c6bb0"),
                                                    self.GRAY)]
            f_t, f_n, f_s = pil_font(30), pil_font(24), pil_font(20)
            ink, sub = (40, 40, 46), (110, 110, 118)
            # -- 左：人均构成环图 --
            d.text((40, 24), "人均实花构成", font=f_t, fill=ink)
            per = [(str(r["item"]), float(r["amount"]) / party) for r in items]
            per_total = sum(v for _, v in per) or 1.0
            cx, cy, R, r0 = 255, 310, 185, 103
            start = -90.0
            for i, (_lab, v) in enumerate(per):
                sweep = max(v / per_total * 360.0, 0.5)
                d.pieslice([cx - R, cy - R, cx + R, cy + R], start, start + sweep,
                           fill=palette[i % len(palette)])
                start += sweep
            d.ellipse([cx - r0, cy - r0, cx + r0, cy + r0], fill=(255, 255, 255))
            amt = f"≈ ¥{per_total:,.0f}"
            d.text((cx - d.textlength("人均实花", font=f_s) / 2, cy - 44), "人均实花",
                   font=f_s, fill=sub)
            d.text((cx - d.textlength(amt, font=f_t) / 2, cy - 8), amt, font=f_t, fill=(30, 30, 36))
            lx, ly = 480, 160
            for i, (lab, v) in enumerate(per[:6]):
                d.rectangle([lx, ly + 4, lx + 22, ly + 26], fill=palette[i % len(palette)])
                d.text((lx + 34, ly), f"{lab}  ¥{v:,.0f} · {v / per_total:.0%}",
                       font=f_n, fill=(60, 60, 66))
                ly += 50
            # -- 右：分日支出柱状图 --
            bx0 = 850
            d.text((bx0, 24), "分日支出节奏（按预算口径估算）", font=f_t, fill=ink)
            per_day = [0.0] * days
            other = 0.0
            nights = max(days - 1, 0)
            for r in items:
                lab, amt_v = str(r["item"]), float(r["amount"])
                if "交通" in lab:
                    per_day[0] += amt_v
                elif "住宿" in lab and nights:
                    for n in range(nights):
                        per_day[min(n, days - 1)] += amt_v / nights
                else:
                    other += amt_v
            for i in range(days):
                per_day[i] += other / days
            max_v = max(per_day) or 1.0
            plot_x0, plot_x1, plot_y0, plot_y1 = bx0 + 20, W - 60, 130, 455
            for i, v in enumerate(per_day):
                cx_i = plot_x0 + (plot_x1 - plot_x0) * (i + 0.5) / days
                bw = min((plot_x1 - plot_x0) / days * 0.52, 120)
                top = plot_y1 - (plot_y1 - plot_y0) * v / max_v
                d.rectangle([cx_i - bw / 2, top, cx_i + bw / 2, plot_y1],
                            fill=palette[i % len(palette)])
                vt = f"¥{v:,.0f}"
                d.text((cx_i - d.textlength(vt, font=f_n) / 2, top - 36), vt,
                       font=f_n, fill=(50, 50, 56))
                lab = f"D{i + 1}"
                if dates and i < len(dates) and dates[i]:
                    lab += f" · {str(dates[i])[5:]}"
                d.text((cx_i - d.textlength(lab, font=f_s) / 2, plot_y1 + 14), lab,
                       font=f_s, fill=(90, 90, 96))
            d.line([plot_x0 - 14, plot_y1, plot_x1, plot_y1], fill=(180, 180, 186), width=2)
            note = ("大头在 D1（往返交通+首晚房费），其余科目按天均摊" if days > 1
                    else "单日行程：全部支出计入 D1")
            d.text((bx0, plot_y1 + 56), note, font=f_s, fill=(130, 130, 136))
            img.save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001 — 图表失败不影响 PDF 主体
            return ""

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
