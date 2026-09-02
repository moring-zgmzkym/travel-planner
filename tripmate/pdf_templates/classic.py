"""Classic 模板：深蓝旅行手册风（原 pdf_gen 版式的零行为变化迁移，默认模板）。"""

from __future__ import annotations

from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                Spacer, Table, TableStyle)

from ..models import TravelProfile
from .base import CONTENT_W, BaseTripTemplate, bold_font_name, day_photo, weekday


class ClassicTemplate(BaseTripTemplate):
    name = "classic"
    display_name = "经典旅行手册"
    description = "深蓝渐变封面 + 实拍照片头条 + 编号分区 + 斑马表格"
    scenes = "默认通用"

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        st = self.style
        basic, detail = profile.basic_info, profile.detail_info
        story: list = []

        # ---- 封面页（路书模板；无图/绘制失败自动回退纯色封面）----
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
        for row in overview_rows:
            info_cells.append([
                Paragraph(row[0], st("ovk", 9, bold=True, color=self.PRIMARY)),
                Paragraph(str(row[1]), st("ovv", 9)),
                Paragraph(row[2], st("ovk2", 9, bold=True, color=self.PRIMARY)),
                Paragraph(str(row[3]), st("ovv2", 9)),
            ])
        t = Table(info_cells, colWidths=[20 * mm, 69 * mm, 20 * mm, 69 * mm])
        t.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [self.BG_LIGHT, colors.white]),
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
        hdr = lambda s: Paragraph(s, st("th", 9, bold=True, color=colors.white))  # noqa: E731

        def _link_cell(url: str, label: str) -> str:
            """友好锚文本超链接：原始长 URL 会把 30mm 窄列按字符硬换行撑爆版面（2026-08-30 实测）。"""
            return (f'<a href="{url}" color="{self.PRIMARY_HEX}"><u>{label}</u></a>' if url else "—")

        order_rows = [[hdr("类型"), hdr("名称/班次"), hdr("关键信息"), hdr("推荐理由"), hdr("直达链接")]]
        for tk in profile.tickets:
            reason = (f'<font face="{bold_font_name()}" color="{self.SUCCESS_HEX}">√ 已勾选</font>　' if tk.selected else "") + (tk.reason or "")
            order_rows.append(["车票", tk.train_no,
                               f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price} 元",
                               Paragraph(reason, st("reason", 8.5)),
                               Paragraph(_link_cell(tk.link, "12306 购票"), st("linkcell", 8.5))])
        for h in profile.hotels:
            reason = (f'<font face="{bold_font_name()}" color="{self.SUCCESS_HEX}">√ 已勾选</font>　' if h.selected else "") + (h.reason or "")
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

        # ---- 叁 逐日行程 ----
        story.append(self.section("叁", "逐日行程"))
        if profile.draft:
            slot_st = lambda: st("slot", 9, bold=True, color=self.ACCENT)  # noqa: E731
            for i, day in enumerate(profile.draft.days):
                wk = weekday(day.date, basic.travel_dates)
                label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
                body_rows = [
                    [Paragraph("上午", slot_st()),
                     Paragraph(day.morning or "—", st("cell", 9.5))],
                    [Paragraph("下午", slot_st()),
                     Paragraph(day.afternoon or "—", st("cell", 9.5))],
                    [Paragraph("晚上", slot_st()),
                     Paragraph(day.evening or "—", st("cell", 9.5))],
                ]
                strip_path = ""
                photo = day_photo(day, profile)
                if photo:
                    strip_path = self.day_strip(photo, label)
                if strip_path:
                    # 照片头条（178×30mm）+ 白底正文卡；头条与正文间不加分隔，视觉上是一张卡
                    story.append(Image(strip_path, width=CONTENT_W, height=CONTENT_W * 185 / 1100))
                    t = Table(body_rows, colWidths=[18 * mm, CONTENT_W - 18 * mm])
                    t.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (0, -1), self.LIGHT),
                        ("LINEBELOW", (0, 0), (-1, -2), 0.4, self.HAIRLINE),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ]))
                else:
                    day_rows = [[Paragraph(label, st("day", 11, bold=True, color=colors.white)), ""]] + \
                        body_rows
                    t = Table(day_rows, colWidths=[18 * mm, CONTENT_W - 18 * mm])
                    t.setStyle(TableStyle([
                        ("SPAN", (0, 0), (1, 0)),
                        ("BACKGROUND", (0, 0), (1, 0), self.PRIMARY),
                        ("BACKGROUND", (0, 1), (0, -1), self.LIGHT),
                        ("LINEBELOW", (0, 1), (-1, -2), 0.4, self.HAIRLINE),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ]))
                story.append(t)
                story.append(Spacer(1, 5))
        story.append(Spacer(1, 2))

        # ---- 肆 预算核算 ----
        story.append(self.section("肆", "预算核算（全团口径）"))
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
                # 口径与预算表一致：条宽按 clamp 后比例画，数字展示真实占用（可超 100%）
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
                    [Paragraph(h.name, st("hname", 11, bold=True))],
                    [Paragraph(f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km"
                               + ("　<font face=\"{}\" color=\"{}\">√ 已勾选</font>".format(bold_font_name(), self.SUCCESS_HEX)
                                  if h.selected else ""), st("hmeta", 9))],
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
        food_blocks = self.food_map(profile)
        if food_blocks:
            story.extend(food_blocks)
        elif uniq_foods:
            story.append(self.food_grid(uniq_foods))
        else:
            # 真实搜索通道结构化字段为空时，回退展示标题含目的地的搜索结果（诚实标注，纯展示，
            # 过滤 Tavily 对 site: 限定遵守不严带来的无关结果）
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
            warn_rows = [[Paragraph("• " + w, st("warn_item", 9.5))] for w in warn_items]
            wt = Table(warn_rows, colWidths=[CONTENT_W])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), self.BG_LIGHT),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
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
