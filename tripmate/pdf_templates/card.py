"""Card 模板：信息卡片式——青绿×靛蓝清爽配色，逐日行程/订单/酒店均为浅底描边独立卡片。

版式要点：逐日行程每天一张独立卡片（照片或主题色头条 + 时段行，BOX 描边，
整卡 KeepTogether 防跨页断裂；卡片高度有界，超长时由 reportlab 自然分页）；
订单按类别分色底徽标卡片行；总览为 2×4 信息小卡阵列；预算占用条沿用基类思路。
所有图片/封面处理复用基类回退机制（失败不崩，降级文字或纯色）。
"""

from __future__ import annotations

from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                Spacer, Table, TableStyle)

from ..models import TravelProfile
from .base import CONTENT_W, BaseTripTemplate, bold_font_name, day_photo, weekday


class CardTemplate(BaseTripTemplate):
    name = "card"
    display_name = "信息卡片"
    description = "青绿×靛蓝清爽配色，逐日行程/订单/酒店均为浅底描边独立卡片，信息分块清晰"
    scenes = "城市游 / 多段交通 / 偏好分块速读"

    # ---- 主题配色（覆写基类：青绿主色 + 靛蓝点缀，与 Classic 深蓝拉开差距）----
    PRIMARY = colors.HexColor("#0f766e")       # 青绿主色
    PRIMARY_HEX = "#0f766e"
    ACCENT = colors.HexColor("#4f46e5")        # 靛蓝点缀（酒店徽标/时段标签/提示条）
    ACCENT_RGB = (79, 70, 229)
    SUCCESS = colors.HexColor("#15803d")
    SUCCESS_HEX = "#15803d"
    WARN = colors.HexColor("#c01c28")
    GRAY = colors.HexColor("#64748b")
    LIGHT = colors.HexColor("#e0f2f1")         # 日程卡时段列底色（浅青）
    BG_LIGHT = colors.HexColor("#f0fdfa")      # 卡片浅底
    HAIRLINE = colors.HexColor("#c7e2dc")      # 卡片描边/细线
    COVER_TOP_RGB = (10, 56, 54)
    COVER_BOT_RGB = (15, 118, 110)
    COVER_GOLD_RGB = (216, 185, 116)

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        st = self.style
        basic, detail = profile.basic_info, profile.detail_info
        story: list = []

        # ---- 封面页（复用基类主题化封面；失败自动回退纯色封面）----
        cover = self.make_cover(profile)
        if cover:
            story.append(Image(cover, width=CONTENT_W,
                               height=CONTENT_W * self.COVER_H / self.COVER_W))
            story.append(PageBreak())

        # ---- 壹 行程总览：2×4 信息小卡阵列 + 宽幅偏好卡 ----
        party = detail.party_size or basic.party_size or 1
        d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
        d1 = basic.travel_dates[-1] if basic.travel_dates else ""
        dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
        budget_txt = f"¥{basic.budget:g}" if basic.budget else "-"
        hotel_pref = detail.hotel.location_pref or "-"
        must = "、".join(detail.must_visit[:6]) or "-"
        styles = " / ".join(basic.style or []) or "休闲"
        rhythm = {"快": "4-5 个景点/天", "慢": "1-2 个景点/天"}.get(detail.pace or "", "2-3 个景点/天")
        budget_full = f"{budget_txt}（上限 {basic.budget_max:g}）" if basic.budget_max else budget_txt

        def mini_card(label: str, value: str) -> Table:
            t = Table([[Paragraph(label, st("mcl", 8, color=self.GRAY, leading=11))],
                       [Paragraph(str(value), st("mcv", 9.5, bold=True, leading=13))]],
                      colWidths=[CONTENT_W / 4 - 5 * mm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), self.BG_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, self.HAIRLINE),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]))
            return t

        facts = [
            ("目的地", basic.destination or "-"), ("出行时间", dates_txt),
            ("行程天数", f"{basic.days or '-'} 天"), ("同行人数", f"{party} 人"),
            ("交通方式", basic.travel_mode or "-"), ("旅行风格", styles),
            ("预算参考", budget_full), ("游览节奏", rhythm),
        ]
        grid = Table([[mini_card(k, v) for k, v in facts[r:r + 4]] for r in (0, 4)],
                     colWidths=[CONTENT_W / 4] * 4)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ]))
        wide = Table([[Paragraph("酒店偏好", st("wk", 8, color=self.GRAY)),
                       Paragraph(hotel_pref, st("wv", 9.5)),
                       Paragraph("必去景点", st("wk2", 8, color=self.GRAY)),
                       Paragraph(must, st("wv2", 9.5))]],
                     colWidths=[20 * mm, CONTENT_W / 2 - 20 * mm, 20 * mm, CONTENT_W / 2 - 20 * mm])
        wide.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.BG_LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.6, self.HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        ov_block: list = [self.section("壹", "行程总览"), Spacer(1, 4), grid, Spacer(1, 3), wide]
        if profile.draft:  # 每日节奏一行速览（真实草稿数据）
            rhythm_line = " ｜ ".join(
                f"D{i + 1} {'→'.join(d.spots[:3]) or (d.morning or '')[:12]}"
                for i, d in enumerate(profile.draft.days))
            ov_block += [Spacer(1, 4),
                         Paragraph(f"每日节奏：{rhythm_line}", st("rhythm", 9, color=self.GRAY))]
        story.append(KeepTogether(ov_block))
        story.append(Spacer(1, 7))

        # ---- 贰 推荐订单清单：每类一个色底徽标 + 每条订单一行浅底卡片 ----
        story.append(self.section("贰", "推荐订单清单（Agent 已按您的要求筛选勾选）"))
        story.append(Spacer(1, 4))

        def _link_cell(url: str, label: str) -> str:
            """友好锚文本超链接：原始长 URL 会把窄列按字符硬换行撑爆版面。"""
            return (f'<a href="{url}" color="{self.PRIMARY_HEX}"><u>{label}</u></a>' if url else "—")

        def _cat_chip(text: str, color) -> Table:
            t = Table([[Paragraph(text, st("chip", 8.5, bold=True, color=colors.white,
                                          alignment=TA_CENTER, leading=11))]], colWidths=[18 * mm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), color),
                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            return t

        def _order_card(kind: str, badge_color, title: str, info: str,
                        reason_html: str, link_html: str) -> Table:
            t = Table([[
                Paragraph(kind, st("obadge", 8.5, bold=True, color=colors.white, alignment=TA_CENTER)),
                [Paragraph(title, st("otitle", 9.5, bold=True)),
                 Paragraph(info, st("oinfo", 8.5, color=self.GRAY, leading=12))],
                Paragraph(reason_html, st("oreason", 8.5)),
                Paragraph(link_html, st("olink", 8.5)),
            ]], colWidths=[13 * mm, 57 * mm, 72 * mm, 36 * mm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), badge_color),
                ("BACKGROUND", (1, 0), (-1, 0), self.BG_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, self.HAIRLINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            return t

        checked = f'<font face="{bold_font_name()}" color="{self.SUCCESS_HEX}">√ 已勾选</font>　'

        def emit_category(kind: str, badge_color, cards: list) -> None:
            """类别徽标与首卡绑定（防徽标孤悬页底），其余卡片顺序排布。"""
            if not cards:
                return
            story.append(KeepTogether([_cat_chip(kind, badge_color), Spacer(1, 3),
                                       cards[0][0], cards[0][1]]))
            for card, sp in cards[1:]:
                story.append(card)
                story.append(sp)

        ticket_cards = []
        for tk in profile.tickets:
            reason = (checked if tk.selected else "") + (tk.reason or "")
            ticket_cards.append((_order_card(
                "车票", self.PRIMARY, tk.train_no,
                f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price} 元",
                reason, _link_cell(tk.link, "12306 购票")), Spacer(1, 2.5)))
        emit_category("车票", self.PRIMARY, ticket_cards)
        hotel_cards = []
        for h in profile.hotels:
            reason = (checked if h.selected else "") + (h.reason or "")
            hotel_cards.append((_order_card(
                "酒店", self.ACCENT, h.name,
                f"{h.price_per_night:g} 元/晚，距地标 {h.distance_km:g}km，评分 {h.rating:g}",
                reason, _link_cell(h.link, "携程订房")), Spacer(1, 2.5)))
        emit_category("酒店", self.ACCENT, hotel_cards)
        if not ticket_cards and not hotel_cards:
            story.append(Paragraph("暂无推荐订单。", st("noorder", 9.5, color=self.GRAY)))
        total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                          for tk in profile.tickets if tk.selected)
        total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                           for h in profile.hotels if h.selected)
        tot_card = Table([[Paragraph(
            f"已勾选订单合计（交通往返 + 住宿）：<font face=\"{bold_font_name()}\">约 {total_order:g} 元</font>",
            st("order_total", 10))]], colWidths=[CONTENT_W])
        tot_card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.6, self.HAIRLINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(Spacer(1, 2))
        story.append(tot_card)
        ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
        if ref_notes:
            story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                                   st("refnote", 8.5, color=self.WARN, spaceAfter=4)))
        story.append(Spacer(1, 7))

        # ---- 叁 逐日行程：每天一张独立卡片（头条 + 时段行，整卡 KeepTogether）----
        story.append(self.section("叁", "逐日行程"))
        story.append(Spacer(1, 4))
        if profile.draft:
            def slot_st():
                return st("slot", 9, bold=True, color=self.ACCENT)

            for i, day in enumerate(profile.draft.days):
                wk = weekday(day.date, basic.travel_dates)
                label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
                body_rows = [
                    [Paragraph("上午", slot_st()), Paragraph(day.morning or "—", st("cell", 9.5))],
                    [Paragraph("下午", slot_st()), Paragraph(day.afternoon or "—", st("cell", 9.5))],
                    [Paragraph("晚上", slot_st()), Paragraph(day.evening or "—", st("cell", 9.5))],
                ]
                strip_path = ""
                photo = day_photo(day, profile)
                if photo:
                    strip_path = self.day_strip(photo, label)  # 失败返回 ""，回退纯色头条
                if strip_path:
                    head_row = [Image(strip_path, width=CONTENT_W,
                                      height=CONTENT_W * 185 / 1100), ""]
                else:
                    head_row = [Paragraph(label, st("dayhdr", 11, bold=True, color=colors.white)), ""]
                t = Table([head_row] + body_rows, colWidths=[18 * mm, CONTENT_W - 18 * mm])
                cmds = [
                    ("SPAN", (0, 0), (1, 0)),
                    ("BOX", (0, 0), (-1, -1), 0.8, self.HAIRLINE),
                    ("BACKGROUND", (0, 1), (0, -1), self.LIGHT),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, self.HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 1), (-1, -1), 8),
                    ("TOPPADDING", (0, 1), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
                ]
                if strip_path:  # 照片头条紧贴描边（零内边距），与正文构成一张完整卡片
                    cmds += [("TOPPADDING", (0, 0), (-1, 0), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
                             ("LEFTPADDING", (0, 0), (-1, 0), 0), ("RIGHTPADDING", (0, 0), (-1, 0), 0)]
                else:
                    cmds += [("BACKGROUND", (0, 0), (-1, 0), self.PRIMARY),
                             ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
                             ("LEFTPADDING", (0, 0), (-1, 0), 8)]
                t.setStyle(TableStyle(cmds))
                # 单卡高度有界（头条 + 三时段行），KeepTogether 保证整卡不跨页断裂；
                # 极端超长文本时由 reportlab 按行自然分页。
                story.append(KeepTogether([t]))
                story.append(Spacer(1, 5))
        story.append(Spacer(1, 2))

        # ---- 肆 预算核算：基类斑马表 + 主题色占用条 ----
        story.append(self.section("肆", "预算核算（全团口径）"))

        def hdr(s: str) -> Paragraph:
            return Paragraph(s, st("th", 9, bold=True, color=colors.white))

        note = (f"预算 {budget['budget']:g}｜上限 {budget['budget_max']:g}｜"
                f"占用 {budget['occupancy']:.0%}") if budget["occupancy"] else "—"
        b_rows = [[hdr("项目"), hdr("说明"), hdr("金额（元）")]] + [
            [r["item"], r["note"], f"{r['amount']:g}"] for r in budget["items"]
        ]
        b_rows.append([Paragraph("合计", st("bsum", 9.5, bold=True)), Paragraph(note, st("bsumnote", 9)),
                       Paragraph(f"{budget['total']:g}", st("bsumamt", 9.5, bold=True, alignment=2))])
        story.append(self.table(b_rows, [26 * mm, 112 * mm, 40 * mm], right_cols=(2,)))
        occ = max(0.0, min(float(budget["occupancy"] or 0), 1.0))
        if occ > 0:
            bar = Table([["", ""]], colWidths=[CONTENT_W * occ, CONTENT_W * (1 - occ)],
                        rowHeights=[3.2 * mm])
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), self.PRIMARY if occ < 0.9 else self.WARN),
                ("BACKGROUND", (1, 0), (1, 0), self.BG_LIGHT),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, self.HAIRLINE),
            ]))
            story.append(Spacer(1, 4))
            # 条+说明绑定不分页（避免占用条落在页底、说明行被孤立到下一页）
            story.append(KeepTogether([
                bar,
                Paragraph(f"预算占用 {float(budget['occupancy'] or 0):.0%}（合计 {budget['total']:g} 元）",
                          st("barcap", 8, color=self.GRAY, spaceBefore=2)),
            ]))
        chart = self.budget_charts(budget, party, basic.days or 1, basic.travel_dates)
        if chart:
            story.append(Spacer(1, 5))
            story.append(Image(chart, width=CONTENT_W, height=CONTENT_W * 560 / 1500))
        for w in budget["warnings"]:
            story.append(Paragraph("※ " + w, st("bwarn", 9.5, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 7))

        # ---- 伍 实景速览（复用基类图片卡；缺图自动降级文字卡）----
        if profile.images:
            story.append(self.section("伍", "实景速览（均标注来源）"))
            story.append(Spacer(1, 4))
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

        # ---- 陆 推荐酒店：浅底卡片（仅展示已补充信息的勾选酒店）----
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
                    [Paragraph(h.name, st("hname", 11, bold=True, color=self.PRIMARY))],
                    [Paragraph(f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km"
                               + ("　<font face=\"{}\" color=\"{}\">√ 已勾选</font>".format(bold_font_name(), self.SUCCESS_HEX)
                                  if h.selected else ""), st("hmeta", 9))],
                    [Paragraph(f"网络评价：{h.review_digest}" if h.review_digest else "网络评价：暂无",
                               st("hreview", 8.5, color=self.GRAY, leading=12))],
                ]
                card = Table([[left_cells, right_cells]], colWidths=[64 * mm, CONTENT_W - 64 * mm])
                card.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), self.BG_LIGHT),
                    ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]))
                hotel_flow.append(card)
                hotel_flow.append(Spacer(1, 5))
            # 标题与首卡绑定，避免章节头孤立在页底
            story.append(KeepTogether([self.section("陆", "推荐酒店（实景 + 网络评价）"),
                                       Spacer(1, 4), hotel_flow[0], hotel_flow[1]]))
            story.extend(hotel_flow[2:])
            story.append(Spacer(1, 2))

        # ---- 柒 美食与注意事项 ----
        story.append(self.section("柒", "美食与注意事项"))
        story.append(Spacer(1, 4))
        foods: list[str] = []
        for g in profile.guide_digest:
            foods += g.foods
        seen_f: set[str] = set()
        uniq_foods = [f for f in foods if not (f in seen_f or seen_f.add(f))]
        food_blocks = self.food_map(profile)
        if food_blocks:
            story.extend(food_blocks)
        elif uniq_foods:
            story.append(self.food_grid(uniq_foods))
        else:
            # 真实搜索通道结构化字段为空时，回退展示标题含目的地的搜索结果（诚实标注，纯展示）
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods_text = ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles)) if titles \
                else "暂无（攻略通道未返回）"
            story.append(Paragraph(foods_text, st("foods", 10)))
        story.append(Spacer(1, 6))

        warns: list[str] = []
        for g in profile.guide_digest:
            warns += g.warnings
        seen_w: set[str] = set()
        warn_items = [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]
        if warn_items:
            warn_rows = [["", Paragraph("• " + w, st("warn_item", 9.5))] for w in warn_items]
            wt = Table(warn_rows, colWidths=[1.8 * mm, CONTENT_W - 1.8 * mm])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), self.ACCENT),
                ("BACKGROUND", (1, 0), (1, -1), self.BG_LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (1, 0), (1, -1), 8),
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
