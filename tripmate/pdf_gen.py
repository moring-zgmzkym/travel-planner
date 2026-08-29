"""PDF 生成（§4.5）：reportlab（Windows 无 GTK，weasyprint 不可用，按企划书备选方案切换）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from .config import OUTPUT_DIR
from .models import TravelProfile
from .planning import compute_budget

PRIMARY = colors.HexColor("#1a5fb4")
LIGHT = colors.HexColor("#eaf1fb")
WARN = colors.HexColor("#c01c28")
GRAY = colors.HexColor("#777777")

_FONT = "MSYH"
_FONT_FILES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
]
_registered = False


def _register_font() -> None:
    global _registered
    if _registered:
        return
    for path, idx in _FONT_FILES:
        try:
            pdfmetrics.registerFont(TTFont(_FONT, path, subfontIndex=idx))
            _registered = True
            return
        except Exception:  # noqa: BLE001 — 逐个字体兜底
            continue
    raise RuntimeError("未找到可用中文字体（msyh/simhei/simsun）")


def _style(name: str, size: int = 10, bold: bool = False, color=colors.black,
           leading: int | None = None, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=_FONT, fontSize=size,
                          leading=leading or int(size * 1.5),
                          textColor=color, **kw)


def build_pdf(profile: TravelProfile, run_id: str) -> str:
    """渲染最终行程 PDF（§1.2：逐日行程表 + 路线 + 美食 + 预算 + 配图（标注来源）+ 注意事项）。"""
    _register_font()
    basic, detail = profile.basic_info, profile.detail_info
    budget = compute_budget(profile, profile.draft)
    out = OUTPUT_DIR / f"行程计划_{basic.destination or '行程'}_{run_id[:8]}.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"TripMate 行程计划 · {basic.destination}")
    story: list = []

    # ---- 头部 ----
    story.append(Paragraph("TripMate · 行程计划", _style("h0", 22, color=PRIMARY, spaceAfter=2)))
    meta = (f"{basic.origin} → {basic.destination}｜{basic.days} 天"
            f"（{basic.travel_dates[0] if basic.travel_dates else '日期待定'} ~ "
            f"{basic.travel_dates[-1] if basic.travel_dates else ''}）｜"
            f"{basic.travel_mode}｜风格 {'/'.join(basic.style) or '-'}｜{detail.party_size or basic.party_size} 人"
            f"｜预算 {basic.budget or '-'} 元（上限 {basic.budget_max or '-'}）")
    story.append(Paragraph(meta, _style("meta", 10, color=GRAY, spaceAfter=8)))

    # ---- 推荐订单清单 ----
    story.append(Paragraph("一、推荐订单清单（Agent 已按您的要求筛选勾选）", _style("h1", 14, color=PRIMARY, spaceAfter=4, spaceBefore=6)))
    order_rows = [["类型", "名称/班次", "关键信息", "推荐理由", "直达链接"]]
    for t in profile.tickets:
        order_rows.append(["车票", t.train_no,
                           f"{t.depart_time} 出发 / {t.arrive_time} 到达，{t.price} 元",
                           ("√ 已勾选：" if t.selected else "") + t.reason or "",
                           t.link or "—"])
    for h in profile.hotels:
        order_rows.append(["酒店", h.name,
                           f"{h.price_per_night} 元/晚，距地标 {h.distance_km}km，评分 {h.rating}",
                           ("√ 已勾选：" if h.selected else "") + (h.reason or ""),
                           h.link or "—"])
    story.append(_table(order_rows, [14 * mm, 30 * mm, 52 * mm, 52 * mm, 28 * mm], font_size=8))
    party = detail.party_size or basic.party_size or 1
    total_order = sum(t.price * (2 if "往返" not in t.train_no else 1) * party
                      for t in profile.tickets if t.selected)
    total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                       for h in profile.hotels if h.selected)
    story.append(Paragraph(f"已勾选订单合计（交通往返 + 住宿）：约 {round(total_order, 1)} 元",
                           _style("order_total", 10, spaceBefore=3, spaceAfter=6)))
    ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
    if ref_notes:
        story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                               _style("refnote", 8.5, color=WARN, spaceAfter=6)))

    # ---- 逐日行程表 ----
    story.append(Paragraph("二、逐日行程", _style("h1", 14, color=PRIMARY, spaceAfter=4, spaceBefore=6)))
    if profile.draft:
        for i, day in enumerate(profile.draft.days):
            day_rows = [
                [Paragraph(f"第 {i + 1} 天 · {day.date}", _style("day", 11, color=colors.white))],
                [Paragraph("上午", _style("slot", 9, color=PRIMARY)),
                 Paragraph(day.morning or "—", _style("cell", 9.5))],
                [Paragraph("下午", _style("slot", 9, color=PRIMARY)),
                 Paragraph(day.afternoon or "—", _style("cell", 9.5))],
                [Paragraph("晚上", _style("slot", 9, color=PRIMARY)),
                 Paragraph(day.evening or "—", _style("cell", 9.5))],
            ]
            t = Table(day_rows, colWidths=[18 * mm, 152 * mm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (0, -1), LIGHT),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(t)
            story.append(Spacer(1, 4))

    # ---- 预算表 ----
    story.append(Paragraph("三、预算核算（全团口径）", _style("h1", 14, color=PRIMARY, spaceAfter=4, spaceBefore=6)))
    b_rows = [["项目", "说明", "金额（元）"]] + [
        [r["item"], r["note"], f"{r['amount']}"] for r in budget["items"]
    ]
    b_rows.append(["合计", f"预算 {budget['budget'] or '-'}｜上限 {budget['budget_max'] or '-'}｜"
                  f"占用 {budget['occupancy']:.0%}" if budget["occupancy"] else "—",
                  f"{budget['total']}"])
    story.append(_table(b_rows, [24 * mm, 106 * mm, 40 * mm]))
    for w in budget["warnings"]:
        story.append(Paragraph("※ " + w, _style("bwarn", 9.5, color=WARN, spaceBefore=2)))

    # ---- 实拍配图 ----
    if profile.images:
        story.append(PageBreak())
        story.append(Paragraph("四、目的地配图（均标注来源）", _style("h1", 14, color=PRIMARY, spaceAfter=6)))
        imgs = profile.images[:8]
        for r in range(0, len(imgs), 2):
            row_cells = []
            for img in imgs[r:r + 2]:
                row_cells.append(_image_cell(img.spot, img.path, img.source))
            t = Table([row_cells], colWidths=[85 * mm, 85 * mm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
            story.append(t)
            story.append(Spacer(1, 4))

    # ---- 美食与注意事项 ----
    story.append(Paragraph("五、美食推荐", _style("h1", 14, color=PRIMARY, spaceAfter=4, spaceBefore=6)))
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

    story.append(Paragraph("六、注意事项与数据来源", _style("h1", 14, color=PRIMARY, spaceAfter=4, spaceBefore=6)))
    warns: list[str] = []
    for g in profile.guide_digest:
        warns += g.warnings
    seen_w: set[str] = set()
    for w in [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]:
        story.append(Paragraph("• " + w, _style("warn_item", 9.5)))
    for i, g in enumerate(profile.guide_digest[:3]):
        story.append(Paragraph(f"攻略来源[{i + 1}]：{g.source_name} {g.source_url}（抓取 {g.fetched_at}）",
                               _style("src", 8, color=GRAY)))
    if profile.weather.get("days"):
        wline = "；".join(f"{d['date']} {d['day_text']} {d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃"
                         for d in profile.weather["days"])
        story.append(Paragraph(f"天气参考（{profile.weather.get('source', '')}）：{wline}",
                               _style("src", 8, color=GRAY)))
    if basic.defaults_applied:
        story.append(Paragraph("默认值说明：以下字段由系统按默认值补齐——" + "、".join(basic.defaults_applied),
                               _style("src", 8, color=WARN)))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"本计划由 TripMate 多 Agent 系统生成 · {datetime.now():%Y-%m-%d %H:%M} · "
                           f"车票/酒店请在官方渠道完成支付（系统不接触支付，§4.4）",
                           _style("footer", 8, color=GRAY)))

    doc.build(story)
    return str(out)


def _table(rows: list, widths: list, font_size: int = 9) -> Table:
    body = []
    for i, row in enumerate(rows):
        body.append([Paragraph(str(c), _style(f"tc{i}", font_size)) for c in row])
    t = Table(body, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _image_cell(spot: str, path: str, source: str) -> Table:
    cells = []
    try:
        with PILImage.open(path) as im:
            w, h = im.size
        img = Image(path, width=78 * mm, height=78 * mm * h / w)
        cells.append(img)
    except Exception:  # noqa: BLE001 — 图片缺失降级为文字卡片（§5.2）
        cells.append(Paragraph(f"【{spot}】图片暂缺", _style("noimg", 10, color=GRAY)))
    src = source if len(source) <= 96 else source[:93] + "..."
    cells.append(Paragraph(f"{spot}｜来源：{src}", _style("imgsrc", 7, color=GRAY, leading=9)))
    t = Table([[c] for c in cells], colWidths=[82 * mm])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t
