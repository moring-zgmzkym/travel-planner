"""Minimal 模板：极简黑白文档风——纯白底、近黑墨字、细线分区，无色块无斑马纹，黑白打印友好。

版式原则：章节标题 = 一行粗体黑字 + 下方细横线；表格 = 粗体灰字表头 + 底线，正文仅行间细线；
日程 = 简洁日期行 + 三行时段文本（不用照片头条）；封面 = 纯白底大号目的地标题 + 灰色细线信息行。
所有图片/封面处理与 base 一致：失败即回退（跳过或降级文字），不影响 PDF 生成。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from PIL import Image as PILImage
from PIL import ImageDraw
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                Spacer, Table, TableStyle)

from ..models import TravelProfile
from .base import (CONTENT_W, CROP_DIR, BaseTripTemplate, bold_font_name,
                   draw_center, pil_font, weekday)


class MinimalTemplate(BaseTripTemplate):
    name = "minimal"
    display_name = "极简黑白文档"
    description = "纯白底黑字文档风：粗体标题 + 细线分区，细线表格无色块，黑白打印友好"
    scenes = "打印友好 / 正式文档 / 极简风偏好"

    # ---- 主题配色覆写：近黑墨色 + 灰阶细线，强调靠线宽与字重而非色块 ----
    INK = colors.HexColor("#1f2937")          # 近黑主色（正文/标题）
    INK_HEX = "#1f2937"
    PRIMARY = colors.HexColor("#1f2937")
    PRIMARY_HEX = "#1f2937"
    ACCENT = colors.HexColor("#4b5563")
    ACCENT_RGB = (75, 85, 99)
    SUCCESS = colors.HexColor("#111827")
    SUCCESS_HEX = "#111827"
    WARN = colors.HexColor("#374151")
    GRAY = colors.HexColor("#6b7280")
    LIGHT = colors.HexColor("#ffffff")
    BG_LIGHT = colors.HexColor("#f9fafb")
    HAIRLINE = colors.HexColor("#d1d5db")
    RULE_SOFT = colors.HexColor("#e5e7eb")    # 占用条底色等极浅灰

    # ---- 页面装饰：无顶部色带，仅页脚细线 ----

    def decorate_later(self, canvas, _doc) -> None:
        """极简风不画顶部色带，后续页只保留页脚细线。"""
        self.draw_footer(canvas)

    # ---- 结构积木（覆写为极简版式）----

    def section(self, num: str, title: str) -> Table:
        """章节标题：一行粗体黑字『壹　标题』+ 下方细横线（不用色块编号）。"""
        t = Table([[Paragraph(f"{num}　{title}",
                              self.style("sec", 13, bold=True, color=self.INK))]],
                  colWidths=[CONTENT_W])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, self.INK),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        return t

    def table(self, rows: list, widths: list, font_size: int = 9, right_cols: tuple = ()) -> Table:
        """极简表格：表头粗体灰字 + 底线，正文近黑无斑马纹，仅行间细线。单元格可传 Paragraph（原样使用）。"""
        body = []
        for i, row in enumerate(rows):
            cells = []
            for j, c in enumerate(row):
                if isinstance(c, Paragraph):
                    cells.append(c)
                    continue
                if i == 0:
                    st = self.style(f"th{j}", font_size, bold=True, color=self.GRAY)
                else:
                    st = self.style(f"tc{i}-{j}", font_size, color=self.INK)
                if j in right_cols:
                    st.alignment = 2  # TA_RIGHT
                cells.append(Paragraph(str(c), st))
            body.append(cells)
        t = Table(body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, self.GRAY),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    def image_cell(self, spot: str, path: str, source: str) -> Table:
        """实景卡片：细灰线边框 + 粗体景点名 + 4:3 图（失败降级文字卡）+ 来源小字，无底色。"""
        cells = [[Paragraph(f"{spot}", self.style("imgspot", 9.5, bold=True, color=self.INK))]]
        try:
            p = self.crop_43(path)
            cells.append([Image(p, width=79 * mm, height=79 * mm * 3 / 4)])
        except Exception:  # noqa: BLE001 — 图片缺失降级为文字卡片
            cells.append([Paragraph(f"【{spot}】图片暂缺", self.style("noimg", 10, color=self.GRAY))])
        src = source if len(source) <= 96 else source[:93] + "..."
        cells.append([Paragraph(f"来源：{src}", self.style("imgsrc", 7.5, color=self.GRAY, leading=10))])
        t = Table(cells, colWidths=[85 * mm])
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, self.HAIRLINE),
            ("LINEBELOW", (0, 0), (0, 0), 0.5, self.HAIRLINE),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    # ---- 封面：纯白底 + 大号黑色目的地标题 + 灰色细线信息行 ----

    def make_cover(self, profile: TravelProfile) -> str:
        """极简封面（PIL 绘制）：白底、居中大号近黑目的地标题、灰色细线分隔的信息行。
        失败返回空串（render 侧跳过封面页）。"""
        basic = profile.basic_info
        detail = profile.detail_info
        dest = basic.destination or "旅行"
        days = basic.days or "-"
        out = CROP_DIR / ("cover_min_" + hashlib.md5(
            f"{self.name}|{dest}|{days}|v1".encode()).hexdigest()[:12] + ".jpg")
        ink = (31, 41, 55)
        gray = (107, 114, 128)
        gray_light = (156, 163, 175)
        rule = (209, 213, 219)
        try:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            w, h = self.COVER_W, self.COVER_H
            im = PILImage.new("RGB", (w, h), (255, 255, 255))
            d = ImageDraw.Draw(im)
            # 顶部：短细线 + 眉题
            d.line(((w - 90) / 2, 236, (w + 90) / 2, 236), fill=rule, width=2)
            draw_center(d, w, 268, "旅 行 路 书", pil_font(30), gray)
            draw_center(d, w, 314, "TRIPMATE ITINERARY", pil_font(20), gray_light)
            # 大号目的地标题（过长自动缩字号，防溢出）
            size = 130
            f_title = pil_font(size)
            while d.textlength(dest, font=f_title) > w * 0.86 and size > 40:
                size -= 6
                f_title = pil_font(size)
            draw_center(d, w, 480, dest, f_title, ink)
            sub = f"{days} 天旅行路书" if basic.days else "旅行路书"
            draw_center(d, w, 680, sub, pil_font(34), gray)
            # 中部细线分隔
            d.line((w * 0.32, 800, w * 0.68, 800), fill=rule, width=2)
            # 信息行：文档式「细线 + 灰色文字」逐行排布
            d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
            d1 = basic.travel_dates[-1] if basic.travel_dates else ""
            dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
            party = (detail.party_size if detail else 0) or basic.party_size
            infos = [
                f"{basic.origin or '出发地'} 出发 · {basic.travel_mode or '交通待定'} 往返",
                f"{dates_txt} · {party or '-'} 人同行",
                (f"预算参考 ¥{basic.budget:g}" if basic.budget else "预算待定"),
            ]
            f_info = pil_font(26)
            y = 866
            for txt in infos:
                d.line((w * 0.30, y, w * 0.70, y), fill=rule, width=2)
                draw_center(d, w, y + 22, txt, f_info, gray)
                y += 96
            # 底部署名
            d.line(((w - 90) / 2, h - 160, (w + 90) / 2, h - 160), fill=rule, width=2)
            draw_center(d, w, h - 128, "TRIPMATE · 多 AGENT 协同旅游规划", pil_font(20), gray_light)
            im.save(out, quality=90)
            return str(out)
        except Exception:  # noqa: BLE001 — 封面失败回退空串（跳过封面页）
            return ""

    # ---- 正文 ----

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        st = self.style
        basic, detail = profile.basic_info, profile.detail_info
        story: list = []

        # ---- 封面页（极简白底；绘制失败自动跳过）----
        cover = self.make_cover(profile)
        if cover:
            story.append(Image(cover, width=CONTENT_W,
                               height=CONTENT_W * self.COVER_H / self.COVER_W))
            story.append(PageBreak())

        # ---- 壹 行程总览 ----
        story.append(self.section("壹", "行程总览"))
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
        for i, row in enumerate(overview_rows):
            info_cells.append([
                Paragraph(row[0], st(f"ovk{i}", 9, bold=True, color=self.GRAY)),
                Paragraph(str(row[1]), st(f"ovv{i}", 9, color=self.INK)),
                Paragraph(row[2], st(f"ovk2-{i}", 9, bold=True, color=self.GRAY)),
                Paragraph(str(row[3]), st(f"ovv2-{i}", 9, color=self.INK)),
            ])
        t = Table(info_cells, colWidths=[20 * mm, 69 * mm, 20 * mm, 69 * mm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, self.HAIRLINE),
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
            story.append(Paragraph(f"每日节奏：{rhythm_line}", st("rhythm", 9, color=self.GRAY)))
        story.append(Spacer(1, 7))

        # ---- 贰 推荐订单清单 ----
        story.append(self.section("贰", "推荐订单清单（Agent 已按您的要求筛选勾选）"))

        def _link_cell(url: str, label: str) -> str:
            """友好锚文本超链接：原始长 URL 会把窄列按字符硬换行撑爆版面。"""
            return (f'<a href="{url}" color="{self.PRIMARY_HEX}"><u>{label}</u></a>' if url else "—")

        order_rows = [["类型", "名称/班次", "关键信息", "推荐理由", "直达链接"]]
        for tk in profile.tickets:
            reason = (f'<font face="{bold_font_name()}">√ 已勾选</font>　' if tk.selected else "") + (tk.reason or "")
            order_rows.append(["车票", tk.train_no,
                               f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price} 元",
                               Paragraph(reason, st("reason", 8.5)),
                               Paragraph(_link_cell(tk.link, "12306 购票"), st("linkcell", 8.5))])
        for h in profile.hotels:
            reason = (f'<font face="{bold_font_name()}">√ 已勾选</font>　' if h.selected else "") + (h.reason or "")
            order_rows.append(["酒店", h.name,
                               f"{h.price_per_night:g} 元/晚，距地标 {h.distance_km:g}km，评分 {h.rating:g}",
                               Paragraph(reason, st("reason2", 8.5)),
                               Paragraph(_link_cell(h.link, "携程订房"), st("linkcell2", 8.5))])
        story.append(self.table(order_rows, [14 * mm, 32 * mm, 50 * mm, 52 * mm, 30 * mm], font_size=8.5))
        total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                          for tk in profile.tickets if tk.selected)
        total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                           for h in profile.hotels if h.selected)
        story.append(Paragraph(f"已勾选订单合计（交通往返 + 住宿）：<font face=\"{bold_font_name()}\">约 {total_order:g} 元</font>",
                               st("order_total", 10, spaceBefore=4, spaceAfter=2)))
        ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
        if ref_notes:
            story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                                   st("refnote", 8.5, color=self.WARN, spaceAfter=4)))
        story.append(Spacer(1, 7))

        # ---- 叁 逐日行程（简洁日期行 + 三行时段文本，不用照片头条）----
        story.append(self.section("叁", "逐日行程"))
        if profile.draft:
            slot_st = lambda i: st(f"slot{i}", 9, bold=True, color=self.GRAY)  # noqa: E731
            for i, day in enumerate(profile.draft.days):
                wk = weekday(day.date, basic.travel_dates)
                label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
                rows = [[Paragraph(label, st(f"daylbl{i}", 10.5, bold=True, color=self.INK)), ""]] + [
                    [Paragraph("上午", slot_st(i)),
                     Paragraph(day.morning or "—", st(f"daym{i}", 9.5, color=self.INK))],
                    [Paragraph("下午", slot_st(i)),
                     Paragraph(day.afternoon or "—", st(f"daya{i}", 9.5, color=self.INK))],
                    [Paragraph("晚上", slot_st(i)),
                     Paragraph(day.evening or "—", st(f"daye{i}", 9.5, color=self.INK))],
                ]
                t = Table(rows, colWidths=[16 * mm, CONTENT_W - 16 * mm])
                t.setStyle(TableStyle([
                    ("SPAN", (0, 0), (1, 0)),
                    ("LINEBELOW", (0, 0), (1, 0), 0.7, self.INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.4, self.HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ]))
                story.append(KeepTogether(t))
                story.append(Spacer(1, 5))
        story.append(Spacer(1, 2))

        # ---- 肆 预算核算 ----
        story.append(self.section("肆", "预算核算（全团口径）"))
        note = (f"预算 {budget['budget']:g}｜上限 {budget['budget_max']:g}｜"
                f"占用 {budget['occupancy']:.0%}") if budget["occupancy"] else "—"
        b_rows = [["项目", "说明", "金额（元）"]] + [
            [r["item"], r["note"], f"{r['amount']:g}"] for r in budget["items"]
        ]
        b_rows.append([Paragraph("合计", st("bsum", 9.5, bold=True, color=self.INK)),
                       Paragraph(note, st("bsumnote", 9, color=self.GRAY)),
                       Paragraph(f"{budget['total']:g}", st("bsumamt", 9.5, bold=True, color=self.INK, alignment=2))])
        story.append(self.table(b_rows, [26 * mm, 112 * mm, 40 * mm], right_cols=(2,)))
        occ = max(0.0, min(float(budget["occupancy"] or 0), 1.0))
        if occ > 0:
            bar = Table([["", ""]], colWidths=[CONTENT_W * occ, CONTENT_W * (1 - occ)],
                        rowHeights=[2.6 * mm])
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), self.INK),
                ("BACKGROUND", (1, 0), (1, 0), self.RULE_SOFT),
            ]))
            story.append(Spacer(1, 4))
            # 条+说明绑定不分页（避免占用条落在页底、说明行被孤立到下一页）
            story.append(KeepTogether([
                bar,
                Paragraph(f"预算占用 {float(budget['occupancy'] or 0):.0%}（合计 {budget['total']:g} 元）",
                          st("barcap", 8, color=self.GRAY, spaceBefore=2)),
            ]))
        for w in budget["warnings"]:
            story.append(Paragraph("※ " + w, st("bwarn", 9.5, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 7))

        # ---- 伍 实景速览 ----
        if profile.images:
            story.append(self.section("伍", "实景速览（均标注来源）"))
            story.append(Spacer(1, 3))
            imgs = profile.images[:8]
            for r in range(0, len(imgs), 2):
                row_cells = [self.image_cell(img.spot, img.path, img.source) for img in imgs[r:r + 2]]
                if len(row_cells) == 1:
                    row_cells.append("")
                t = Table([row_cells], colWidths=[89 * mm, 89 * mm])
                t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                story.append(t)
                story.append(Spacer(1, 5))
            story.append(Spacer(1, 2))

        # ---- 陆 推荐酒店（仅展示已补充信息的勾选酒店）----
        enriched = [h for h in profile.hotels if (h.image_path or h.review_digest) and h.selected] \
            or [h for h in profile.hotels if h.image_path or h.review_digest]
        if enriched:
            hotel_flow: list = []
            for h in enriched[:2]:
                left_cells = []
                if h.image_path:  # 空路径直接走文字卡（PIL 对空串的异常发生在 doc.build 期，兜不住）
                    try:
                        img_path = self.crop_43(h.image_path)
                        left_cells.append([Image(img_path, width=60 * mm, height=45 * mm)])
                    except Exception:  # noqa: BLE001 — 图缺退文字
                        left_cells.append([Paragraph("酒店图片暂缺", st("noimg2", 9, color=self.GRAY))])
                else:
                    left_cells.append([Paragraph("酒店图片暂缺", st("noimg2", 9, color=self.GRAY))])
                right_cells = [
                    [Paragraph(h.name, st("hname", 11, bold=True, color=self.INK))],
                    [Paragraph(f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km"
                               + (f'　<font face="{bold_font_name()}">√ 已勾选</font>' if h.selected else ""),
                               st("hmeta", 9))],
                    [Paragraph(f"网络评价：{h.review_digest}" if h.review_digest else "网络评价：暂无",
                               st("hreview", 8.5, color=self.GRAY, leading=12))],
                ]
                card = Table([[left_cells, right_cells]], colWidths=[64 * mm, CONTENT_W - 64 * mm])
                card.setStyle(TableStyle([
                    ("BOX", (0, 0), (-1, -1), 0.5, self.HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]))
                hotel_flow.append(card)
                hotel_flow.append(Spacer(1, 5))
            # 标题与首卡绑定，避免章节头孤立在页底
            story.append(KeepTogether([self.section("陆", "推荐酒店（实景 + 网络评价）"),
                                       Spacer(1, 3), hotel_flow[0], hotel_flow[1]]))
            story.extend(hotel_flow[2:])
            story.append(Spacer(1, 2))

        # ---- 柒 美食与注意事项 ----
        story.append(self.section("柒", "美食与注意事项"))
        foods: list[str] = []
        for g in profile.guide_digest:
            foods += g.foods
        seen_f: set[str] = set()
        uniq_foods = [f for f in foods if not (f in seen_f or seen_f.add(f))]
        if uniq_foods:
            story.append(self.food_grid(uniq_foods))
        else:
            # 真实搜索通道结构化字段为空时，回退展示标题含目的地的搜索结果（诚实标注，纯展示）
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods_text = ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles)) if titles \
                else "暂无（攻略通道未返回）"
            story.append(Paragraph(foods_text, st("foods", 10, color=self.INK)))
        story.append(Spacer(1, 6))

        warns: list[str] = []
        for g in profile.guide_digest:
            warns += g.warnings
        seen_w: set[str] = set()
        warn_items = [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]
        if warn_items:
            warn_rows = [[Paragraph("· " + w, st(f"warn_item{i}", 9.5, color=self.INK))]
                         for i, w in enumerate(warn_items)]
            wt = Table(warn_rows, colWidths=[CONTENT_W])
            wt.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, self.HAIRLINE),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(wt)
        for i, g in enumerate(profile.guide_digest[:3]):
            story.append(Paragraph(f"攻略来源[{i + 1}]：{g.source_name} {g.source_url}（抓取 {g.fetched_at}）",
                                   st("src", 8, color=self.GRAY, spaceBefore=3)))
        if profile.weather.get("days"):
            wline = "；".join(f"{d['date']} {d['day_text']} {d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃"
                             for d in profile.weather["days"])
            story.append(Paragraph(f"天气参考（{profile.weather.get('source', '')}）：{wline}",
                                   st("src2", 8, color=self.GRAY, spaceBefore=3)))
        if basic.defaults_applied:
            story.append(Paragraph("默认值说明：以下字段由系统按默认值补齐——" + "、".join(basic.defaults_applied),
                                   st("src3", 8, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"本计划由 TripMate 多 Agent 系统生成 · {datetime.now():%Y-%m-%d %H:%M} · "
                               f"车票/酒店请在官方渠道完成支付（系统不接触支付，§4.4）",
                               st("footer", 8, color=self.GRAY)))
        return story
