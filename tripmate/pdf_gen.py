"""PDF 生成（§4.5）：reportlab（Windows 无 GTK，weasyprint 不可用，按企划书备选方案切换）。

视觉层为确定性模板（LLM 只提供内容，不参与排版）：旅行手册风——实拍图横幅首屏、
编号分区、卡片化日程、无竖线斑马表格。所有图像处理失败均回退纯色/原图/文字卡，
不影响 PDF 生成本身。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
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
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

from .config import OUTPUT_DIR
from .models import TravelProfile
from .planning import compute_budget

PRIMARY = colors.HexColor("#1a5fb4")
PRIMARY_HEX = "#1a5fb4"
ACCENT = colors.HexColor("#e66100")
SUCCESS = colors.HexColor("#1a7f37")
SUCCESS_HEX = "#1a7f37"
WARN = colors.HexColor("#c01c28")
GRAY = colors.HexColor("#6b7280")
LIGHT = colors.HexColor("#eaf1fb")     # 日程卡时段列底色
BG_LIGHT = colors.HexColor("#f4f7fb")  # 斑马纹/chips 底色
HAIRLINE = colors.HexColor("#d9e2ee")

_M = 16 * mm                    # 左右页边距
_CONTENT_W = 210 * mm - 2 * _M  # 内容区宽 178mm

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
CROP_DIR = OUTPUT_DIR / "images" / "crops"


def _register_font() -> None:
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


def _style(name: str, size: int = 10, bold: bool = False, color=colors.black,
           leading: int | None = None, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=_bold_name if bold else _FONT, fontSize=size,
                          leading=leading or int(size * 1.5),
                          textColor=color, **kw)


# ---- 页面装饰（页脚 + 后续页顶部色带）----

def _decorate_first(canvas, _doc) -> None:
    _footer(canvas)


def _decorate_later(canvas, _doc) -> None:
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, h - 3.2 * mm, w, 3.2 * mm, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, h - 3.2 * mm, 28 * mm, 3.2 * mm, fill=1, stroke=0)
    canvas.restoreState()
    _footer(canvas)


def _footer(canvas) -> None:
    canvas.saveState()
    canvas.setStrokeColor(HAIRLINE)
    canvas.setLineWidth(0.5)
    canvas.line(_M, 12.5 * mm, 210 * mm - _M, 12.5 * mm)
    canvas.setFont(_FONT, 7.5)
    canvas.setFillColor(GRAY)
    canvas.drawString(_M, 8.5 * mm, "TripMate · 多 Agent 协同旅游规划")
    canvas.drawRightString(210 * mm - _M, 8.5 * mm, f"第 {canvas.getPageNumber()} 页")
    canvas.restoreState()


# ---- 首屏横幅（PIL 绘制：实拍图裁剪 + 压暗标题条；无图/失败回退品牌纯色）----



def _pil_font(size: int) -> ImageFont.FreeTypeFont:
    for path, idx in _BOLD_FILES:
        try:
            return ImageFont.truetype(path, size, index=idx)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _crop_ratio(im: PILImage.Image, ratio: float, anchor: float = 0.45) -> PILImage.Image:
    """居中（可调锚点）裁剪到指定宽高比；anchor=0 顶对齐、1 底对齐。"""
    w, h = im.size
    th = int(w / ratio)
    if th <= h:
        top = int((h - th) * anchor)
        return im.crop((0, top, w, top + th))
    tw = int(h * ratio)
    left = (w - tw) // 2
    return im.crop((left, 0, left + tw, h))


def _section(num: str, title: str) -> Table:
    """编号分区标题：品牌色编号方块 + 粗体标题 + 基线细横线。"""
    t = Table([[Paragraph(num, _style("secnum", 12, bold=True, color=colors.white,
                                      alignment=TA_CENTER, leading=15)),
                Paragraph(title, _style("sect", 14, bold=True, color=PRIMARY))]],
              colWidths=[9 * mm, _CONTENT_W - 9 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), PRIMARY),
        ("LINEBELOW", (1, 0), (1, 0), 0.7, HAIRLINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
    ]))
    return t


def _table(rows: list, widths: list, font_size: int = 9, right_cols: tuple = ()) -> Table:
    """表头品牌色白粗体；正文无竖线 + 斑马纹 + 行间细横线。单元格可传 Paragraph（原样使用）。"""
    body = []
    for i, row in enumerate(rows):
        cells = []
        for j, c in enumerate(row):
            if isinstance(c, Paragraph):
                cells.append(c)
                continue
            st = _style(f"tc{i}-{j}", font_size)
            if j in right_cols:
                st.alignment = 2  # TA_RIGHT
            cells.append(Paragraph(str(c), st))
        body.append(cells)
    t = Table(body, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, HAIRLINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _weekday(date_str: str, travel_dates: list) -> str:
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = datetime.strptime(date_str, fmt)
            if fmt == "%m-%d" and travel_dates and travel_dates[0][:4].isdigit():
                d = d.replace(year=int(travel_dates[0][:4]))
            return "周" + "一二三四五六日"[d.weekday()]
        except ValueError:
            continue
    return ""


def _day_photo(day, profile: TravelProfile) -> str:
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


def _day_strip(path: str, title: str) -> str:
    """日程卡照片头条：实拍图裁 178×30mm + 左深右透渐变压暗 + 上下缘羽化淡出 + 白色粗体标题。

    渐变而非整条压暗：右侧照片保持鲜活（"想去旅游"感），左侧文字区保证可读；
    上下羽化让条带与白底页面自然融合——照片内的大字牌匾在条带边缘渐隐而非被硬切，
    消除"文字被拦腰截断"的观感。失败返回空串（回退蓝色日头条）。
    """
    w, h = 1100, 185
    out = CROP_DIR / ("day_" + hashlib.md5(f"{path}|{title}|v3".encode()).hexdigest()[:12] + ".jpg")
    try:
        CROP_DIR.mkdir(parents=True, exist_ok=True)
        with PILImage.open(path) as im:
            photo = _crop_ratio(im.convert("RGB"), w / h).resize((w, h))
        grad = PILImage.new("L", (w, 1))
        for x in range(w):
            grad.putpixel((x, 0), int(190 - (190 - 30) * x / (w - 1)))
        overlay = PILImage.new("RGBA", (w, h), (13, 36, 66, 0))
        overlay.putalpha(grad.resize((w, h)))
        base = PILImage.alpha_composite(photo.convert("RGBA"), overlay)
        # 上下缘羽化：顶部 10%、底部 14% 高度线性淡出到白底（与下方正文卡自然衔接）
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
        d.rectangle((0, 0, 9, h), fill=(230, 97, 0))
        f = _pil_font(40)
        ty = (h - 46) // 2
        d.text((32, ty + 2), title, font=f, fill=(10, 25, 45))   # 投影
        d.text((30, ty), title, font=f, fill=(255, 255, 255))
        base.save(out, quality=88)
        return str(out)
    except Exception:  # noqa: BLE001 — 失败回退蓝色日头条
        return ""


def _crop_43(path: str) -> str:
    """统一 4:3 居中裁剪（缓存于 outputs/images/crops/，不污染原图 md5 缓存）；失败回退原图。"""
    out = CROP_DIR / (Path(path).stem + "_43.jpg")
    try:
        if not out.exists():
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            with PILImage.open(path) as im:
                _crop_ratio(im.convert("RGB"), 4 / 3).save(out, quality=88)
        return str(out)
    except Exception:  # noqa: BLE001 — 裁剪失败退回原图（现状行为）
        return path


def _image_cell(spot: str, path: str, source: str) -> Table:
    cells = [[Paragraph(f"{spot}", _style("imgspot", 9.5, bold=True))]]
    try:
        p = _crop_43(path)
        cells.append([Image(p, width=79 * mm, height=79 * mm * 3 / 4)])
    except Exception:  # noqa: BLE001 — 图片缺失降级为文字卡片（§5.2）
        cells.append([Paragraph(f"【{spot}】图片暂缺", _style("noimg", 10, color=GRAY))])
    src = source if len(source) <= 96 else source[:93] + "..."
    cells.append([Paragraph(f"来源：{src}", _style("imgsrc", 7.5, color=GRAY, leading=10))])
    t = Table(cells, colWidths=[85 * mm])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, HAIRLINE),
        ("BACKGROUND", (0, 0), (0, 0), BG_LIGHT),
        ("LINEBELOW", (0, 0), (0, 0), 0.5, HAIRLINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


# ---- 封面页（需求 7：参考"旅行路书"模板——深蓝底 + 目的地实拍条 + 大字标题 + 数据徽章）----

_COVER_W, _COVER_H = 1100, 1528  # ≈178×247mm（A4 内容区整页）
_NAVY_TOP, _NAVY_BOT = (27, 40, 74), (32, 54, 104)
_GOLD = (212, 175, 105)


def _cover_base() -> PILImage.Image:
    """深蓝竖向渐变底。"""
    base = PILImage.new("RGB", (_COVER_W, _COVER_H))
    px = base.load()
    for y in range(_COVER_H):
        t = y / (_COVER_H - 1)
        px_row = tuple(int(a + (b - a) * t) for a, b in zip(_NAVY_TOP, _NAVY_BOT))
        for x in range(_COVER_W):
            px[x, y] = px_row
    return base


def _draw_center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    x = (_COVER_W - draw.textlength(text, font=font)) / 2
    draw.text((x, y), text, font=font, fill=fill)


def _make_cover(profile: TravelProfile) -> str:
    """封面：深蓝底 + 底部实拍条（渐变融入）+ 大字标题 + 风格 tag + 底部信息行。

    全程回退安全：无实拍图/绘制失败均回退纯色封面。"""
    basic = profile.basic_info
    dest = basic.destination or "旅行"
    days = basic.days or "-"
    out = CROP_DIR / ("cover_" + hashlib.md5(f"{dest}|{days}|v1".encode()).hexdigest()[:12] + ".jpg")
    try:
        CROP_DIR.mkdir(parents=True, exist_ok=True)
        base = _cover_base().convert("RGBA")
        # 底部实拍条：高 420px，顶缘 160px 渐变融入深蓝
        strip_h, blend = 420, 160
        for item in profile.images[:8]:
            if any(k in (item.source or "") for k in ("示意", "非实景", "占位")) or not item.path:
                continue
            try:
                with PILImage.open(item.path) as im:
                    photo = _crop_ratio(im.convert("RGB"), _COVER_W / strip_h).resize((_COVER_W, strip_h))
                mask = PILImage.new("L", (_COVER_W, strip_h), 255)
                mpx = mask.load()
                for y in range(blend):
                    v = int(255 * y / blend)
                    for x in range(_COVER_W):
                        mpx[x, y] = v
                base.paste(photo, (0, _COVER_H - strip_h), mask)
                break
            except Exception:  # noqa: BLE001 — 换下一张
                continue
        d = ImageDraw.Draw(base)
        # 金色细线 + 底部信息
        d.rectangle((0, _COVER_H - strip_h - 3, _COVER_W, _COVER_H - strip_h), fill=_GOLD)
        d.rectangle((0, 0, 14, _COVER_H), fill=_GOLD)
        f_tag = _pil_font(26)
        f_title = _pil_font(88)
        f_sub = _pil_font(34)
        f_chip = _pil_font(24)
        # 顶部 tagline 胶囊
        tagline = " · ".join(basic.style or []) or "轻松出行"
        tagline = f"{basic.origin or '出发地'} 出发 · {tagline}"
        tw = d.textlength(tagline, font=f_tag)
        x0, y0 = (_COVER_W - tw) / 2 - 34, 300
        d.rounded_rectangle((x0, y0, x0 + tw + 68, y0 + 58), radius=29,
                            outline=_GOLD, width=2)
        _draw_center(d, y0 + 12, tagline, f_tag, _GOLD)
        # 主标题 / 副标题
        _draw_center(d, 470, f"{dest}", f_title, (255, 255, 255))
        sub = f"{days} 天旅行路书".replace(" -1 天", "")
        _draw_center(d, 590, sub, f_sub, (222, 230, 242))
        sub2 = f"{basic.origin or ''} — {dest} · TRIPMATE ITINERARY".strip(" —")
        _draw_center(d, 648, sub2, _pil_font(22), (150, 168, 196))
        # 风格 tag 胶囊行
        tags = (basic.style or [])[:4] or ["休闲"]
        chip_gap, pad = 18, 46
        widths = [d.textlength(t, font=f_chip) + pad * 2 for t in tags]
        total = sum(widths) + chip_gap * (len(tags) - 1)
        cx, cy = (_COVER_W - total) / 2, 760
        for t, w in zip(tags, widths):
            d.rounded_rectangle((cx, cy, cx + w, cy + 52), radius=26,
                                outline=(150, 168, 196), width=2)
            tx = cx + (w - d.textlength(t, font=f_chip)) / 2
            d.text((tx, cy + 10), t, font=f_chip, fill=(200, 212, 230))
            cx += w + chip_gap
        # 底部信息行（实拍条之上）
        info_y = _COVER_H - strip_h - 96
        f_num = _pil_font(46)
        f_lab = _pil_font(20)
        party = basic.party_size or detail.party_size if (detail := profile.detail_info) else basic.party_size
        stats = [(f"{days}天", f"{basic.travel_mode or '高铁'} 往返"),
                 (basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定"), "出行时间"),
                 (f"{party or '-'} 人", "同行人数"),
                 (f"¥{int(basic.budget)}" if basic.budget else "-", "预算参考")]
        col_w = _COVER_W / len(stats)
        for i, (num, lab) in enumerate(stats):
            cx = col_w * i + col_w / 2
            d.text((cx - d.textlength(num, font=f_num) / 2, info_y), num, font=f_num, fill=_GOLD)
            d.text((cx - d.textlength(lab, font=f_lab) / 2, info_y + 66), lab, font=f_lab,
                   fill=(180, 194, 216))
        base = base.convert("RGB")
        base.save(out, quality=90)
        return str(out)
    except Exception:  # noqa: BLE001 — 封面失败回退纯色
        try:
            base = _cover_base()
            d = ImageDraw.Draw(base)
            _draw_center(d, 600, f"{dest} · 旅行路书", _pil_font(80), (255, 255, 255))
            base.save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001
            return ""


# ---- 主流程 ----

def build_pdf(profile: TravelProfile, run_id: str) -> str:
    """渲染最终行程 PDF（§1.2：逐日行程表 + 路线 + 美食 + 预算 + 配图（标注来源）+ 注意事项）。"""
    _register_font()
    basic, detail = profile.basic_info, profile.detail_info
    budget = compute_budget(profile, profile.draft)
    out = OUTPUT_DIR / f"行程计划_{basic.destination or '行程'}_{run_id[:8]}.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=_M, rightMargin=_M,
                            topMargin=15 * mm, bottomMargin=18 * mm,
                            title=f"TripMate 旅行路书 · {basic.destination}")
    story: list = []

    # ---- 封面页（需求 7：路书模板；无图/绘制失败自动回退纯色封面）----
    cover = _make_cover(profile)
    if cover:
        story.append(Image(cover, width=_CONTENT_W, height=_CONTENT_W * _COVER_H / _COVER_W))
        story.append(PageBreak())

    # ---- 壹 行程总览（关键信息一览，全部来自黑板真实数据）----
    story.append(_section("壹", "行程总览"))
    story.append(Spacer(1, 4))
    party = detail.party_size or basic.party_size or 1
    d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
    d1 = basic.travel_dates[-1] if basic.travel_dates else ""
    dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
    budget_txt = f"¥{basic.budget:g}" if basic.budget else "-"
    hotel_pref = detail.hotel.location_pref or "-"
    must = "、".join(detail.must_visit[:6]) or "-"
    styles = " / ".join(basic.style or []) or "休闲"
    rhythm = {"快": "4-5 个景点/天", "慢": "1-2 个景点/天"}.get(detail.pace or "", "2-3 个景点/天")
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
            Paragraph(row[0], _style("ovk", 9, bold=True, color=PRIMARY)),
            Paragraph(str(row[1]), _style("ovv", 9)),
            Paragraph(row[2], _style("ovk2", 9, bold=True, color=PRIMARY)),
            Paragraph(str(row[3]), _style("ovv2", 9)),
        ])
    t = Table(info_cells, colWidths=[20 * mm, 69 * mm, 20 * mm, 69 * mm])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [BG_LIGHT, colors.white]),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, HAIRLINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    if profile.draft:  # 每日节奏一行速览（真实草稿数据）
        rhythm_line = " ｜ ".join(
            f"D{i + 1} {'→'.join(d.spots[:3]) or (d.morning or '')[:12]}"
            for i, d in enumerate(profile.draft.days))
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"每日节奏：{rhythm_line}", _style("rhythm", 9, color=GRAY)))
    story.append(Spacer(1, 7))

    # ---- 贰 推荐订单清单 ----
    story.append(_section("贰", "推荐订单清单（Agent 已按您的要求筛选勾选）"))
    hdr = lambda s: Paragraph(s, _style("th", 9, bold=True, color=colors.white))  # noqa: E731

    def _link_cell(url: str, label: str) -> str:
        """友好锚文本超链接：原始长 URL 会把 30mm 窄列按字符硬换行撑爆版面（2026-08-30 实测）。"""
        return (f'<a href="{url}" color="{PRIMARY_HEX}"><u>{label}</u></a>' if url else "—")

    order_rows = [[hdr("类型"), hdr("名称/班次"), hdr("关键信息"), hdr("推荐理由"), hdr("直达链接")]]
    for t in profile.tickets:
        reason = (f'<font face="{_bold_name}" color="{SUCCESS_HEX}">√ 已勾选</font>　' if t.selected else "") + (t.reason or "")
        order_rows.append(["车票", t.train_no,
                           f"{t.depart_time} 出发 / {t.arrive_time} 到达，{t.price} 元",
                           Paragraph(reason, _style("reason", 8.5)),
                           Paragraph(_link_cell(t.link, "12306 购票"), _style("linkcell", 8.5))])
    for h in profile.hotels:
        reason = (f'<font face="{_bold_name}" color="{SUCCESS_HEX}">√ 已勾选</font>　' if h.selected else "") + (h.reason or "")
        order_rows.append(["酒店", h.name,
                           f"{h.price_per_night:g} 元/晚，距地标 {h.distance_km:g}km，评分 {h.rating:g}",
                           Paragraph(reason, _style("reason2", 8.5)),
                           Paragraph(_link_cell(h.link, "携程订房"), _style("linkcell2", 8.5))])
    story.append(_table(order_rows, [14 * mm, 32 * mm, 50 * mm, 52 * mm, 30 * mm], font_size=8.5))
    total_order = sum(t.price * (2 if "往返" not in t.train_no else 1) * party
                      for t in profile.tickets if t.selected)
    total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                       for h in profile.hotels if h.selected)
    story.append(Paragraph(f"已勾选订单合计（交通往返 + 住宿）：<font face=\"{_bold_name}\">约 {total_order:g} 元</font>",
                           _style("order_total", 10, spaceBefore=4, spaceAfter=2)))
    ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
    if ref_notes:
        story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                               _style("refnote", 8.5, color=WARN, spaceAfter=4)))
    story.append(Spacer(1, 7))

    # ---- 叁 逐日行程 ----
    story.append(_section("叁", "逐日行程"))
    if profile.draft:
        slot_st = lambda: _style("slot", 9, bold=True, color=ACCENT)  # noqa: E731
        for i, day in enumerate(profile.draft.days):
            wk = _weekday(day.date, basic.travel_dates)
            label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
            body_rows = [
                [Paragraph("上午", slot_st()),
                 Paragraph(day.morning or "—", _style("cell", 9.5))],
                [Paragraph("下午", slot_st()),
                 Paragraph(day.afternoon or "—", _style("cell", 9.5))],
                [Paragraph("晚上", slot_st()),
                 Paragraph(day.evening or "—", _style("cell", 9.5))],
            ]
            strip_path = ""
            photo = _day_photo(day, profile)
            if photo:
                strip_path = _day_strip(photo, label)
            if strip_path:
                # 照片头条（178×30mm）+ 白底正文卡；头条与正文间不加分隔，视觉上是一张卡
                story.append(Image(strip_path, width=_CONTENT_W, height=_CONTENT_W * 185 / 1100))
                t = Table(body_rows, colWidths=[18 * mm, _CONTENT_W - 18 * mm])
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (0, -1), LIGHT),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]))
            else:
                day_rows = [[Paragraph(label, _style("day", 11, bold=True, color=colors.white)), ""]] + \
                    body_rows
                t = Table(day_rows, colWidths=[18 * mm, _CONTENT_W - 18 * mm])
                t.setStyle(TableStyle([
                    ("SPAN", (0, 0), (1, 0)),
                    ("BACKGROUND", (0, 0), (1, 0), PRIMARY),
                    ("BACKGROUND", (0, 1), (0, -1), LIGHT),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.4, HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]))
            story.append(t)
            story.append(Spacer(1, 5))
    story.append(Spacer(1, 2))

    # ---- 肆 预算核算 ----
    story.append(_section("肆", "预算核算（全团口径）"))
    note = (f"预算 {budget['budget']:g}｜上限 {budget['budget_max']:g}｜"
            f"占用 {budget['occupancy']:.0%}") if budget["occupancy"] else "—"
    b_rows = [[hdr("项目"), hdr("说明"), hdr("金额（元）")]] + [
        [r["item"], r["note"], f"{r['amount']:g}"] for r in budget["items"]
    ]
    b_rows.append([Paragraph("合计", _style("bsum", 9.5, bold=True)), Paragraph(note, _style("bsumnote", 9)),
                   Paragraph(f"{budget['total']:g}", _style("bsumamt", 9.5, bold=True, alignment=2))])
    story.append(_table(b_rows, [26 * mm, 112 * mm, 40 * mm], right_cols=(2,)))
    occ = max(0.0, min(float(budget["occupancy"] or 0), 1.0))
    if occ > 0:
        bar = Table([["", ""]], colWidths=[_CONTENT_W * occ, _CONTENT_W * (1 - occ)],
                    rowHeights=[3.2 * mm])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), PRIMARY if occ < 0.9 else WARN),
            ("BACKGROUND", (1, 0), (1, 0), BG_LIGHT),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, HAIRLINE),
        ]))
        story.append(Spacer(1, 4))
        # 条+说明绑定不分页（避免占用条落在页底、说明行被孤立到下一页）
        story.append(KeepTogether([
            bar,
            # 口径与预算表一致：条宽按 clamp 后比例画，数字展示真实占用（可超 100%）
            Paragraph(f"预算占用 {float(budget['occupancy'] or 0):.0%}（合计 {budget['total']:g} 元）",
                      _style("barcap", 8, color=GRAY, spaceBefore=2)),
        ]))
    for w in budget["warnings"]:
        story.append(Paragraph("※ " + w, _style("bwarn", 9.5, color=WARN, spaceBefore=3)))
    story.append(Spacer(1, 7))

    # ---- 伍 实景速览 ----
    if profile.images:
        story.append(_section("伍", "实景速览（均标注来源）"))
        story.append(Spacer(1, 3))
        imgs = profile.images[:8]
        for r in range(0, len(imgs), 2):
            row_cells = [_image_cell(img.spot, img.path, img.source) for img in imgs[r:r + 2]]
            if len(row_cells) == 1:
                row_cells.append("")
            t = Table([row_cells], colWidths=[89 * mm, 89 * mm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
            story.append(t)
            story.append(Spacer(1, 5))
        story.append(Spacer(1, 2))

    # ---- 陆 推荐酒店（需求 6：实景 + 评分 + 网络评价；仅展示已补充信息的勾选酒店）----
    enriched = [h for h in profile.hotels if (h.image_path or h.review_digest) and h.selected] \
        or [h for h in profile.hotels if h.image_path or h.review_digest]
    if enriched:
        hotel_flow: list = []
        for h in enriched[:2]:
            left_cells = []
            if h.image_path:  # 空路径直接走文字卡（PIL 对空串的异常发生在 doc.build 期，兜不住）
                try:
                    img_path = _crop_43(h.image_path)
                    left_cells.append([Image(img_path, width=60 * mm, height=45 * mm)])
                except Exception:  # noqa: BLE001 — 图缺退文字
                    left_cells.append([Paragraph("酒店图片暂缺", _style("noimg2", 9, color=GRAY))])
            else:
                left_cells.append([Paragraph("酒店图片暂缺", _style("noimg2", 9, color=GRAY))])
            right_cells = [
                [Paragraph(h.name, _style("hname", 11, bold=True))],
                [Paragraph(f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km"
                           + ("　<font face=\"{}\" color=\"{}\">√ 已勾选</font>".format(_bold_name, SUCCESS_HEX)
                              if h.selected else ""), _style("hmeta", 9))],
                [Paragraph(f"网络评价：{h.review_digest}" if h.review_digest else "网络评价：暂无",
                           _style("hreview", 8.5, color=GRAY, leading=12))],
            ]
            card = Table([[left_cells, right_cells]], colWidths=[64 * mm, _CONTENT_W - 64 * mm])
            card.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.5, HAIRLINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            hotel_flow.append(card)
            hotel_flow.append(Spacer(1, 5))
        # 标题与首卡绑定，避免章节头孤立在页底
        story.append(KeepTogether([_section("陆", "推荐酒店（实景 + 网络评价）"),
                                   Spacer(1, 3), hotel_flow[0], hotel_flow[1]]))
        story.extend(hotel_flow[2:])
        story.append(Spacer(1, 2))

    # ---- 柒 美食与注意事项 ----
    story.append(_section("柒", "美食与注意事项"))
    foods: list[str] = []
    for g in profile.guide_digest:
        foods += g.foods
    seen_f: set[str] = set()
    uniq_foods = [f for f in foods if not (f in seen_f or seen_f.add(f))]
    foods_text = "、".join(uniq_foods[:12])
    if not foods_text:
        # 真实搜索通道结构化字段为空时，回退展示标题含目的地的搜索结果（诚实标注，纯展示，
        # 过滤 Tavily 对 site: 限定遵守不严带来的无关结果）
        dest = basic.destination or ""
        titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
        titles = list(dict.fromkeys(titles))[:5]
        foods_text = ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles)) if titles \
            else "暂无（攻略通道未返回）"
    story.append(Paragraph(foods_text, _style("foods", 10)))
    story.append(Spacer(1, 6))

    # ---- 注意事项与数据来源（并入柒）----
    warns: list[str] = []
    for g in profile.guide_digest:
        warns += g.warnings
    seen_w: set[str] = set()
    warn_items = [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]
    if warn_items:
        warn_rows = [[Paragraph("• " + w, _style("warn_item", 9.5))] for w in warn_items]
        wt = Table(warn_rows, colWidths=[_CONTENT_W])
        wt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BG_LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(wt)
    for i, g in enumerate(profile.guide_digest[:3]):
        story.append(Paragraph(f"攻略来源[{i + 1}]：{g.source_name} {g.source_url}（抓取 {g.fetched_at}）",
                               _style("src", 8, color=GRAY, spaceBefore=3)))
    if profile.weather.get("days"):
        wline = "；".join(f"{d['date']} {d['day_text']} {d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃"
                         for d in profile.weather["days"])
        story.append(Paragraph(f"天气参考（{profile.weather.get('source', '')}）：{wline}",
                               _style("src2", 8, color=GRAY, spaceBefore=3)))
    if basic.defaults_applied:
        story.append(Paragraph("默认值说明：以下字段由系统按默认值补齐——" + "、".join(basic.defaults_applied),
                               _style("src3", 8, color=WARN, spaceBefore=3)))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"本计划由 TripMate 多 Agent 系统生成 · {datetime.now():%Y-%m-%d %H:%M} · "
                           f"车票/酒店请在官方渠道完成支付（系统不接触支付，§4.4）",
                           _style("footer", 8, color=GRAY)))

    doc.build(story, onFirstPage=_decorate_first, onLaterPages=_decorate_later)
    return str(out)
