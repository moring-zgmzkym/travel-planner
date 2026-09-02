"""Journal 模板：奶油牛皮纸底色的旅行手账风路书。

墨绿主色 + 奶油底色（#f7f1e3）+ 胶带黄 / 淡粉点缀。灵魂视觉二件：
1. 拍立得照片框——PIL 在奶油底上画模糊阴影 + 白色厚边框 + 顶部胶带条 +
   底部手写体地名（实景速览、逐日缩略、酒店卡共用）；
2. 日期胶带头条——色块标签 + 手账式虚线 / 点线分隔（reportlab LINEBELOW dashes）。

版式独立于 Classic，但七个章节的数据读取完全一致；所有 PIL 失败均回退
（基类 image_cell / 纯色卡 / 纯文字卡），绝不让 PDF 生成崩溃。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                Spacer, Table, TableStyle)

from ..models import TravelProfile
from .base import (CONTENT_W, CROP_DIR, BaseTripTemplate, bold_font_name,
                   crop_ratio, day_photo, pil_font, weekday)

_DASH = 1  # reportlab 线命令 cap 参数（平头），其后跟 dash 数组


def _pol_geometry(size: int) -> tuple:
    """拍立得画布几何：卡宽 cw、卡高 ch（含底部题字带）、留白 m（阴影空间）。"""
    cw = size
    ch = int(cw * 1.12)
    m = max(16, cw // 22)
    return cw, ch, m


class JournalTemplate(BaseTripTemplate):
    name = "journal"
    display_name = "旅行手账"
    description = "奶油牛皮纸底色 + 拍立得照片框 + 胶带日期头条的复古手账风路书"
    scenes = "休闲度假、旅行纪念、分享赠阅"
    footer_text = "TripMate · 旅行手账"

    # ---- 手账配色（覆写基类主题）----
    PRIMARY = colors.HexColor("#3d5a45")       # 墨绿
    PRIMARY_HEX = "#3d5a45"
    ACCENT = colors.HexColor("#c78d3d")        # 胶带琥珀黄
    ACCENT_RGB = (199, 141, 61)
    SUCCESS = colors.HexColor("#4a7c59")
    SUCCESS_HEX = "#4a7c59"
    WARN = colors.HexColor("#a84a32")          # 复古砖红
    GRAY = colors.HexColor("#7d715e")          # 暖灰
    LIGHT = colors.HexColor("#efe4c9")         # 时段列牛皮底色
    BG_LIGHT = colors.HexColor("#f7f1e3")      # 奶油纸底（整页背景）
    HAIRLINE = colors.HexColor("#d8c9a8")
    CARD = colors.HexColor("#fffdf6")          # 手账卡片纸白
    TAPE_EDGE = colors.HexColor("#a9722c")     # 胶带压边
    TAPE_YELLOW_RGB = (233, 204, 130)          # 胶带黄（拍立得顶贴）
    TAPE_PINK_RGB = (226, 164, 162)            # 淡粉胶带（交替点缀）
    INK_RGB = (74, 62, 45)                     # 手写深棕
    BG_RGB = (247, 241, 227)                   # 与 BG_LIGHT 一致（PIL 用）
    COVER_TOP_RGB = (61, 90, 69)               # 封面墨绿渐变（复用基类 make_cover）
    COVER_BOT_RGB = (41, 62, 48)
    COVER_GOLD_RGB = (238, 224, 190)           # 奶油金

    # ---- 页面装饰：整页奶油底 + 顶部胶带色带 ----

    def _paint_bg(self, canvas) -> None:
        canvas.saveState()
        canvas.setFillColor(self.BG_LIGHT)
        w, h = A4
        canvas.rect(0, 0, w, h, fill=1, stroke=0)
        canvas.restoreState()

    def decorate_first(self, canvas, _doc) -> None:
        self._paint_bg(canvas)
        self.draw_footer(canvas)

    def decorate_later(self, canvas, _doc) -> None:
        self._paint_bg(canvas)
        super().decorate_later(canvas, _doc)

    # ---- 手账式章节标题与表格 ----

    def section(self, num: str, title: str) -> Table:
        """胶带编号块 + 墨绿标题 + 虚线下划线。"""
        t = Table([[Paragraph(num, self.style("jnum", 11, bold=True, color=colors.white,
                                              alignment=TA_CENTER, leading=14)),
                    Paragraph(title, self.style("jtitle", 13.5, bold=True, color=self.PRIMARY))]],
                  colWidths=[10 * mm, CONTENT_W - 10 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), self.ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, self.GRAY, _DASH, (3, 3)),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
        return t

    def table(self, rows: list, widths: list, font_size: int = 9, right_cols: tuple = ()) -> Table:
        """手账表格：墨绿表头 + 纸白/奶油斑马纹 + 虚线行分隔。单元格 Paragraph 原样使用。"""
        body = []
        for i, row in enumerate(rows):
            cells = []
            for j, c in enumerate(row):
                if isinstance(c, Paragraph):
                    cells.append(c)
                    continue
                st = self.style(f"jtc{i}-{j}", font_size)
                if j in right_cols:
                    st.alignment = 2  # TA_RIGHT
                cells.append(Paragraph(str(c), st))
            body.append(cells)
        t = Table(body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), self.PRIMARY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [self.CARD, self.BG_LIGHT]),
            ("LINEBELOW", (0, 1), (-1, -1), 0.5, self.HAIRLINE, _DASH, (3, 2)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    # ---- 拍立得照片框（PIL；失败向外抛，由调用方回退）----

    def _polaroid_ratio(self, size: int) -> float:
        cw, ch, m = _pol_geometry(size)
        return (ch + 2 * m) / (cw + 2 * m)

    def _make_polaroid(self, path: str, caption: str, size: int = 680,
                       tape: tuple | None = None) -> str:
        """奶油底 + 高斯模糊阴影 + 白色厚边卡 + 照片 + 顶部胶带 + 底部手写题字。"""
        tape = tape or self.TAPE_YELLOW_RGB
        key = f"{path}|{caption}|{size}|{tape}|v1"
        out = CROP_DIR / ("pol_" + hashlib.md5(key.encode()).hexdigest()[:12] + ".jpg")
        if out.exists():
            return str(out)
        CROP_DIR.mkdir(parents=True, exist_ok=True)
        cw, ch, m = _pol_geometry(size)
        W, H = cw + 2 * m, ch + 2 * m
        base = PILImage.new("RGBA", (W, H), self.BG_RGB + (255,))
        x0, y0, x1, y1 = m, m - 4, m + cw, m - 4 + ch
        radius = max(6, size // 60)
        sh = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((x0 + 7, y0 + 12, x1 + 7, y1 + 12),
                                             radius=radius, fill=(96, 78, 48, 90))
        sh = sh.filter(ImageFilter.GaussianBlur(max(3, size // 75)))
        base = PILImage.alpha_composite(base, sh)
        d = ImageDraw.Draw(base)
        d.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=(255, 253, 247))
        pad = int(cw * 0.055)
        pw = cw - 2 * pad
        px0, py0 = x0 + pad, y0 + pad
        with PILImage.open(path) as im:
            photo = crop_ratio(im.convert("RGB"), 1.0).resize((pw, pw))
        base.paste(photo, (px0, py0))
        tw, th = int(cw * 0.30), max(10, int(cw * 0.075))
        tape_im = PILImage.new("RGBA", (tw, th), tape + (175,))
        base.paste(tape_im, (x0 + (cw - tw) // 2, y0 - th // 2), tape_im)
        fsize = max(12, int(cw * 0.052))
        f = pil_font(fsize)
        cap = caption if len(caption) <= 12 else caption[:11] + "…"
        try:
            cap_w = d.textlength(cap, font=f)
        except Exception:  # noqa: BLE001 — 个别字体对象无 textlength
            cap_w = len(cap) * fsize
        d.text((x0 + (cw - cap_w) / 2, (py0 + pw + y1) / 2 - fsize / 2),
               cap, font=f, fill=self.INK_RGB)
        base.convert("RGB").save(out, quality=88)
        return str(out)

    def polaroid_cell(self, spot: str, path: str, source: str, tape: tuple | None = None) -> Table:
        """实景速览用拍立得卡；PIL 任一环节失败回退基类 image_cell。"""
        try:
            p = self._make_polaroid(path, spot, size=680, tape=tape)
            w = 74 * mm
            img = Image(p, width=w, height=w * self._polaroid_ratio(680))
        except Exception:  # noqa: BLE001 — 回退基类照片卡
            return self.image_cell(spot, path, source)
        src = source if len(source) <= 96 else source[:93] + "..."
        cells = [[img],
                 [Paragraph(f"来源：{src}", self.style("jpolsrc", 7.5, color=self.GRAY,
                                                       leading=10, alignment=TA_CENTER))]]
        t = Table(cells, colWidths=[85 * mm])
        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t

    def _day_card(self, i: int, day, profile: TravelProfile) -> Table:
        """逐日卡：日期胶带头条（色块 + 点线）+ 时段行；有当日照片时右侧贴迷你拍立得。"""
        basic = profile.basic_info
        wk = weekday(day.date, basic.travel_dates)
        label = f"DAY {i + 1} · {day.date}" + (f" · {wk}" if wk else "")
        slot_st = self.style("jslot", 9, bold=True, color=self.ACCENT)
        body_rows = [
            [Paragraph("上午", slot_st), Paragraph(day.morning or "—", self.style("jdm", 9.5))],
            [Paragraph("下午", slot_st), Paragraph(day.afternoon or "—", self.style("jda", 9.5))],
            [Paragraph("晚上", slot_st), Paragraph(day.evening or "—", self.style("jde", 9.5))],
        ]
        mini_w = 33 * mm
        photo = day_photo(day, profile)
        right = None
        if photo:
            try:
                p = self._make_polaroid(photo, f"DAY {i + 1}", size=300,
                                        tape=self.TAPE_PINK_RGB)
                w = 26 * mm
                right = Image(p, width=w, height=w * self._polaroid_ratio(300))
            except Exception:  # noqa: BLE001 — 拍立得失败则纯文字卡
                right = None
        inner_w = CONTENT_W - mini_w - 2 * mm if right else CONTENT_W
        body = Table([[Paragraph(label, self.style("jday", 10.5, bold=True, color=colors.white)), ""]]
                     + body_rows, colWidths=[18 * mm, inner_w - 18 * mm])
        body.setStyle(TableStyle([
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (1, 0), self.ACCENT),              # 日期胶带
            ("LINEBELOW", (0, 0), (1, 0), 0.7, self.TAPE_EDGE, _DASH, (5, 2)),
            ("BACKGROUND", (0, 1), (0, -1), self.LIGHT),
            ("BACKGROUND", (1, 1), (1, -1), self.CARD),
            ("LINEBELOW", (0, 1), (-1, -2), 0.5, self.HAIRLINE, _DASH, (1, 2)),  # 手账点线
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        if right is None:
            return body
        outer = Table([[body, right]], colWidths=[inner_w + 2 * mm, mini_w])
        outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return outer

    # ---- 七章正文（数据读取与 Classic 一致，版式为手账重设计）----

    def build_story(self, profile: TravelProfile, budget: dict) -> list:
        st = self.style
        basic, detail = profile.basic_info, profile.detail_info
        story: list = []

        # ---- 封面（复古墨绿渐变，复用基类；失败自动回退纯色封面）----
        cover = self.make_cover(profile)
        if cover:
            story.append(Image(cover, width=CONTENT_W,
                               height=CONTENT_W * self.COVER_H / self.COVER_W))
            story.append(PageBreak())

        # ---- 壹 行程总览（手账资料卡：牛皮键栏 + 点线分隔）----
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
                Paragraph(row[0], st("jk1", 9, bold=True, color=self.PRIMARY)),
                Paragraph(str(row[1]), st("jv1", 9)),
                Paragraph(row[2], st("jk2", 9, bold=True, color=self.PRIMARY)),
                Paragraph(str(row[3]), st("jv2", 9)),
            ])
        t = Table(info_cells, colWidths=[20 * mm, 69 * mm, 20 * mm, 69 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), self.LIGHT),
            ("BACKGROUND", (2, 0), (2, -1), self.LIGHT),
            ("BACKGROUND", (1, 0), (1, -1), self.CARD),
            ("BACKGROUND", (3, 0), (3, -1), self.CARD),
            ("BOX", (0, 0), (-1, -1), 0.9, self.HAIRLINE),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, self.HAIRLINE, _DASH, (2, 2)),
            ("LINEAFTER", (1, 0), (1, -1), 0.5, self.HAIRLINE, _DASH, (2, 2)),
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
            story.append(Paragraph(f"每日节奏：{rhythm_line}", st("jrhythm", 9, color=self.GRAY)))
        story.append(Spacer(1, 7))

        # ---- 贰 推荐订单清单 ----
        story.append(self.section("贰", "推荐订单清单（Agent 已按您的要求筛选勾选）"))
        hdr = lambda s: Paragraph(s, st("jth", 9, bold=True, color=colors.white))  # noqa: E731

        def _link_cell(url: str, label: str) -> str:
            """友好锚文本超链接：原始长 URL 会把窄列按字符硬换行撑爆版面。"""
            return (f'<a href="{url}" color="{self.PRIMARY_HEX}"><u>{label}</u></a>' if url else "—")

        order_rows = [[hdr("类型"), hdr("名称/班次"), hdr("关键信息"), hdr("推荐理由"), hdr("直达链接")]]
        for tk in profile.tickets:
            reason = (f'<font face="{bold_font_name()}" color="{self.SUCCESS_HEX}">√ 已勾选</font>　' if tk.selected else "") + (tk.reason or "")
            order_rows.append(["车票", tk.train_no,
                               f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price} 元",
                               Paragraph(reason, st("jreason", 8.5)),
                               Paragraph(_link_cell(tk.link, "12306 购票"), st("jlink", 8.5))])
        for h in profile.hotels:
            reason = (f'<font face="{bold_font_name()}" color="{self.SUCCESS_HEX}">√ 已勾选</font>　' if h.selected else "") + (h.reason or "")
            order_rows.append(["酒店", h.name,
                               f"{h.price_per_night:g} 元/晚，距地标 {h.distance_km:g}km，评分 {h.rating:g}",
                               Paragraph(reason, st("jreason2", 8.5)),
                               Paragraph(_link_cell(h.link, "携程订房"), st("jlink2", 8.5))])
        story.append(self.table(order_rows, [14 * mm, 32 * mm, 50 * mm, 52 * mm, 30 * mm], font_size=8.5))
        total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                          for tk in profile.tickets if tk.selected)
        total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                           for h in profile.hotels if h.selected)
        story.append(Paragraph(f"已勾选订单合计（交通往返 + 住宿）：<font face=\"{bold_font_name()}\">约 {total_order:g} 元</font>",
                               st("jordtot", 10, spaceBefore=4, spaceAfter=2)))
        ref_notes = [x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only]
        if ref_notes:
            story.append(Paragraph("※ 数据来源说明：" + "；".join(sorted(set(ref_notes))),
                                   st("jrefnote", 8.5, color=self.WARN, spaceAfter=4)))
        story.append(Spacer(1, 7))

        # ---- 叁 逐日行程（日期胶带头条 + 点线分隔 + 迷你拍立得）----
        story.append(self.section("叁", "逐日行程"))
        story.append(Spacer(1, 3))
        if profile.draft:
            for i, day in enumerate(profile.draft.days):
                story.append(KeepTogether([self._day_card(i, day, profile), Spacer(1, 6)]))
        story.append(Spacer(1, 2))

        # ---- 肆 预算核算 ----
        story.append(self.section("肆", "预算核算（全团口径）"))
        note = (f"预算 {budget['budget']:g}｜上限 {budget['budget_max']:g}｜"
                f"占用 {budget['occupancy']:.0%}") if budget["occupancy"] else "—"
        b_rows = [[hdr("项目"), hdr("说明"), hdr("金额（元）")]] + [
            [r["item"], r["note"], f"{r['amount']:g}"] for r in budget["items"]
        ]
        b_rows.append([Paragraph("合计", st("jbsum", 9.5, bold=True)), Paragraph(note, st("jbnote", 9)),
                       Paragraph(f"{budget['total']:g}", st("jbamt", 9.5, bold=True, alignment=2))])
        story.append(self.table(b_rows, [26 * mm, 112 * mm, 40 * mm], right_cols=(2,)))
        occ = max(0.0, min(float(budget["occupancy"] or 0), 1.0))
        if occ > 0:
            bar = Table([["", ""]], colWidths=[CONTENT_W * occ, CONTENT_W * (1 - occ)],
                        rowHeights=[3.2 * mm])
            bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), self.PRIMARY if occ < 0.9 else self.WARN),
                ("BACKGROUND", (1, 0), (1, 0), self.LIGHT),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, self.HAIRLINE),
            ]))
            story.append(Spacer(1, 4))
            story.append(KeepTogether([
                bar,
                Paragraph(f"预算占用 {float(budget['occupancy'] or 0):.0%}（合计 {budget['total']:g} 元）",
                          st("jbarcap", 8, color=self.GRAY, spaceBefore=2)),
            ]))
        for w in budget["warnings"]:
            story.append(Paragraph("※ " + w, st("jbwarn", 9.5, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 7))

        # ---- 伍 实景速览（拍立得墙：黄 / 粉胶带交替）----
        if profile.images:
            story.append(self.section("伍", "实景速览（均标注来源）"))
            story.append(Spacer(1, 3))
            imgs = profile.images[:8]
            for r in range(0, len(imgs), 2):
                row_cells = [self.polaroid_cell(img.spot, img.path, img.source,
                                                tape=self.TAPE_PINK_RGB if (r + k) % 4 else self.TAPE_YELLOW_RGB)
                             for k, img in enumerate(imgs[r:r + 2])]
                if len(row_cells) == 1:
                    row_cells.append("")
                t = Table([row_cells], colWidths=[89 * mm, 89 * mm])
                t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                story.append(t)
                story.append(Spacer(1, 5))
            story.append(Spacer(1, 2))

        # ---- 陆 推荐酒店（纸白卡 + 拍立得房型照）----
        enriched = [h for h in profile.hotels if (h.image_path or h.review_digest) and h.selected] \
            or [h for h in profile.hotels if h.image_path or h.review_digest]
        if enriched:
            hotel_flow: list = []
            for h in enriched[:2]:
                left_cells = []
                if h.image_path:  # 空路径直接走文字卡
                    try:
                        pol = self._make_polaroid(h.image_path, h.name[:10], size=380)
                        w = 50 * mm
                        left_cells.append([Image(pol, width=w, height=w * self._polaroid_ratio(380))])
                    except Exception:  # noqa: BLE001 — 图缺退文字
                        left_cells.append([Paragraph("酒店图片暂缺", st("jnoimg", 9, color=self.GRAY))])
                else:
                    left_cells.append([Paragraph("酒店图片暂缺", st("jnoimg2", 9, color=self.GRAY))])
                left = Table(left_cells, colWidths=[56 * mm])
                left.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                          ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
                right_cells = [
                    [Paragraph(h.name, st("jhname", 11, bold=True, color=self.PRIMARY))],
                    [Paragraph(f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km"
                               + ("　<font face=\"{}\" color=\"{}\">√ 已勾选</font>".format(bold_font_name(), self.SUCCESS_HEX)
                                  if h.selected else ""), st("jhmeta", 9))],
                    [Paragraph(f"网络评价：{h.review_digest}" if h.review_digest else "网络评价：暂无",
                               st("jhrev", 8.5, color=self.GRAY, leading=12))],
                ]
                card = Table([[left, right_cells]], colWidths=[60 * mm, CONTENT_W - 60 * mm])
                card.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), self.CARD),
                    ("BOX", (0, 0), (-1, -1), 0.9, self.HAIRLINE),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, self.HAIRLINE, _DASH, (3, 2)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]))
                hotel_flow.append(card)
                hotel_flow.append(Spacer(1, 5))
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
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods_text = ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles)) if titles \
                else "暂无（攻略通道未返回）"
            story.append(Paragraph(foods_text, st("jfoods", 10)))
        story.append(Spacer(1, 6))

        warns: list[str] = []
        for g in profile.guide_digest:
            warns += g.warnings
        seen_w: set[str] = set()
        warn_items = [x for x in warns if not (x in seen_w or seen_w.add(x))][:8]
        if warn_items:
            warn_rows = [[Paragraph("• " + w, st("jwarnitem", 9.5))] for w in warn_items]
            wt = Table(warn_rows, colWidths=[CONTENT_W])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), self.CARD),
                ("BOX", (0, 0), (-1, -1), 0.7, self.HAIRLINE, _DASH, (4, 2)),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, self.HAIRLINE, _DASH, (1, 2)),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(wt)
        for i, g in enumerate(profile.guide_digest[:3]):
            story.append(Paragraph(f"攻略来源[{i + 1}]：{g.source_name} {g.source_url}（抓取 {g.fetched_at}）",
                                   st("jsrc", 8, color=self.GRAY, spaceBefore=3)))
        if profile.weather.get("days"):
            wline = "；".join(f"{d['date']} {d['day_text']} {d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃"
                             for d in profile.weather["days"])
            story.append(Paragraph(f"天气参考（{profile.weather.get('source', '')}）：{wline}",
                                   st("jwx", 8, color=self.GRAY, spaceBefore=3)))
        if basic.defaults_applied:
            story.append(Paragraph("默认值说明：以下字段由系统按默认值补齐——" + "、".join(basic.defaults_applied),
                                   st("jdef", 8, color=self.WARN, spaceBefore=3)))
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"本计划由 TripMate 多 Agent 系统生成 · {datetime.now():%Y-%m-%d %H:%M} · "
                               f"车票/酒店请在官方渠道完成支付（系统不接触支付，§4.4）",
                               st("jfoot", 8, color=self.GRAY)))
        return story
