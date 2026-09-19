"""TravelProfile → 唐风路书模板上下文。

只做数据整形与确定性加工（预算/穿搭/路线段间文案与 reportlab 版式逐项对齐，
功能对齐规格见 docs/pdf-template-plan.md），排版交给 Jinja2 模板。
外部/LLM 文本一律以纯文本进上下文，由模板 autoescape 转义；仅本模块自产的
SVG（内部数值/已转义插值）以 Markup 传入。所有加工失败均回退空值/占位，
不让 PDF 构建失败。
"""

from __future__ import annotations

import base64
import io
import math
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..config import OUTPUT_DIR
from ..models import GuideExtras, TravelProfile
from ..planning import compute_budget
from ..tools.weather import outfit_advice, weather_emoji
from .themes import get_theme, resolve_theme

# 与 pdf_templates/base.py 目的地消毒规则保持一致（HTML 路径独立成包，不反向依赖 reportlab 层）
_DEST_BAD = re.compile(r'[\\/:*?"<>|\r\n]')

# 图表色板默认值（lushu 主题；其余主题经 build_context 从 themes 配置注入）
PALETTE = ["#B03A2E", "#D9A441", "#2E3766", "#5F7E62", "#C7803C", "#8A94B0"]
_STOP_BADGE = {"spot": "🏛", "meal": "🍜", "hotel": "🏨"}


def output_path(profile: TravelProfile, run_id: str) -> Path:
    """成品路径：outputs/行程计划_{消毒后目的地}_{run_id 前 8 位}.pdf（与 reportlab 路径同规则）。"""
    dest = _DEST_BAD.sub("_", (profile.basic_info.destination or "行程").strip()) or "行程"
    return OUTPUT_DIR / f"行程计划_{dest}_{(run_id or '')[:8]}.pdf"


def _safe_url(url: str) -> str:
    """仅放行 http(s) 链接（LLM/外部文本进 href 前的白名单）。"""
    u = (url or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def _img_uri(path: str, max_w: int = 1600) -> str:
    """本地图片 → data URI（超宽降采样 + JPEG 重压缩，控制 HTML 体积）；失败返回空串。"""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        from PIL import Image as PILImage
        with PILImage.open(p) as im:
            im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, max(1, int(im.height * max_w / im.width))))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — 单图失败回退占位，不阻塞
        return ""


def _seg_text(seg) -> str:
    """段间连接行文案（复刻 base.route_day_block 的格式与"估算"标注）。"""
    if not seg.mode:
        return "↓ 距离未知（端点缺坐标）"
    dist = (f"{seg.distance_m / 1000:g}km" if seg.distance_m >= 1000
            else (f"{seg.distance_m}m" if seg.distance_m else ""))
    parts = [x for x in (dist, f"{seg.duration_min}min") if x]
    return f"↓ {seg.mode} " + " / ".join(parts) + ("（估算）" if seg.reference_only else "")


def _real_images(profile: TravelProfile) -> list:
    """实拍图（跳过示意/非实景/占位，与 base.day_photo 同一过滤规则）。"""
    return [i for i in profile.images
            if i.path and not any(k in (i.source or "") for k in ("示意", "非实景", "占位"))]


def _caption_map(profile: TravelProfile) -> dict:
    """景点 → 图注（简介+游玩建议；精确→包含模糊匹配，复刻 classic._caption）。"""
    note_map = {n.name: n for n in profile.spot_notes}

    def caption(spot: str) -> str:
        note = note_map.get(spot)
        if note is None:
            for name, n in note_map.items():
                if spot and (spot in name or name in spot):
                    note = n
                    break
        if note is None:
            return ""
        parts = [p for p in (f"简介：{note.intro}" if note.intro else "",
                             f"游玩：{note.activities}" if note.activities else "") if p]
        return "　".join(parts)

    return caption


def _donut_svg(items: list[tuple[str, float]], total: float,
               ink: str = "#2E3766", muted: str = "#7C7A8C",
               palette: list[str] | None = None) -> str:
    """预算构成环形图（SVG stroke-dasharray；插值仅数值/色值，无转义需求）。"""
    palette = palette or PALETTE
    pts = [(label, float(a)) for label, a in items if isinstance(a, (int, float)) and a > 0]
    if not pts or total <= 0:
        return ""
    if len(pts) > 5:  # 超过 5 项合并为"其他"，保证环形可读
        head, rest = pts[:4], pts[4:]
        pts = head + [("其他", sum(a for _, a in rest))]
    r, c = 54.0, 70.0
    circ = 2 * math.pi * r
    segs, off = [], 0.0
    for i, (_, amount) in enumerate(pts):
        frac = min(amount / total, 1.0)
        segs.append(
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{palette[i % len(palette)]}" '
            f'stroke-width="26" stroke-dasharray="{frac * circ:.2f} {circ:.2f}" '
            f'stroke-dashoffset="{-off * circ:.2f}" transform="rotate(-90 {c} {c})"/>')
        off += frac
    center = (f'<text x="{c}" y="{c - 2}" text-anchor="middle" font-size="13" font-weight="700" '
              f'fill="{ink}" font-family="STZhongsong,SimSun,serif">¥{total:g}</text>'
              f'<text x="{c}" y="{c + 15}" text-anchor="middle" font-size="8" fill="{muted}">'
              f'合计（全团口径）</text>')
    return f'<svg viewBox="0 0 140 140" style="width:42mm;height:42mm">{"".join(segs)}{center}</svg>'


def _stack_svg(items: list[tuple[str, float]], total: float,
               palette: list[str] | None = None) -> str:
    """预算构成横向堆叠条（与环形图同一份数据、同一组色）。"""
    palette = palette or PALETTE
    pts = [(label, float(a)) for label, a in items if isinstance(a, (int, float)) and a > 0]
    if not pts or total <= 0:
        return ""
    w, h = 100.0, 14.0
    rects, x = [], 0.0
    for i, (_, amount) in enumerate(pts):
        frac = min(amount / total, 1.0 - x / 100.0)
        if frac <= 0:
            break
        rects.append(f'<rect x="{x:.2f}" y="0" width="{frac * 100:.2f}" height="{h}" '
                     f'fill="{palette[i % len(palette)]}"/>')
        x += frac * 100.0
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:16pt;display:block;border-radius:4pt">'
            f'{"".join(rects)}</svg>')


def _temp_svg(weather_days: list[dict], accent: str = "#D9A441",
              accent_text: str = "#B9862E", muted: str = "#7C7A8C") -> str:
    """逐日最高温折线（weather.days 含数值温度才渲染；日期为内部数据，仍经 escape）。"""
    pts = [(str(d.get("date") or ""), d.get("temp_max"))
           for d in weather_days if isinstance(d.get("temp_max"), (int, float))]
    if len(pts) < 2:
        return ""
    w, h, pad = 500.0, 150.0, 34.0
    temps = [t for _, t in pts]
    lo, hi = min(temps) - 3, max(temps) + 3
    span = (hi - lo) or 1.0
    step = (w - 2 * pad) / (len(pts) - 1)
    xy = [(pad + i * step, h - pad - (t - lo) / span * (h - 2 * pad)) for i, (_, t) in enumerate(pts)]
    parts = [f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in xy)}" fill="none" '
             f'stroke="{accent}" stroke-width="2.5"/>']
    for (x, y), (date_s, t) in zip(xy, pts):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{accent}"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="10" '
                     f'fill="{accent_text}">{int(t)}°</text>')
        parts.append(f'<text x="{x:.1f}" y="{h - 10:.1f}" text-anchor="middle" font-size="8.5" '
                     f'fill="{muted}">{escape(date_s[5:] or date_s)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;display:block">'
            f'{"".join(parts)}</svg>')


def build_context(profile: TravelProfile, theme: str | None = None) -> dict:
    """TravelProfile → 模板上下文（封面/正文/封底共用）。

    theme：主题名（themes.THEMES 键），None/未知回退默认主题；决定图表色板、
    甘特色块与章节文案（chapters/sec_titles），正文 12 章结构全主题共用。"""
    cfg = get_theme(resolve_theme(theme))
    palette = cfg["palette"]
    basic, detail = profile.basic_info, profile.detail_info
    draft = profile.draft
    party = detail.party_size or basic.party_size or 1
    caption_of = _caption_map(profile)

    # ---- 行程总览（对齐 classic 壹章 6 行键值表）----
    d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
    d1 = basic.travel_dates[-1] if basic.travel_dates else ""
    dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
    budget_txt = f"¥{basic.budget:g}" if basic.budget else "-"
    must = "、".join(detail.must_visit[:6]) or "-"
    styles = " / ".join(basic.style or []) or "休闲"
    rhythm = {"快": "4-5 个景点/天", "慢": "1-2 个景点/天"}.get(detail.pace or "", "2-3 个景点/天")
    hotel_pref = detail.hotel.location_pref or "-"
    overview_rows = [
        ("目的地", basic.destination or "-", "出发地", basic.origin or "-"),
        ("出行时间", dates_txt, "行程天数", f"{basic.days or '-'} 天"),
        ("交通方式", basic.travel_mode or "-", "同行人数", f"{party} 人"),
        ("预算参考", f"{budget_txt}（上限 {basic.budget_max:g}）" if basic.budget_max else budget_txt,
         "旅行风格", styles),
        ("酒店偏好", hotel_pref, "必去景点", must),
        ("游览节奏", rhythm, "数据说明", "车票/酒店以订单清单为准"),
    ]
    rhythm_line = ""
    if draft:
        rhythm_line = " ｜ ".join(
            f"D{i + 1} {'→'.join(d.spots[:3]) or (d.morning or '')[:12]}"
            for i, d in enumerate(draft.days))

    # ---- 天气与穿搭（无预报整体隐藏；气温曲线需 ≥2 个数值点）----
    wdays = profile.weather.get("days") or []
    weather_rows = []
    for d in wdays[:6]:
        day_text = str(d.get("day_text", "")) or "—"
        tmin, tmax = d.get("temp_min"), d.get("temp_max")
        temp = (f"{tmin}~{tmax}℃"
                if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)) else "—")
        weather_rows.append({
            "date": str(d.get("date", "")),
            "emoji": weather_emoji(day_text),
            "day_text": day_text,
            "temp": temp,
            "advice": outfit_advice(day_text, d.get("temp_max"), d.get("temp_min")),
        })
    weather = {
        "rows": weather_rows,
        "source": profile.weather.get("source", ""),
        "curve": _temp_svg(wdays, accent=cfg["svg_accent"],
                           accent_text=cfg["svg_accent_text"], muted=cfg["svg_muted"]),
    } if wdays else None

    # ---- 订单清单（勾选/链接/参考值标注，对齐 classic 叁章）----
    order_rows = []
    for tk in profile.tickets:
        order_rows.append({
            "kind": "车票", "name": tk.train_no,
            "info": f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price:g}\u00A0元",
            "reason": tk.reason or "", "selected": tk.selected,
            "link": _safe_url(tk.link), "link_label": "12306 购票",
        })
    for h in profile.hotels:
        order_rows.append({
            "kind": "酒店", "name": h.name,
            "info": (f"{h.price_per_night:g}\u00A0元/晚，距地标\u00A0{h.distance_km:g}km，"
                     f"评分\u00A0{h.rating:g}"),
            "reason": h.reason or "", "selected": h.selected,
            "link": _safe_url(h.link), "link_label": "携程订房",
        })
    total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                      for tk in profile.tickets if tk.selected)
    total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                       for h in profile.hotels if h.selected)
    ref_sources = sorted({x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only})
    orders = {
        "rows": order_rows,
        "total": f"已勾选订单合计（交通往返 + 住宿）：约 {total_order:g} 元",
        "ref_note": ("※ 数据来源说明：" + "；".join(ref_sources)) if ref_sources else "",
    }

    # ---- 逐日（草稿时段 + 每日路线 + 当日实拍）----
    real = _real_images(profile)
    real_by_spot = {i.spot: i for i in real}

    def match_image(spot: str):
        if spot in real_by_spot:
            return real_by_spot[spot]
        for name, item in real_by_spot.items():
            if spot and (spot in name or name in spot):
                return item
        return None

    day_ctx = []
    for i, day in enumerate((draft.days if draft else [])):
        rd = profile.routes[i] if i < len(profile.routes) else None
        stops, conns = [], []
        if rd and rd.stops:
            for j, s in enumerate(rd.stops):
                label = (f"{s.meal}·" if s.kind == "meal" and s.meal else "") + s.name
                stops.append({
                    "kind": s.kind, "badge": _STOP_BADGE.get(s.kind, "🏛"), "label": label,
                    "address": s.address or "", "note": s.note or "",
                    "nav_url": _safe_url(s.nav_url), "reference_only": s.reference_only,
                })
                if j < len(rd.stops) - 1 and j < len(rd.segments):
                    seg = rd.segments[j]
                    if seg.mode:
                        conns.append(_seg_text(seg))
            n_meal = sum(1 for s in rd.stops if s.kind == "meal")
        else:
            n_meal = 0
        route = None
        if stops:
            names = [s["label"] for s in stops]
            meals = [s["label"] for s in stops if s["kind"] == "meal"]
            route = {
                "total_km": (rd.total_km if rd else 0.0),
                "stops": stops,
                "conns": conns,
                "summary": (rd.summary if rd else ""),
                "warnings": list(rd.warnings) if rd else [],
                "kpi": [("站点", f"{len(stops)} 个"), ("全天约", f"{(rd.total_km if rd else 0):g} km"),
                        ("餐饮", f"{n_meal} 次" if n_meal else "自理")],
                "lane_go": " → ".join(names),
                "lane_traffic": "；".join(conns) if conns else "以市内公共交通 + 步行为主（见路线表）",
                "lane_food": "、".join(meals) if meals else "当日未安排定点餐厅，随行程灵活解决",
            }
        photos = []
        for spot in day.spots:
            item = match_image(spot)
            if item:
                photos.append({"spot": item.spot, "uri": _img_uri(item.path),
                               "caption": caption_of(item.spot)})
            if len(photos) >= 3:
                break
        day_ctx.append({
            "n": i + 1, "date": day.date,
            "weekday": _weekday(day.date, basic.travel_dates),
            "spots": day.spots[:3],
            "morning": day.morning or "—", "afternoon": day.afternoon or "—",
            "evening": day.evening or "—",
            "route": route, "photos": photos,
        })

    # ---- 出发前90秒速览（数据 chips + 每日主题表；主题/一条线优先取 LLM 锦囊）----
    extras: GuideExtras = profile.guide_extras or GuideExtras()
    nights = max((basic.days or 1) - 1, 0)
    dates_short = _short_dates(basic.travel_dates)
    budget_head = f"¥{basic.budget:g}" if basic.budget else "预算待定"
    per_head = f"≈¥{basic.budget / party:g}/人" if basic.budget and party else budget_head
    stat_chips = [
        (f"{party} 人", "结伴同行"),
        (f"{basic.days or '-'} 天 {nights} 晚", f"{basic.travel_mode or '高铁'} 往返"),
        (per_head, f"总预算 {budget_head}" if basic.budget else "预算参考"),
        (dates_short or "日期待定", "出行窗口"),
    ]
    theme_rows = []
    for i, d in enumerate(draft.days if draft else []):
        rd = profile.routes[i] if i < len(profile.routes) else None
        th = extras.day_themes[i] if i < len(extras.day_themes) else None
        theme = (th.theme if th and th.theme else "")
        if not theme:
            theme = " · ".join(d.spots[:2]) or ("自由安排" if not (d.morning or d.afternoon or d.evening) else "当日行程")
        line = (th.line if th and th.line else "") or (" → ".join(d.spots[:5]) if d.spots
                else " · ".join(x for x in (d.morning, d.afternoon, d.evening) if x)[:76])
        meta = (f"{len(rd.stops)} 个点位 · 约 {rd.total_km:g} km" if rd and rd.stops else "")
        theme_rows.append({
            "label": f"D{i + 1}", "date": d.date, "weekday": _weekday(d.date, basic.travel_dates),
            "theme": theme, "line": line or "—", "meta": meta,
        })
    snapshot = {
        "chips": stat_chips,
        "intro": extras.overview_intro,
        "day_rows": theme_rows,
    } if theme_rows or stat_chips else None
    for i, tr in enumerate(theme_rows):   # 逐日 banner 复用同一份当日主题
        if i < len(day_ctx):
            day_ctx[i]["theme"] = tr["theme"]

    # ---- 抢约闹钟日历（锦囊优先；无锦囊时由已选车票/必去点确定性回退；两者皆无则隐藏）----
    alarm_rows = [{"when": a.when, "action": a.action, "channel": a.channel,
                   "difficulty": a.difficulty or "🟡 中"}
                  for a in extras.alarms if a.when or a.action]
    if not alarm_rows:
        year = basic.travel_dates[0][:4] if basic.travel_dates and basic.travel_dates[0][:4].isdigit() else ""
        sel_tks = [t for t in profile.tickets if t.selected] or list(profile.tickets[:2])
        for t in sel_tks[:2]:
            d = _parse_day(basic.travel_dates[0], year) if basic.travel_dates else None
            when = f"{d:%m月%d日} 前 15 天" if d else "开售即抢"
            alarm_rows.append({"when": when, "action": f"12306 抢 {t.train_no} 车票"
                               f"（{t.depart_time} 开出，{t.price:g} 元/人）",
                               "channel": "开售即抢；候补与同窗备选班次同步提交", "difficulty": "🔴 紧俏"})
        if detail.must_visit:
            alarm_rows.append({"when": "出发前 1-7 天", "difficulty": "🟡 中",
                               "action": f"「{'」「'.join(detail.must_visit[:2])}」等热门景点实名预约",
                               "channel": "博物馆/古迹类多需提前 1-7 天在官方公众号或小程序预约"})
    alarms = {"rows": alarm_rows} if alarm_rows else None

    # ---- 行程节奏甘特（draft 三段 → 色块行；纯展示，无时刻数据不编造钟点）----
    gantt = None
    if draft and draft.days:
        g_morning, g_afternoon, g_evening = cfg["gantt_colors"]
        gantt = {
            "note": extras.rhythm_note or "每天按「上午 / 下午 / 晚上」三段推进，累了就停，留白比赶路更重要。",
            "colors": cfg["gantt_colors"],
            "rows": [{"label": f"D{i + 1}", "date": d.date,
                      "blocks": [(g_morning, "上午", d.morning or "自由安排"),
                                 (g_afternoon, "下午", d.afternoon or "自由安排"),
                                 (g_evening, "晚上", d.evening or "自由安排")]}
                     for i, d in enumerate(draft.days)],
        }

    # ---- 预算（数据源 compute_budget，对齐 classic 伍章）----
    budget_ctx = None
    if draft:
        b = compute_budget(profile, draft)
        items = [(str(r.get("item", "")), float(r.get("amount") or 0)) for r in b["items"]]
        occ = max(0.0, min(float(b["occupancy"] or 0), 1.0))
        legend = [{"label": lb, "amount": f"¥{a:g}",
                   "color": palette[i % len(palette)]} for i, (lb, a) in enumerate(items)]
        budget_ctx = {
            "rows": [{"item": r.get("item", ""), "note": r.get("note", ""),
                      "amount": f"{float(r.get('amount') or 0):g}"} for r in b["items"]],
            "note": (f"预算 {b['budget']:g}｜上限 {b['budget_max']:g}｜占用 {b['occupancy']:.0%}"
                     if b["occupancy"] else "—"),
            "total": f"{b['total']:g}",
            "occ_pct": f"{float(b['occupancy'] or 0):.0%}",
            "occ_bar": f"{occ * 100:.0f}%",   # 条宽按 clamp 后比例画（口径与 classic 一致）
            "occ_color": cfg["occ_warn"] if occ >= 0.9 else cfg["occ_ok"],
            "occ_disp": f"预算占用 {float(b['occupancy'] or 0):.0%}（合计 {b['total']:g} 元）",
            "warnings": list(b["warnings"]),
            "donut": _donut_svg(items, float(b["total"] or 0), ink=cfg["svg_ink"],
                                muted=cfg["svg_muted"], palette=palette),
            "stack": _stack_svg(items, float(b["total"] or 0), palette=palette),
            "legend": legend,
        }

    # ---- 实景速览（对齐 classic 陆章：前 8 图 + 图注回退来源）----
    gallery = []
    for item in profile.images[:8]:
        cap = caption_of(item.spot)
        gallery.append({
            "spot": item.spot, "uri": _img_uri(item.path), "caption": cap,
            "source_note": "" if cap else (("来源：" + (item.source or "")[:93]) if item.source else ""),
        })

    # ---- 酒店（三选一定稿：评分序 A★主推/B/C；定稿理由优先取 LLM 锦囊）----
    hotel_tags = ("★ 主推 A · 综合最优", "备选 B · 体验升级", "备选 C · 预算友好")
    hotels = []
    for rank, h in enumerate(profile.hotels[:3], 1):
        hotels.append({
            "rank": rank, "name": h.name, "selected": h.selected,
            "tag": hotel_tags[rank - 1],
            "meta": f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km",
            "desc": (f"网络评价：{h.review_digest}" if h.review_digest
                     else f"推荐理由：{h.reason or '综合评分靠前'}"),
            "img_uri": _img_uri(h.image_path),
        })
    hotel_verdict = extras.hotel_verdict

    # ---- 美食（三态回退，对齐 classic 捌章）----
    if profile.food_notes:
        foods = {"mode": "notes", "notes": [
            {"name": f.name, "intro": f.intro or "", "img_uri": _img_uri(f.image_path)}
            for f in profile.food_notes[:6]]}
    else:
        uniq: list[str] = []
        for g in profile.guide_digest:
            for f in g.foods:
                if f and f not in uniq:
                    uniq.append(f)
        if uniq[:12]:
            foods = {"mode": "grid", "grid": uniq[:12], "notes": []}
        else:
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods = {"mode": "fallback", "grid": [], "notes": [],
                     "text": ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles))
                     if titles else "暂无（攻略通道未返回）"}

    warns: list[str] = []
    for g in profile.guide_digest:
        for w in g.warnings:
            if w and w not in warns:
                warns.append(w)
    warns = warns[:8]
    sources = [{"name": g.source_name, "url": g.source_url, "fetched": g.fetched_at}
               for g in profile.guide_digest[:3]]

    cover = _cover_ctx(profile, party, nights)
    return {
        "basic": {
            "origin": basic.origin or "出发地", "destination": basic.destination or "旅行",
            "days": basic.days or "-", "nights": nights, "dates_txt": dates_txt,
            "mode": basic.travel_mode or "高铁", "styles": styles,
            "party": party, "budget_txt": budget_txt,
        },
        "overview_rows": overview_rows,
        "rhythm_line": rhythm_line,
        "principles": _GENERIC_PRINCIPLES,
        "chapters": cfg["chapter_labels"],
        "sec_titles": cfg["titles"],
        "theme_name": cfg["display_name"],
        "tl_colors": cfg["gantt_colors"],   # 逐日时间轴圆点（上午/下午/晚上，与甘特同色）
        "snapshot": snapshot,
        "alarms": alarms,
        "gantt": gantt,
        "weather": weather,
        "orders": orders,
        "days": day_ctx,
        "budget": budget_ctx,
        "gallery": gallery,
        "hotels": hotels,
        "hotel_verdict": hotel_verdict,
        "foods": foods,
        "warnings": warns,
        "sources": sources,
        "defaults": list(basic.defaults_applied),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "checklist": _GENERIC_CHECKLIST,
        "pretrip": _GENERIC_PRETRIP,
        "emergencies": _GENERIC_EMERGENCIES,
        "cover": cover,
        "back": {
            "title": "旅途愉快",
            "sub": f"{basic.destination or '旅行'} · {basic.days or '-'} 天 {nights} 晚 · {party} 人同行",
            "roles": _TEAM_ROLES,
            "line1": f"{basic.origin or ''} — {basic.destination or ''} · {dates_txt}".strip(" —·"),
            "line2": f"TripMate 多 Agent 协同旅游规划系统 · {datetime.now():%Y 年 %m 月} 统筹汇编",
        },
    }


def _cover_ctx(profile: TravelProfile, party: int, nights: int) -> dict:
    """封面（出片之旅样张版式）：实景照片满版 + 红徽标 + 主题词 + 衬线大标题 + 数据 chips +
    抢票倒计时条；无图时模板回退深色渐变，版式仍成立。"""
    basic = profile.basic_info
    days = basic.days or "-"
    dates_short = _short_dates(basic.travel_dates)
    styles = [s for s in (basic.style or []) if s]
    stats = [
        (dates_short or "日期待定", f"{days} 天 {nights} 晚" if nights > 0 else f"{days} 天行程"),
        (f"¥{int(basic.budget)}" if basic.budget else "-", "预算参考"),
        (f"{party} 人", f"{basic.travel_mode or '高铁'} 往返"),
        (f"{len(profile.detail_info.must_visit)} 个" if profile.detail_info.must_visit else "-",
         "必去点位"),
    ]
    # 满版底图：城市宣传图优先，无则取第一张实拍（示意/占位跳过，与 day_photo 同规则）
    cover_uri = ""
    for p in (profile.cover_images or []):
        cover_uri = _img_uri(p, max_w=1600)
        if cover_uri:
            break
    if not cover_uri:
        for item in _real_images(profile)[:1]:
            cover_uri = _img_uri(item.path, max_w=1600)
    return {
        "badge": f"{basic.destination or '旅行'} · 出行计划",
        "en_line": (f"{basic.origin or ''} — {basic.destination or 'TRIP'} "
                    f"TRAVEL ITINERARY · {datetime.now():%Y}").strip(" —").upper(),
        "motto": " × ".join(styles[:3]) or "轻松出行 × 慢慢看",
        "title": f"{basic.destination or '旅行'}{days}天出行路书。",
        "subtitle": (f"{basic.origin or ''} 出发 · {dates_short or basic.date_text or '日期待定'}"
                     f" · {party} 人同行").strip(" ·"),
        "doc_no": f"{datetime.now():%Y}—{basic.destination or 'TM'}",
        "days_nights": f"{nights} 晚 {days} 天" if nights > 0 else f"{days} 天",
        "stats": stats,
        "countdown": _countdown(profile),
        "cover_uri": cover_uri,
        "foot": f"TripMate 多 Agent 系统统筹生成 · {datetime.now():%Y 年 %m 月}",
    }


def _weekday(date_str: str, travel_dates: list) -> str:
    """周几标注（复刻 base.weekday 的解析与年份推断）。"""
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = _dt.strptime(date_str, fmt)
            if fmt == "%m-%d" and travel_dates and travel_dates[0][:4].isdigit():
                d = d.replace(year=int(travel_dates[0][:4]))
            return "周" + "一二三四五六日"[d.weekday()]
        except ValueError:
            continue
    return ""


def _parse_day(date_str: str, year_hint: str = ""):
    """"2026-10-01"/"10-01" → date（%m-%d 年份取 year_hint 前四位）；失败返回 None。"""
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = _dt.strptime((date_str or "").strip(), fmt)
            if fmt == "%m-%d" and year_hint[:4].isdigit():
                d = d.replace(year=int(year_hint[:4]))
            return d.date()
        except ValueError:
            continue
    return None


def _short_dates(travel_dates: list) -> str:
    """出行窗口短格式（"2026-10-01"…→ "10.01–10.04"）；不可解析回退原文本首项。"""
    if not travel_dates:
        return ""
    d0, d1 = _parse_day(travel_dates[0]), _parse_day(travel_dates[-1])
    if not d0:
        return str(travel_dates[0])
    if d1 and d1 != d0:
        return f"{d0:%m.%d}–{d1:%m.%d}"
    return f"{d0:%m.%d}"


def _countdown(profile: TravelProfile) -> str:
    """封面抢票倒计时条（出片之旅样张版式）：出行日期倒推 15 天为开抢口径；
    日期不可解析或无车票时返回空串，封面自动隐藏该条。"""
    basic = profile.basic_info
    tks = [t for t in profile.tickets if t.selected] or list(profile.tickets[:1])
    base = basic.travel_dates
    if not tks or not base:
        return ""
    year = base[0][:4] if base[0][:4].isdigit() else ""
    dep, ret = _parse_day(base[0], year), (_parse_day(base[-1], year) if len(base) > 1 else None)
    if not dep:
        return ""
    from datetime import timedelta
    bits = [f"{dep - timedelta(days=15):%m月%d日} 前后抢去程 {tks[0].train_no}"]
    if ret and ret != dep:
        bits.append(f"{ret - timedelta(days=15):%m月%d日} 前后抢返程")
    return "行前抢票倒计时：" + " · ".join(bits) + " —— 闹钟现在就设"


# ---- 通用内容（TripMate 数据模型无对应字段，按已确认决策补齐并标注"通用建议"）----

# 封底团队分工（呼应 4-Agent 架构，出片之旅样张封底版式）
_TEAM_ROLES = [("行程统筹", "首席行程主理人"), ("景点调研", "风光体验官"),
               ("交通动线", "交通动线规划师"), ("食宿方案", "食宿品鉴专家")]

_GENERIC_PRINCIPLES = [
    ("晨", "不早起赶场", "核心景点安排在状态最好的上午，出门时间以睡饱为前提。"),
    ("居", "住宿锚点固定", "住交通枢纽附近，全程少搬行李，晚归也有熟路回酒店。"),
    ("缓", "每日留白", "每天 2-3 个核心点即可，留白比赶路更重要，累了就停。"),
    ("顺", "同区串联", "同区域景点一天串完，减少折返与无效通勤。"),
]

_GENERIC_CHECKLIST = [
    ("证件", "身份证、优惠证件（学生证/老年证）"),
    ("充电", "充电宝、充电线、插头"),
    ("雨具", "折叠伞或轻便雨衣"),
    ("防晒", "防晒霜、遮阳帽、墨镜"),
    ("药品", "常用药、创可贴、晕车药"),
    ("穿戴", "舒适的鞋（每天步行量大）"),
    ("饮水", "保温杯或随身水壶"),
    ("现金", "少量现金 + 手机支付备份"),
    ("其他", "纸巾、湿巾、垃圾袋"),
]

_GENERIC_PRETRIP = [
    "热门景点（博物馆/古迹类）多需提前 1-7 天在官方公众号或小程序实名预约，出发前逐个确认。",
    "节假日车票开售即抢，候补与备选车次同步提交；机票早买早便宜。",
    "酒店下单后确认入住时间与取消政策；旺季优先选可免费取消的房型。",
    "每天行程不宜排满：把最重要的 1-2 个点放在上午，其余视体力与天气灵活取舍。",
]

_GENERIC_EMERGENCIES = [
    ("景点约满 / 门票售罄", "改约相邻时段，或启用同区域备选景点（出发前先挑好 1-2 个平替）。"),
    ("恶劣天气", "户外景点替换为博物馆/室内街区等室内方案，雨具提前备好。"),
    ("身体不适", "调整当日节奏就近休息，必要时前往正规医院；行程留有缓冲日更从容。"),
    ("交通中断 / 停运", "改乘其他线路或网约车，先保大交通（返程），市内景点顺延取舍。"),
]
